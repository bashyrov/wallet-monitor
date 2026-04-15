"""
Funding rate screener — fetches perpetual futures funding rates from multiple
exchanges using public (no-auth) endpoints.

Two-tier cache:
  _cache     — price/rate data,  TTL = 30s  (CACHE_TTL)
  _ivl_cache — interval_h data,  TTL = 1h   (IVL_TTL)

Interval data changes very rarely (only when an exchange adjusts funding
frequency for a symbol). Fetching it separately avoids 200+ HTTP requests
per 30s cycle for MEXC/Bitget and keeps Binance/Bybit/OKX instrument calls
out of the hot path.
"""
import asyncio
import logging
import time

import httpx

logger = logging.getLogger("avalant.screener")

_http = httpx.AsyncClient(
    timeout=15,
    headers={"User-Agent": "Mozilla/5.0"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
)

# ── Price/rate cache ───────────────────────────────────────────────────────────
_cache: dict[str, tuple[list, float]] = {}
CACHE_TTL = 6.0  # seconds — slightly longer than broadcast interval to avoid double-fetching

# ── Interval cache ─────────────────────────────────────────────────────────────
_ivl_cache: dict[str, tuple[dict[str, float], float]] = {}
IVL_TTL = 3600.0  # 1 hour — intervals change very rarely

# ── OKX funding-rate cache (500 per-symbol requests, rates change every 8h) ───
# {inst_id: {"rate": float, "next_ts": int}}
_okx_fr_cache: tuple[dict[str, dict], float] = ({}, 0.0)
OKX_FR_TTL = 300.0  # 5 minutes


def _mono() -> float:
    return time.monotonic()


# ══════════════════════════════════════════════════════════════════════════════
# Interval fetchers  (called once per hour per exchange)
# ══════════════════════════════════════════════════════════════════════════════

async def _ivl_binance() -> dict[str, float]:
    """GET /fapi/v1/fundingInfo → {symbol: fundingIntervalHours}"""
    r = await _http.get("https://fapi.binance.com/fapi/v1/fundingInfo")
    r.raise_for_status()
    out: dict[str, float] = {}
    for fi in r.json():
        h = fi.get("fundingIntervalHours")
        if h is not None:
            out[fi["symbol"]] = float(h)
    return out


async def _ivl_bybit() -> dict[str, float]:
    """GET /v5/market/instruments-info → {symbol: interval_h}"""
    r = await _http.get(
        "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000"
    )
    r.raise_for_status()
    out: dict[str, float] = {}
    for inst in r.json().get("result", {}).get("list", []):
        sym = inst.get("symbol", "")
        fi_min = inst.get("fundingInterval")  # minutes
        if fi_min:
            out[sym] = round(int(fi_min) / 60, 2)
    return out


async def _ivl_okx() -> dict[str, float]:
    """GET /api/v5/public/instruments?instType=SWAP → {instId: interval_h}
    Note: interval is derived from fundingTime-prevFundingTime in _fetch_okx_funding_rates().
    This function is kept only to provide the list of USDT inst_ids.
    """
    r = await _http.get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
    r.raise_for_status()
    # Return empty dict — intervals will be filled by _fetch_okx_funding_rates from timestamps
    out: dict[str, float] = {}
    for i in r.json().get("data", []):
        if i.get("settleCcy") != "USDT":
            continue
        out[i["instId"]] = 0.0  # placeholder; real value set in fr_map
    return out


async def _ivl_aster() -> dict[str, float]:
    """GET /fapi/v1/fundingInfo → {symbol: fundingIntervalHours}
    Also fetches exchangeInfo to filter out non-TRADING symbols (1001x/settling contracts).
    """
    info_r, fi_r = await asyncio.gather(
        _http.get("https://fapi.asterdex.com/fapi/v1/exchangeInfo"),
        _http.get("https://fapi.asterdex.com/fapi/v1/fundingInfo"),
    )
    # Only keep symbols actively trading (excludes 1001x SETTLING contracts)
    trading = {
        s["symbol"] for s in (info_r.json().get("symbols") or [])
        if s.get("status") == "TRADING"
    }
    out: dict[str, float] = {}
    for fi in fi_r.json():
        sym = fi.get("symbol", "")
        if sym not in trading:
            continue
        h = fi.get("fundingIntervalHours")
        if h is not None:
            out[sym] = float(h)
    return out



async def _ivl_mexc() -> dict[str, float]:
    """
    Parallel GET /api/v1/contract/funding_rate/{sym} for all USDT symbols.
    collectCycle = interval in hours.
    Done once per hour instead of every 30s (200+ requests).
    """
    tick_r = await _http.get("https://contract.mexc.com/api/v1/contract/ticker")
    tick_r.raise_for_status()
    syms = [
        t["symbol"] for t in (tick_r.json().get("data") or [])
        if t.get("symbol", "").endswith("_USDT")
    ]
    sem = asyncio.Semaphore(5)

    async def _one(sym: str) -> tuple[str, float | None]:
        async with sem:
            try:
                r = await _http.get(
                    f"https://contract.mexc.com/api/v1/contract/funding_rate/{sym}"
                )
                cycle = (r.json().get("data") or {}).get("collectCycle")
                return sym, float(cycle) if cycle is not None else None
            except Exception:
                return sym, None

    results = await asyncio.gather(*[_one(s) for s in syms])
    return {sym: h for sym, h in results if h is not None}


async def _ivl_bitget() -> dict[str, float]:
    """
    Parallel GET /api/v2/mix/market/current-fund-rate for all USDT-FUTURES symbols.
    fundingRateInterval = interval in hours.
    Done once per hour instead of every 30s (200+ requests).
    """
    tick_r = await _http.get(
        "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
    )
    tick_r.raise_for_status()
    syms = [
        t["symbol"] for t in (tick_r.json().get("data") or [])
        if t.get("symbol", "").endswith("USDT")
    ]
    sem = asyncio.Semaphore(5)

    async def _one(sym: str) -> tuple[str, float | None]:
        async with sem:
            try:
                r = await _http.get(
                    f"https://api.bitget.com/api/v2/mix/market/current-fund-rate"
                    f"?symbol={sym}&productType=USDT-FUTURES"
                )
                data = r.json().get("data") or [{}]
                item = data[0] if isinstance(data, list) else data
                ivl = item.get("fundingRateInterval")
                return sym, float(ivl) if ivl is not None else None
            except Exception:
                return sym, None

    results = await asyncio.gather(*[_one(s) for s in syms])
    return {sym: h for sym, h in results if h is not None}


async def _ivl_hyperliquid() -> dict[str, float]:
    """Derive interval from two consecutive funding history entries for BTC."""
    now_ms = int(time.time() * 1000)
    r = await _http.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "fundingHistory", "coin": "BTC", "startTime": now_ms - 7_200_000},
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    entries = r.json()
    if len(entries) >= 2:
        dt_ms = entries[-1]["time"] - entries[-2]["time"]
        interval_h = round(dt_ms / 3_600_000, 2)
    else:
        return {}
    # All HL assets share the same interval
    return {"__all__": interval_h}


async def _ivl_ethereal() -> dict[str, float]:
    """Derive interval from field name: funding_rate1h → 1h per protocol design.
    Confirmed via consecutive funding timestamps (always 1h apart)."""
    # Ethereal exposes no interval field; funding_rate1h naming + settlement timestamps confirm 1h
    # We derive it by checking next_funding_time from market data
    try:
        from ethereal import AsyncRESTClient  # type: ignore
        client = await AsyncRESTClient.create({"base_url": "https://api.ethereal.trade"})
        try:
            products = await client.list_products()
            # All Ethereal products settle hourly — confirmed by next_funding_time spacing
            # Use the funding_rate field name (rate1h) as the contract
            return {"__all__": 1.0}
        finally:
            await client.close()
    except Exception:
        return {}


# ── Interval fetcher registry ──────────────────────────────────────────────────
_IVL_FETCHERS = {
    "binance":      _ivl_binance,
    "bybit":        _ivl_bybit,
    "okx":          _ivl_okx,
    "aster":        _ivl_aster,
    "hyperliquid":  _ivl_hyperliquid,
    "ethereal":     _ivl_ethereal,
    "mexc":         _ivl_mexc,
    "bitget":       _ivl_bitget,
}


async def _get_interval_map(exchange: str) -> dict[str, float]:
    """Return {symbol: interval_h} for the exchange, refreshed every hour."""
    cached, at = _ivl_cache.get(exchange, ({}, 0.0))
    if _mono() - at < IVL_TTL and cached:
        return cached
    fetcher = _IVL_FETCHERS.get(exchange)
    if not fetcher:
        return cached
    try:
        result = await fetcher()
        _ivl_cache[exchange] = (result, _mono())
        logger.debug("Interval map refreshed for %s (%d symbols)", exchange, len(result))
        return result
    except Exception as exc:
        logger.warning("Interval fetch %s failed: %s", exchange, exc)
        return cached  # serve stale on error


# ══════════════════════════════════════════════════════════════════════════════
# Price/rate fetchers  (called every 30s)
# ══════════════════════════════════════════════════════════════════════════════

# ── Binance Futures ────────────────────────────────────────────────────────────
async def _fetch_binance() -> list[dict]:
    prem_r, tick_r = await asyncio.gather(
        _http.get("https://fapi.binance.com/fapi/v1/premiumIndex"),
        _http.get("https://fapi.binance.com/fapi/v1/ticker/24hr"),
    )
    prem_r.raise_for_status()
    tick_r.raise_for_status()
    ivl = await _get_interval_map("binance")
    tick_map: dict[str, dict] = {t["symbol"]: t for t in tick_r.json()}
    out = []
    for item in prem_r.json():
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        token = sym[:-4]
        rate = float(item.get("lastFundingRate") or 0)
        next_ms = int(item.get("nextFundingTime") or 0)
        tick = tick_map.get(sym, {})
        price = float(tick.get("lastPrice") or item.get("markPrice") or 0)
        interval_h = ivl.get(sym)
        if price == 0 or rate == 0 or interval_h is None:
            continue
        out.append({
            "symbol": token,
            "exchange": "binance",
            "price": price,
            "rate": rate,
            "next_ts": next_ms // 1000,
            "interval_h": interval_h,
            "volume_usd": float(tick.get("quoteVolume") or 0),
        })
    return out


# ── Bybit Linear ───────────────────────────────────────────────────────────────
async def _fetch_bybit() -> list[dict]:
    r = await _http.get("https://api.bybit.com/v5/market/tickers?category=linear")
    r.raise_for_status()
    ivl = await _get_interval_map("bybit")
    out = []
    for item in r.json().get("result", {}).get("list", []):
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        token = sym[:-4]
        rate_str = item.get("fundingRate") or ""
        if not rate_str:
            continue
        rate = float(rate_str)
        next_ms = int(item.get("nextFundingTime") or 0)
        price = float(item.get("lastPrice") or item.get("markPrice") or 0)
        interval_h = ivl.get(sym)
        if price == 0 or rate == 0 or interval_h is None:
            continue
        out.append({
            "symbol": token,
            "exchange": "bybit",
            "price": price,
            "rate": rate,
            "next_ts": next_ms // 1000,
            "interval_h": interval_h,
            "volume_usd": float(item.get("turnover24h") or 0),
        })
    return out


# ── Gate.io Futures (USDT-settled) ─────────────────────────────────────────────
async def _fetch_gate() -> list[dict]:
    contracts_r, tickers_r = await asyncio.gather(
        _http.get("https://api.gateio.ws/api/v4/futures/usdt/contracts"),
        _http.get("https://api.gateio.ws/api/v4/futures/usdt/tickers"),
    )
    contracts_r.raise_for_status()
    tickers_r.raise_for_status()
    ticker_map: dict[str, dict] = {t["contract"]: t for t in tickers_r.json()}
    now = int(time.time())
    out = []
    for item in contracts_r.json():
        name = item.get("name", "")
        if not name.endswith("_USDT"):
            continue
        token = name[:-5]
        rate = float(item.get("funding_rate") or 0)
        # funding_next_apply IS the next funding timestamp
        next_ts = int(item.get("funding_next_apply") or 0)
        interval = item.get("funding_interval")
        if not interval:
            continue
        interval = int(interval)
        while next_ts and next_ts < now:
            next_ts += interval
        ticker = ticker_map.get(name, {})
        price = float(ticker.get("last") or ticker.get("mark_price") or item.get("mark_price") or 0)
        vol_usd = float(ticker.get("volume_24h_quote") or ticker.get("volume_24h_usd") or 0)
        if price == 0 or rate == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "gate",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": round(interval / 3600, 2),
            "volume_usd": vol_usd,
        })
    return out


# ── KuCoin Futures ─────────────────────────────────────────────────────────────
async def _fetch_kucoin() -> list[dict]:
    r = await _http.get("https://api-futures.kucoin.com/api/v1/contracts/active")
    r.raise_for_status()
    out = []
    for item in (r.json().get("data") or []):
        sym = item.get("symbol", "")
        if not sym.endswith("USDTM"):
            continue
        token = sym[:-5]
        if token == "XBT":
            token = "BTC"
        rate = float(item.get("fundingFeeRate") or 0)
        next_ms = int(item.get("nextFundingRateDateTime") or 0)
        next_ts = next_ms // 1000 if next_ms else 0
        price = float(item.get("lastTradePrice") or item.get("markPrice") or item.get("indexPrice") or 0)
        if price == 0 or rate == 0:
            continue
        # currentFundingRateGranularity = interval in ms (28800000=8h, 3600000=1h)
        granularity_ms = item.get("currentFundingRateGranularity") or item.get("fundingRateGranularity")
        if not granularity_ms:
            continue
        interval_h = round(int(granularity_ms) / 3_600_000, 2)
        vol_base = float(item.get("volumeOf24h") or 0)
        out.append({
            "symbol": token,
            "exchange": "kucoin",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": interval_h,
            "volume_usd": round(vol_base * price, 2),
        })
    return out


# ── Hyperliquid (perp DEX) ─────────────────────────────────────────────────────
async def _fetch_hyperliquid() -> list[dict]:
    r = await _http.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "metaAndAssetCtxs"},
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    meta, ctxs = r.json()
    universe = meta.get("universe", [])
    ivl_map = await _get_interval_map("hyperliquid")
    interval_h = ivl_map.get("__all__")
    if interval_h is None:
        return []
    now_ts = int(time.time())
    next_ts = (now_ts // int(interval_h * 3600) + 1) * int(interval_h * 3600)
    out = []
    for asset_meta, ctx in zip(universe, ctxs):
        token = asset_meta.get("name", "")
        rate_1h = float(ctx.get("funding") or 0)
        price = float(ctx.get("midPx") or ctx.get("markPx") or 0)
        if price == 0 or rate_1h == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "hyperliquid",
            "price": price,
            "rate": rate_1h,
            "next_ts": next_ts,
            "interval_h": interval_h,
            "volume_usd": float(ctx.get("dayNtlVlm") or 0),
        })
    return out


# ── OKX Linear SWAP ───────────────────────────────────────────────────────────
async def _fetch_okx_funding_rates(inst_ids: list[str]) -> dict[str, dict]:
    """
    Fetch per-symbol funding rates from OKX (500 requests).
    Cached separately for OKX_FR_TTL (5 min) — rates only change every 8h.
    """
    global _okx_fr_cache
    cached, at = _okx_fr_cache
    if _mono() - at < OKX_FR_TTL and cached:
        return cached

    sem = asyncio.Semaphore(50)

    async def _one(inst_id: str) -> tuple[str, dict]:
        async with sem:
            try:
                r = await _http.get(
                    f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}"
                )
                if r.status_code != 200:
                    return inst_id, {}
                d = (r.json().get("data") or [{}])[0]
                rate = float(d.get("fundingRate") or 0)
                next_ts = int(d.get("nextFundingTime") or 0) // 1000
                # derive interval from timestamps (more reliable than instruments field)
                prev_ms = int(d.get("prevFundingTime") or 0)
                curr_ms = int(d.get("fundingTime") or 0)
                interval_h = None
                if prev_ms > 0 and curr_ms > prev_ms:
                    interval_h = round((curr_ms - prev_ms) / 3_600_000, 2)
                return inst_id, {
                    "rate": rate,
                    "next_ts": next_ts,
                    "interval_h": interval_h,
                }
            except Exception:
                return inst_id, {}

    results = await asyncio.gather(*[_one(i) for i in inst_ids])
    fr_map = dict(results)
    _okx_fr_cache = (fr_map, _mono())
    logger.debug("OKX funding rates refreshed (%d symbols)", len(fr_map))
    return fr_map


async def _fetch_okx() -> list[dict]:
    # Prices update every 5s (bulk endpoints); funding rates cached 5min
    tick_r, mark_r = await asyncio.gather(
        _http.get("https://www.okx.com/api/v5/market/tickers?instType=SWAP"),
        _http.get("https://www.okx.com/api/v5/public/mark-price?instType=SWAP"),
    )
    tick_r.raise_for_status()

    ivl = await _get_interval_map("okx")

    # Build USDT inst_ids from cached interval map (already filtered when built)
    inst_ids = list(ivl.keys()) if ivl else []
    # If interval cache is empty (first run), fetch instruments to get inst_ids
    if not inst_ids:
        instr_r = await _http.get(
            "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
        )
        if instr_r.status_code == 200:
            fresh_ivl: dict[str, float] = {}
            for i in instr_r.json().get("data", []):
                if i.get("settleCcy") != "USDT":
                    continue
                fih = i.get("fundingIntervalHours") or i.get("fundingInterval")
                fresh_ivl[i["instId"]] = float(fih) if fih else 8.0
            _ivl_cache["okx"] = (fresh_ivl, _mono())
            ivl = fresh_ivl
            inst_ids = list(ivl.keys())

    # Funding rates: cached 5min (expensive 500-request batch)
    fr_map = await _fetch_okx_funding_rates(inst_ids)

    tick_data = tick_r.json().get("data", [])
    mark_price_map: dict[str, float] = {}
    if mark_r.status_code == 200:
        for m in mark_r.json().get("data", []):
            mark_price_map[m["instId"]] = float(m.get("markPx") or 0)
    price_map: dict[str, float] = {
        t["instId"]: float(t.get("last") or 0) or mark_price_map.get(t["instId"], 0)
        for t in tick_data
    }
    vol_map_okx: dict[str, float] = {
        t["instId"]: float(t.get("volCcy24h") or 0) * float(t.get("last") or 0)
        for t in tick_data
    }

    out = []
    for inst_id in inst_ids:
        fr = fr_map.get(inst_id, {})
        rate = fr.get("rate", 0.0)
        price = price_map.get(inst_id, 0.0)
        interval_h = fr.get("interval_h")
        if price == 0 or rate == 0 or interval_h is None:
            continue
        token = inst_id.replace("-USDT-SWAP", "")
        out.append({
            "symbol": token,
            "exchange": "okx",
            "price": price,
            "rate": rate,
            "next_ts": fr.get("next_ts", 0),
            "interval_h": interval_h,
            "volume_usd": vol_map_okx.get(inst_id, 0),
        })
    return out


# ── MEXC Futures ───────────────────────────────────────────────────────────────
async def _fetch_mexc() -> list[dict]:
    tick_r = await _http.get("https://contract.mexc.com/api/v1/contract/ticker")
    tick_r.raise_for_status()
    tickers = tick_r.json().get("data") or []

    # Interval cache filled by warm-up at startup; if cold — skip until ready
    ivl = await _get_interval_map("mexc")
    if not ivl:
        return []

    out = []
    for item in tickers:
        sym = item.get("symbol", "")
        if not sym.endswith("_USDT"):
            continue
        token = sym[:-5]
        rate = float(item.get("fundingRate") or 0)
        price = float(item.get("lastPrice") or item.get("fairPrice") or 0)
        interval_h = ivl.get(sym)
        if price == 0 or rate == 0 or interval_h is None:
            continue
        next_ts = int(item.get("nextSettleTime") or 0) // 1000
        out.append({
            "symbol": token,
            "exchange": "mexc",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": interval_h,
            "volume_usd": float(item.get("amount24") or 0),
        })
    return out


# ── Bitget USDT Futures ────────────────────────────────────────────────────────
async def _fetch_bitget() -> list[dict]:
    tick_r = await _http.get(
        "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
    )
    tick_r.raise_for_status()
    tickers = tick_r.json().get("data") or []

    ivl = await _get_interval_map("bitget")
    if not ivl:
        return []

    out = []
    for item in tickers:
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        token = sym[:-4]
        rate = float(item.get("fundingRate") or 0)
        price = float(item.get("lastPr") or item.get("markPrice") or 0)
        interval_h = ivl.get(sym)
        if price == 0 or rate == 0 or interval_h is None:
            continue
        next_ts = int(item.get("nextFundingTime") or 0) // 1000
        out.append({
            "symbol": token,
            "exchange": "bitget",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": interval_h,
            "volume_usd": float(item.get("usdtVolume") or 0),
        })
    return out


# ── Aster DEX (Binance Futures-compatible API) ─────────────────────────────────
_aster_vol_cache: tuple[dict[str, float], float] = ({}, 0.0)
_aster_price_cache: tuple[dict[str, float], float] = ({}, 0.0)
ASTER_VOL_TTL = 60.0    # volume changes slowly, refresh every 60s
ASTER_PRICE_TTL = 5.0   # lastPrice refreshed every tick cycle

async def _fetch_aster() -> list[dict]:
    global _aster_vol_cache, _aster_price_cache
    prem_r = await _http.get("https://fapi.asterdex.com/fapi/v1/premiumIndex")
    prem_r.raise_for_status()
    ivl = await _get_interval_map("aster")

    vol_map, vol_at = _aster_vol_cache
    price_map, price_at = _aster_price_cache

    now = _mono()
    # ticker/24hr: fetch every tick for lastPrice; also refresh volume every 60s
    if now - price_at >= ASTER_PRICE_TTL:
        try:
            tick_r = await _http.get("https://fapi.asterdex.com/fapi/v1/ticker/24hr")
            if tick_r.status_code == 200:
                ticks = tick_r.json()
                price_map = {t["symbol"]: float(t.get("lastPrice") or 0) for t in ticks}
                _aster_price_cache = (price_map, now)
                if now - vol_at >= ASTER_VOL_TTL:
                    vol_map = {t["symbol"]: float(t.get("quoteVolume") or 0) for t in ticks}
                    _aster_vol_cache = (vol_map, now)
        except Exception:
            pass

    out = []
    for item in prem_r.json():
        sym = item.get("symbol", "")
        if sym.startswith("SHIELD"):  # 1001x Shield mode — skip
            continue
        if sym.endswith("USDT"):
            token = sym[:-4]
        elif sym.endswith("USD"):
            token = sym[:-3]
        else:
            continue
        rate = float(item.get("lastFundingRate") or 0)
        next_ms = int(item.get("nextFundingTime") or 0)
        # lastPrice from ticker is most up-to-date; fall back to markPrice/indexPrice
        price = price_map.get(sym) or float(item.get("markPrice") or item.get("indexPrice") or 0)
        interval_h = ivl.get(sym)
        if price == 0 or rate == 0 or interval_h is None:
            continue
        out.append({
            "symbol": token,
            "exchange": "aster",
            "price": price,
            "rate": rate,
            "next_ts": next_ms // 1000,
            "interval_h": interval_h,
            "volume_usd": vol_map.get(sym, 0),
        })
    return out


# ── Ethereal DEX (via ethereal-sdk) ───────────────────────────────────────────
async def _fetch_ethereal() -> list[dict]:
    try:
        from ethereal import AsyncRESTClient  # type: ignore
    except ImportError:
        logger.warning("ethereal-sdk not installed, skipping Ethereal")
        return []

    client = await AsyncRESTClient.create({"base_url": "https://api.ethereal.trade"})
    try:
        ivl_map = await _get_interval_map("ethereal")
        interval_h = ivl_map.get("__all__")
        if interval_h is None:
            return []
        products = await client.list_products()
        ids = [p.id for p in products]
        prices = await client.list_market_prices(product_ids=ids)
        price_map = {str(p.product_id): float(p.oracle_price) for p in prices}
        now = int(time.time())
        interval_s = int(interval_h * 3600)
        next_ts = (now // interval_s + 1) * interval_s
        out = []
        for p in products:
            if str(p.status) != "Status1.active":
                continue
            token = p.base_token_name
            rate_1h = float(p.funding_rate1h or 0)
            price = price_map.get(str(p.id), 0)
            if price == 0 or rate_1h == 0:
                continue
            out.append({
                "symbol": token,
                "exchange": "ethereal",
                "price": price,
                "rate": rate_1h,
                "next_ts": next_ts,
                "interval_h": interval_h,
                "volume_usd": 0,
            })
        return out
    finally:
        await client.close()



# ── WhiteBIT Futures (public) ──────────────────────────────────────────────────
async def _fetch_whitebit() -> list[dict]:
    r = await _http.get("https://whitebit.com/api/v4/public/futures")
    r.raise_for_status()
    data = r.json()
    contracts = data if isinstance(data, list) else (data.get("result") or data.get("data") or [])
    out = []
    for item in contracts:
        ticker_id = item.get("ticker_id") or item.get("market") or item.get("name") or ""
        if not ticker_id.endswith("_PERP"):
            continue
        token = ticker_id[:-5]
        rate = float(item.get("funding_rate") or 0)
        price = float(item.get("last_price") or item.get("index_price") or 0)
        interval_min = item.get("funding_interval_minutes")
        if price == 0 or rate == 0 or interval_min is None:
            continue
        next_ts = int(item.get("next_funding_rate_timestamp") or 0) // 1000
        interval_h = round(int(interval_min) / 60, 2)
        vol = float(item.get("money_volume") or item.get("volume_24h") or 0)
        out.append({
            "symbol": token,
            "exchange": "whitebit",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": interval_h,
            "volume_usd": vol,
        })
    return out


# ── BingX Perp Futures (public) ────────────────────────────────────────────────
async def _fetch_bingx() -> list[dict]:
    prem_r, tick_r = await asyncio.gather(
        _http.get("https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"),
        _http.get("https://open-api.bingx.com/openApi/swap/v2/quote/ticker"),
    )
    prem_r.raise_for_status()
    vol_map: dict[str, float] = {}
    last_price_map: dict[str, float] = {}
    if tick_r.status_code == 200:
        for t in (tick_r.json().get("data") or []):
            s = t.get("symbol", "")
            vol_map[s] = float(t.get("quoteVolume") or t.get("volume") or 0)
            last_price_map[s] = float(t.get("lastPrice") or t.get("last") or 0)
    out = []
    for item in (prem_r.json().get("data") or []):
        sym = item.get("symbol") or ""
        if not sym.endswith("-USDT"):
            continue
        token = sym[:-5]
        rate = float(item.get("lastFundingRate") or 0)
        next_ms = int(item.get("nextFundingTime") or 0)
        fih = item.get("fundingIntervalHours")
        if rate == 0 or fih is None:
            continue
        interval_h = float(fih)
        price = last_price_map.get(sym) or float(item.get("markPrice") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "bingx",
            "price": price,
            "rate": rate,
            "next_ts": next_ms // 1000 if next_ms else 0,
            "interval_h": interval_h,
            "volume_usd": vol_map.get(sym, 0),
        })
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Dispatcher
# ══════════════════════════════════════════════════════════════════════════════

FETCHERS: dict[str, object] = {
    "binance":     _fetch_binance,
    "bybit":       _fetch_bybit,
    "okx":         _fetch_okx,
    "gate":        _fetch_gate,
    "kucoin":      _fetch_kucoin,
    "mexc":        _fetch_mexc,
    "bitget":      _fetch_bitget,
    "hyperliquid": _fetch_hyperliquid,
    "aster":       _fetch_aster,
    "ethereal":    _fetch_ethereal,
    "whitebit":    _fetch_whitebit,
    "bingx":       _fetch_bingx,
}


async def _get_rows(exchange: str) -> list[dict]:
    cached_rows, cached_at = _cache.get(exchange, ([], 0.0))
    if _mono() - cached_at < CACHE_TTL and cached_rows:
        return cached_rows
    try:
        rows = await FETCHERS[exchange]()
        _cache[exchange] = (rows, _mono())
        logger.debug("Screener %s: %d contracts", exchange, len(rows))
        return rows
    except Exception as exc:
        logger.warning("Screener %s fetch failed: %s", exchange, exc)
        return cached_rows


async def get_funding_data() -> dict:
    results = await asyncio.gather(
        *(_get_rows(ex) for ex in FETCHERS),
        return_exceptions=True,
    )

    all_rows: list[dict] = []
    for ex, result in zip(FETCHERS.keys(), results):
        if isinstance(result, list):
            for row in result:
                row["apr"] = round(row["rate"] * (8760 / row["interval_h"]) * 100, 4)
            all_rows.extend(result)

    from collections import defaultdict
    sym_exch: dict[str, set] = defaultdict(set)
    for row in all_rows:
        sym_exch[row["symbol"]].add(row["exchange"])
    cross = {sym for sym, exs in sym_exch.items() if len(exs) >= 2}
    for row in all_rows:
        row["cross_listed"] = row["symbol"] in cross

    all_rows.sort(key=lambda r: abs(r["apr"]), reverse=True)

    return {
        "ts": int(time.time()),
        "exchanges": list(FETCHERS.keys()),
        "rows": all_rows,
    }


# ── Fee config (taker, as fraction) ───────────────────────────────────────────
EXCHANGE_FEES: dict[str, float] = {
    "binance":     0.0004,
    "bybit":       0.00055,
    "okx":         0.0005,
    "gate":        0.0005,
    "kucoin":      0.0006,
    "mexc":        0.0002,
    "bitget":      0.0006,
    "hyperliquid": 0.00035,
    "aster":       0.0005,
    "ethereal":    0.0003,

    "whitebit":    0.0006,
    "bingx":       0.0005,
}
_DEFAULT_FEE = 0.0006


def _fee(exchange: str) -> float:
    return EXCHANGE_FEES.get(exchange, _DEFAULT_FEE)


async def get_arbitrage_opportunities() -> dict:
    data = await get_funding_data()
    rows = data["rows"]

    _ARB_EXCLUDE = {"kraken"}

    by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        if r["exchange"] in _ARB_EXCLUDE:
            continue
        by_symbol.setdefault(r["symbol"], []).append(r)

    opportunities: list[dict] = []
    for symbol, entries in by_symbol.items():
        if len(entries) < 2:
            continue
        for i in range(len(entries)):
            for j in range(len(entries)):
                if i == j:
                    continue
                long_e = entries[i]
                short_e = entries[j]
                rate_l = long_e["rate"] * (8.0 / long_e["interval_h"])
                rate_s = short_e["rate"] * (8.0 / short_e["interval_h"])
                gross = rate_s - rate_l
                if gross <= 0:
                    continue
                fee_l = _fee(long_e["exchange"])
                fee_s = _fee(short_e["exchange"])
                total_fees = 2.0 * (fee_l + fee_s)
                p_l = long_e["price"]
                p_s = short_e["price"]
                price_spread = (p_s - p_l) / p_l if p_l > 0 else 0.0
                net = gross + price_spread - total_fees
                opportunities.append({
                    "symbol": symbol,
                    "long_exchange":  long_e["exchange"],
                    "short_exchange": short_e["exchange"],
                    "long_rate":      round(rate_l * 100, 6),
                    "short_rate":     round(rate_s * 100, 6),
                    "long_price":     p_l,
                    "short_price":    p_s,
                    "long_volume":    long_e.get("volume_usd", 0),
                    "short_volume":   short_e.get("volume_usd", 0),
                    "gross_funding":  round(gross * 100, 6),
                    "price_spread":   round(price_spread * 100, 4),
                    "fee_long":       round(fee_l * 100, 4),
                    "fee_short":      round(fee_s * 100, 4),
                    "total_fees":     round(total_fees * 100, 4),
                    "net_profit":     round(net * 100, 6),
                    "gross_apr":      round(gross * (8760 / 8) * 100, 4),
                    "net_apr":        round(net * (8760 / 8) * 100, 4),
                    "valid_price":    p_l <= p_s,
                    "next_ts_long":   long_e.get("next_ts", 0),
                    "next_ts_short":  short_e.get("next_ts", 0),
                })

    opportunities.sort(key=lambda x: x["net_profit"], reverse=True)

    return {
        "ts": data["ts"],
        "exchanges": list(FETCHERS.keys()),
        "fees": {ex: round(f * 100, 4) for ex, f in EXCHANGE_FEES.items()},
        "opportunities": opportunities,
    }


def get_cached_rates() -> dict[str, dict]:
    """Return flat dict {exchange:symbol → {rate, interval_h, price}} from current cache.
    Used by the alert service to check spreads without triggering new fetches.
    """
    result: dict[str, dict] = {}
    for exchange, (rows, _) in _cache.items():
        for row in rows:
            key = f"{exchange}:{row['symbol']}"
            result[key] = {
                "rate":       row.get("rate", 0.0),
                "interval_h": row.get("interval_h", 8),
                "price":      row.get("price", 0.0),
            }
    return result
