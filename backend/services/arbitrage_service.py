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
import os
import time

import httpx

logger = logging.getLogger("avalant.screener")

_http = httpx.AsyncClient(
    # connect=15s: Contabo's outbound path to crypto-exchange edges
    # frequently runs above 5s during TLS+IPv6 fallback. The previous
    # 5s ceiling caused continual ConnectTimeout bursts that left the
    # screener feed 20-30s stale even though every venue's data plane
    # was actually reachable. read=8s is enough — the actual response
    # is sub-second once the handshake lands.
    timeout=httpx.Timeout(connect=15.0, read=8.0, write=5.0, pool=2.0),
    headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=200, max_keepalive_connections=60, keepalive_expiry=30),
    http2=False,  # most exchanges work better with HTTP/1.1 keepalive
)

# ── Price/rate cache ───────────────────────────────────────────────────────────
_cache: dict[str, tuple[list, float]] = {}
CACHE_TTL = 6.0  # seconds — prices fresh enough for 4s recompute; per-fetcher timeout protects against rate-limited exchanges
_FAST_PATH_LAST_WRITE: float = 0.0  # throttle funding.json writes to once per 2s on the fast path

# ── Interval cache ─────────────────────────────────────────────────────────────
_ivl_cache: dict[str, tuple[dict[str, float], float]] = {}
IVL_TTL = 3600.0  # 1 hour — intervals change very rarely
# Per-exchange override: Aster delists symbols ~daily (pre-trading/settled),
# so a 1h ivl cache keeps dead symbols in the arb feed for up to an hour —
# depth endpoint then returns 400 -4108 and orderbook panels look "stuck".
# 10min is short enough that a fresh delist falls off the UI quickly.
IVL_TTL_BY_EX = {"aster": 600.0}

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


# Locks are keyed by (exchange, loop_id) because the spot / dex refresh
# loops spin up fresh event loops per cycle — reusing an asyncio.Lock
# created in a previous loop raises "bound to a different event loop".
_ivl_locks: dict[tuple[str, int], asyncio.Lock] = {}


def _get_ivl_lock(exchange: str) -> asyncio.Lock:
    loop = asyncio.get_event_loop()
    key = (exchange, id(loop))
    lock = _ivl_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ivl_locks[key] = lock
    return lock


async def _refresh_interval(exchange: str) -> None:
    """Fire-and-forget background refresh for slow interval fetchers."""
    fetcher = _IVL_FETCHERS.get(exchange)
    if not fetcher:
        return
    lock = _get_ivl_lock(exchange)
    if lock.locked():
        return
    async with lock:
        try:
            result = await fetcher()
            _ivl_cache[exchange] = (result, _mono())
            logger.info("Interval cache refreshed for %s (%d symbols)", exchange, len(result))
        except Exception as exc:
            logger.warning("Background interval fetch %s failed: %s", exchange, exc)


_SLOW_IVL = {"mexc", "bitget"}  # per-symbol fetch takes 30-45s — never block on these


async def _get_interval_map(exchange: str, *, allow_blocking: bool = True) -> dict[str, float]:
    """Return {symbol: interval_h} for the exchange, refreshed every hour.
    Per-exchange lock prevents duplicate concurrent fetches.
    For slow fetchers (MEXC/Bitget), when cache is cold, kick off refresh in
    background and return whatever we have (possibly empty) — caller falls
    back to a sane default interval."""
    cached, at = _ivl_cache.get(exchange, ({}, 0.0))
    ttl = IVL_TTL_BY_EX.get(exchange, IVL_TTL)
    if _mono() - at < ttl and cached:
        return cached
    fetcher = _IVL_FETCHERS.get(exchange)
    if not fetcher:
        return cached
    lock = _get_ivl_lock(exchange)
    # Slow fetchers: never block user-facing requests
    if exchange in _SLOW_IVL and not allow_blocking:
        if not lock.locked():
            asyncio.create_task(_refresh_interval(exchange))
        return cached
    async with lock:
        # Re-check after acquiring lock — another coroutine may have filled it
        cached, at = _ivl_cache.get(exchange, ({}, 0.0))
        if _mono() - at < IVL_TTL and cached:
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
# Binance keeps delisted perps in /fapi/v1/premiumIndex with status=SETTLING
# (e.g. NTRN), so we cross-check against /fapi/v1/exchangeInfo's TRADING
# set. Cached for 10 min — exchangeInfo changes a few times a day.
_binance_perp_trading_cache: tuple[set[str], float] = (set(), 0.0)
_BINANCE_PERP_INFO_TTL = 600.0


async def _binance_perp_trading_set() -> set[str]:
    global _binance_perp_trading_cache
    syms, ts = _binance_perp_trading_cache
    if syms and (time.time() - ts) < _BINANCE_PERP_INFO_TTL:
        return syms
    try:
        r = await _http.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
        if r.status_code != 200:
            return syms
        fresh = {
            s["symbol"]
            for s in (r.json().get("symbols") or [])
            if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
        }
        if fresh:
            _binance_perp_trading_cache = (fresh, time.time())
            return fresh
    except Exception as exc:
        logger.debug("binance fapi exchangeInfo failed: %s", exc)
    return syms


async def _fetch_binance() -> list[dict]:
    prem_r, tick_r = await asyncio.gather(
        _http.get("https://fapi.binance.com/fapi/v1/premiumIndex"),
        _http.get("https://fapi.binance.com/fapi/v1/ticker/24hr"),
    )
    prem_r.raise_for_status()
    tick_r.raise_for_status()
    ivl = await _get_interval_map("binance")
    trading = await _binance_perp_trading_set()
    tick_map: dict[str, dict] = {t["symbol"]: t for t in tick_r.json()}
    out = []
    for item in prem_r.json():
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        # Drop SETTLING / BREAK perps. Empty trading set (API hiccup) means
        # no filtering — better than an empty arb feed during a Binance blip.
        if trading and sym not in trading:
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
async def _fetch_paradex() -> list[dict]:
    """Paradex perp-DEX on Starknet. Public REST — no auth required for the
    market-summary feed.

    `GET /v1/markets/summary?market=ALL` returns ~600 markets; PERP-only is
    ~80-100 symbols. funding_rate on this endpoint is the *per-period* rate
    (period = 8 h confirmed via /v1/funding/data cross-check), so we store
    it as-is and flag interval_h=8.
    """
    r = await _http.get("https://api.prod.paradex.trade/v1/markets/summary?market=ALL")
    r.raise_for_status()
    items = (r.json() or {}).get("results") or []
    out = []
    # Next funding time is next 8h boundary UTC (Paradex pays on 00/08/16).
    now_ts = int(time.time())
    interval_s = 8 * 3600
    next_ts = (now_ts // interval_s + 1) * interval_s
    for it in items:
        sym = (it.get("symbol") or "")
        if not sym.endswith("-USD-PERP"):
            continue
        # "BTC-USD-PERP" → "BTC"
        base = sym[:-len("-USD-PERP")]
        if not base:
            continue
        try:
            price = float(it.get("mark_price") or it.get("last_traded_price") or 0)
            rate = float(it.get("funding_rate") or 0)
            vol = float(it.get("volume_24h") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or rate == 0:
            continue
        out.append({
            "symbol": base,
            "exchange": "paradex",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": 8.0,
            "volume_usd": vol * price,  # Paradex reports volume in base; convert.
        })
    return out


async def _fetch_lighter() -> list[dict]:
    """Lighter zk perp-DEX. Public REST — no auth required.

    Two endpoints joined by symbol:
      /api/v1/funding-rates   — every market's current rate (Lighter exposes
                                its own + reference rates from binance/bybit/
                                hyperliquid; we filter exchange=='lighter')
      /api/v1/exchangeStats   — last_trade_price + daily quote volume

    Funding interval is 1h (confirmed via /api/v1/fundings, timestamps spaced
    3600s apart). Pay times are on the hour UTC.
    """
    fr_resp, st_resp = await asyncio.gather(
        _http.get("https://mainnet.zklighter.elliot.ai/api/v1/funding-rates"),
        _http.get("https://mainnet.zklighter.elliot.ai/api/v1/exchangeStats"),
        return_exceptions=True,
    )
    if isinstance(fr_resp, Exception) or isinstance(st_resp, Exception):
        return []
    try:
        fr_resp.raise_for_status()
        st_resp.raise_for_status()
    except Exception:
        return []
    rates = (fr_resp.json() or {}).get("funding_rates") or []
    stats = (st_resp.json() or {}).get("order_book_stats") or []

    rate_by_sym: dict[str, float] = {}
    for r in rates:
        if (r.get("exchange") or "").lower() != "lighter":
            continue
        sym = (r.get("symbol") or "").upper()
        if not sym:
            continue
        try:
            rate_by_sym[sym] = float(r.get("rate") or 0)
        except (TypeError, ValueError):
            continue

    now_ts = int(time.time())
    next_ts = (now_ts // 3600 + 1) * 3600

    out: list[dict] = []
    for s in stats:
        sym = (s.get("symbol") or "").upper()
        if sym not in rate_by_sym:
            continue
        try:
            price = float(s.get("last_trade_price") or 0)
            vol_usd = float(s.get("daily_quote_token_volume") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        rate = rate_by_sym[sym]
        if rate == 0:
            continue
        out.append({
            "symbol": sym,
            "exchange": "lighter",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": 1.0,
            "volume_usd": vol_usd,
        })
    return out


async def _fetch_kraken() -> list[dict]:
    """Kraken Futures linear perps — public REST, no auth required.

    Single endpoint: /derivatives/api/v3/tickers returns markPrice +
    fundingRate + 24h volume in one call. We filter to PF_ prefix
    (linear USD-collateralised perps; PI_ is the legacy inverse set
    we ignore). Funding interval is 1h.

    Symbol convention: PF_<TOKEN>USD. Kraken uses XBT for Bitcoin —
    we normalise back to BTC so the screener cross-joins the way
    `BTC` does on every other venue.
    """
    try:
        r = await _http.get("https://futures.kraken.com/derivatives/api/v3/tickers")
        r.raise_for_status()
    except Exception:
        return []
    items = (r.json() or {}).get("tickers") or []
    now_ts = int(time.time())
    next_ts = (now_ts // 3600 + 1) * 3600
    out: list[dict] = []
    for t in items:
        sym = t.get("symbol") or ""
        if not sym.startswith("PF_") or not sym.endswith("USD"):
            continue
        if t.get("suspended"):
            continue
        token = sym[len("PF_"):-len("USD")]
        if token == "XBT":
            token = "BTC"
        try:
            price = float(t.get("markPrice") or t.get("last") or 0)
            rate = float(t.get("fundingRate") or 0)
            vol = float(t.get("volumeQuote") or t.get("vol24h") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or rate == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "kraken",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": 1.0,
            "volume_usd": vol,
        })
    return out


async def _fetch_backpack() -> list[dict]:
    """Backpack perps — public REST, no auth required.

    Three endpoints joined by symbol:
      /api/v1/markets    — fundingInterval (ms) + marketType filter
      /api/v1/markPrices — fundingRate, markPrice, nextFundingTimestamp
      /api/v1/tickers    — 24h quoteVolume in USDC

    Symbol convention: <BASE>_USDC_PERP — we strip the suffix.
    """
    mk_resp, mp_resp, tk_resp = await asyncio.gather(
        _http.get("https://api.backpack.exchange/api/v1/markets"),
        _http.get("https://api.backpack.exchange/api/v1/markPrices"),
        _http.get("https://api.backpack.exchange/api/v1/tickers"),
        return_exceptions=True,
    )
    if any(isinstance(x, Exception) for x in (mk_resp, mp_resp, tk_resp)):
        return []
    try:
        mk_resp.raise_for_status()
        mp_resp.raise_for_status()
        tk_resp.raise_for_status()
    except Exception:
        return []
    markets = mk_resp.json() or []
    mark_prices = mp_resp.json() or []
    tickers = tk_resp.json() or []

    # symbol → fundingInterval_h (default 1h if absent)
    ivl_by_sym: dict[str, float] = {}
    for m in markets:
        if (m.get("marketType") or "") != "PERP":
            continue
        sym = m.get("symbol") or ""
        ivl_ms = m.get("fundingInterval")
        if not sym:
            continue
        try:
            ivl_by_sym[sym] = max(1.0, float(ivl_ms) / 3_600_000.0) if ivl_ms else 1.0
        except (TypeError, ValueError):
            ivl_by_sym[sym] = 1.0

    # symbol → 24h quote volume (USDC ≈ USD)
    vol_by_sym: dict[str, float] = {}
    for t in tickers:
        sym = t.get("symbol") or ""
        if not sym.endswith("_USDC_PERP"):
            continue
        try:
            vol_by_sym[sym] = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue

    out: list[dict] = []
    for mp in mark_prices:
        sym = mp.get("symbol") or ""
        if not sym.endswith("_USDC_PERP"):
            continue
        base = sym[:-len("_USDC_PERP")]
        if not base or sym not in ivl_by_sym:
            continue
        try:
            price = float(mp.get("markPrice") or 0)
            rate = float(mp.get("fundingRate") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or rate == 0:
            continue
        next_ts_ms = mp.get("nextFundingTimestamp")
        try:
            next_ts = int(next_ts_ms) // 1000 if next_ts_ms else 0
        except (TypeError, ValueError):
            next_ts = 0
        out.append({
            "symbol": base,
            "exchange": "backpack",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": ivl_by_sym[sym],
            "volume_usd": vol_by_sym.get(sym, 0.0),
        })
    return out


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
_okx_fr_refresh_inflight = False


async def _refresh_okx_funding_rates(inst_ids: list[str]) -> None:
    """Background: fetch per-symbol funding rates (500 req). Caller does NOT await
    this — price-path must stay fast. After completion, _okx_fr_cache is updated."""
    global _okx_fr_cache, _okx_fr_refresh_inflight
    if _okx_fr_refresh_inflight:
        return
    _okx_fr_refresh_inflight = True
    try:
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
                    prev_ms = int(d.get("prevFundingTime") or 0)
                    curr_ms = int(d.get("fundingTime") or 0)
                    interval_h = None
                    if prev_ms > 0 and curr_ms > prev_ms:
                        interval_h = round((curr_ms - prev_ms) / 3_600_000, 2)
                    return inst_id, {"rate": rate, "next_ts": next_ts, "interval_h": interval_h}
                except Exception:
                    return inst_id, {}

        results = await asyncio.gather(*[_one(i) for i in inst_ids])
        fr_map = dict(results)
        _okx_fr_cache = (fr_map, _mono())
        logger.info("OKX funding rates refreshed (%d symbols)", len(fr_map))
    finally:
        _okx_fr_refresh_inflight = False


async def _fetch_okx_funding_rates(inst_ids: list[str]) -> dict[str, dict]:
    """Return cached funding rates immediately. Trigger background refresh if
    cache expired. NEVER blocks the caller on 500 HTTP requests."""
    cached, at = _okx_fr_cache
    if _mono() - at >= OKX_FR_TTL or not cached:
        asyncio.create_task(_refresh_okx_funding_rates(inst_ids))
    return cached or {}


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

    # MEXC per-symbol interval fetch takes ~45s (461 requests) — never block;
    # kick off background refresh. Symbols not yet in the cache are SKIPPED
    # rather than emitted with a guessed 4h default: a wrong interval mis-
    # normalises APR by 2x and would leak into the screener for the ~45s
    # between cold start and first warm cache write.
    ivl = await _get_interval_map("mexc", allow_blocking=False)

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

    # Same as MEXC — per-symbol interval fetch is slow; skip symbols whose
    # interval is not yet cached rather than emit a guessed default.
    ivl = await _get_interval_map("bitget", allow_blocking=False)

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
    # ticker/24hr: fetch every tick for lastPrice; also refresh volume every 60s.
    # If the fetch fails, log it AND invalidate the caches past their grace
    # window so we don't silently ship stale prices/volumes forever.
    if now - price_at >= ASTER_PRICE_TTL:
        try:
            tick_r = await _http.get("https://fapi.asterdex.com/fapi/v1/ticker/24hr")
            tick_r.raise_for_status()
            ticks = tick_r.json()
            price_map = {t["symbol"]: float(t.get("lastPrice") or 0) for t in ticks}
            _aster_price_cache = (price_map, now)
            if now - vol_at >= ASTER_VOL_TTL:
                vol_map = {t["symbol"]: float(t.get("quoteVolume") or 0) for t in ticks}
                _aster_vol_cache = (vol_map, now)
        except Exception as exc:
            logger.warning("aster ticker/24hr fetch failed: %s: %s", type(exc).__name__, exc)
            # Evict caches that are more than 3× their normal TTL old —
            # keeps us from serving minute-old aster prices when the API
            # is having an extended outage.
            if now - price_at > ASTER_PRICE_TTL * 3:
                _aster_price_cache = ({}, now)
                price_map = {}
            if now - vol_at > ASTER_VOL_TTL * 3:
                _aster_vol_cache = ({}, now)
                vol_map = {}

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


# ── HTX (Huobi) Perp Futures (public) ─────────────────────────────────────────
# USDT-M Linear swap: funding is typically 8h across the board.
async def _fetch_htx() -> list[dict]:
    fund_r, tick_r = await asyncio.gather(
        _http.get("https://api.hbdm.com/linear-swap-api/v1/swap_batch_funding_rate"),
        _http.get("https://api.hbdm.com/linear-swap-ex/market/detail/batch_merged"),
    )
    fund_r.raise_for_status()
    rates: dict[str, dict] = {}
    for it in (fund_r.json().get("data") or []):
        cc = it.get("contract_code") or ""
        if not cc.endswith("-USDT"):
            continue
        token = cc[:-5]
        try:
            rate = float(it.get("funding_rate") or 0)
        except (TypeError, ValueError):
            continue
        if rate == 0:
            continue
        next_ms = int(it.get("funding_time") or 0)
        rates[token] = {"rate": rate, "next_ts": next_ms // 1000 if next_ms else 0}
    tick_r.raise_for_status()
    ticks = (tick_r.json().get("ticks") or tick_r.json().get("data") or [])
    prices: dict[str, tuple[float, float]] = {}
    for t in ticks:
        cc = t.get("contract_code") or ""
        if not cc.endswith("-USDT"):
            continue
        token = cc[:-5]
        try:
            price = float(t.get("close") or 0)
            vol_usd = float(t.get("trade_turnover") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        prices[token] = (price, vol_usd)
    out = []
    for token, fr in rates.items():
        pv = prices.get(token)
        if not pv:
            continue
        price, vol = pv
        out.append({
            "symbol": token,
            "exchange": "htx",
            "price": price,
            "rate": fr["rate"],
            "next_ts": fr["next_ts"],
            "interval_h": 8.0,
            "volume_usd": vol,
        })
    return out


# ── Extended (Starknet perpetual DEX, public) ─────────────────────────────────
# Hourly funding across all USD-collateralised markets.
async def _fetch_extended() -> list[dict]:
    r = await _http.get("https://api.starknet.extended.exchange/api/v1/info/markets")
    r.raise_for_status()
    out = []
    for m in (r.json().get("data") or []):
        if m.get("status") != "ACTIVE" or not m.get("active"):
            continue
        name = m.get("name") or ""
        if not name.endswith("-USD"):
            continue
        token = name[:-4]
        stats = m.get("marketStats") or {}
        try:
            price = float(stats.get("lastPrice") or stats.get("markPrice") or 0)
            rate = float(stats.get("fundingRate") or 0)
            vol_usd = float(stats.get("dailyVolume") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or rate == 0:
            continue
        next_ms = int(stats.get("nextFundingRate") or 0)
        out.append({
            "symbol": token,
            "exchange": "extended",
            "price": price,
            "rate": rate,
            "next_ts": next_ms // 1000 if next_ms else 0,
            "interval_h": 1.0,
            "volume_usd": vol_usd,
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
    "paradex":     _fetch_paradex,
    "htx":         _fetch_htx,
    "extended":    _fetch_extended,
    "lighter":     _fetch_lighter,
    "backpack":    _fetch_backpack,
    "kraken":      _fetch_kraken,
}


_FETCHER_TIMEOUT = 10.0  # bound per-exchange fetch so one slow API can't stall the gather


async def _get_rows(exchange: str) -> list[dict]:
    # Prefer the WS-funding stream if healthy — avoids REST rate-limits and
    # gives 100-300ms freshness instead of 3-6s.
    try:
        from backend.services.funding_ws import get_ws_rows
        ws_rows = get_ws_rows(exchange)
    except Exception as exc:
        logger.debug("%s: get_ws_rows raised: %s", exchange, exc)
        ws_rows = None
    if ws_rows:
        # Normalise every row to the REST-schema and stamp the exchange.
        # If `interval_h` is missing the row is dropped — we must know the
        # funding interval to compute APR correctly. Every adapter sets it
        # explicitly for its venue (usually 8.0) so a missing value means
        # the adapter changed and nobody updated the downstream assumption.
        #
        # Aster-specific: the WS feed keeps delivering premiumIndex rows for
        # symbols that are in SETTLING / DELIVERED status (DEGO etc). They
        # appear in arb/spot opps, then the depth endpoint 400s because
        # they're no longer tradable. Intersect with the TRADING-only ivl
        # map to drop them before they reach the UI.
        aster_tradable: set[str] | None = None
        if exchange == "aster":
            ivl_map, ivl_at = _ivl_cache.get("aster", ({}, 0.0))
            # Kick off a background refresh when cache is stale/empty — WS
            # path otherwise never triggers _fetch_aster → ivl_map stays {}.
            ttl = IVL_TTL_BY_EX.get("aster", IVL_TTL)
            if (_mono() - ivl_at) > ttl or not ivl_map:
                lock = _get_ivl_lock("aster")
                if not lock.locked():
                    asyncio.create_task(_refresh_interval("aster"))
            if ivl_map:
                aster_tradable = {k[:-4] for k in ivl_map.keys() if k.endswith("USDT")}
        normalised: list[dict] = []
        dropped = 0
        for r in ws_rows:
            if r.get("interval_h") is None:
                dropped += 1
                continue
            if aster_tradable is not None and r.get("symbol") not in aster_tradable:
                continue
            r["exchange"] = exchange
            r.setdefault("volume_usd", 0)
            r.setdefault("next_ts", 0)
            normalised.append(r)
        if dropped:
            logger.warning("%s WS: dropped %d rows missing interval_h", exchange, dropped)
        _cache[exchange] = (normalised, _mono())
        return normalised

    cached_rows, cached_at = _cache.get(exchange, ([], 0.0))
    if _mono() - cached_at < CACHE_TTL and cached_rows:
        return cached_rows
    # Circuit breaker — skip REST entirely when the venue is in cooldown.
    # Serves last-known cache instead of hammering a flaky endpoint.
    from backend.services._circuit import circuit as _circuit
    if not _circuit.allow(f"rest:{exchange}"):
        return cached_rows
    try:
        rows = await asyncio.wait_for(FETCHERS[exchange](), timeout=_FETCHER_TIMEOUT)
        _cache[exchange] = (rows, _mono())
        _circuit.ok(f"rest:{exchange}")
        logger.debug("Screener %s: %d contracts (REST)", exchange, len(rows))
        return rows
    except asyncio.TimeoutError:
        _circuit.fail(f"rest:{exchange}")
        logger.warning("Screener %s fetch timeout (>%ss) — using cached", exchange, _FETCHER_TIMEOUT)
        return cached_rows
    except Exception as exc:
        msg = str(exc)
        # 418 = Binance/Aster IP ban (can last 2-10 min). Open circuit
        # immediately with a long cooldown so we don't keep triggering the
        # ban and starving the shared pool with doomed retries.
        if "418" in msg or "I'm a teapot" in msg or "Client Error (418)" in msg:
            _circuit.hard_fail(f"rest:{exchange}", cooldown_s=180.0)
            logger.warning("Screener %s: HTTP 418 — opening circuit 180s", exchange)
            return cached_rows
        # 429 — same idea, shorter cooldown.
        if "429" in msg or "Too Many Requests" in msg:
            _circuit.hard_fail(f"rest:{exchange}", cooldown_s=60.0)
            logger.warning("Screener %s: HTTP 429 — opening circuit 60s", exchange)
            return cached_rows
        _circuit.fail(f"rest:{exchange}")
        logger.warning("Screener %s fetch failed: %s: %r", exchange, type(exc).__name__, exc)
        return cached_rows


_FILE_CACHE_DIR = "/tmp/avalant_cache"


def _write_file_cache(name: str, data: dict) -> None:
    """Atomically write JSON to a file so other workers can read it."""
    import json as _json, os, tempfile
    os.makedirs(_FILE_CACHE_DIR, exist_ok=True)
    path = os.path.join(_FILE_CACHE_DIR, name)
    try:
        fd, tmp = tempfile.mkstemp(dir=_FILE_CACHE_DIR, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            _json.dump(data, f)
        os.replace(tmp, path)  # atomic on POSIX
    except Exception:
        logger.exception("file cache write failed: %s", name)


# In-process memoization for parsed file caches. Same mtime → return the
# previously parsed object; mtime changed → re-parse. Cuts arbitrage.json /
# funding.json read latency from ~50ms (open + parse 0.5–1MB) to <100µs
# (one os.stat + dict reference). Each /screener/* request that previously
# re-parsed the file now hits this memoization. Stable mtime even at the
# go-fetcher's 250ms dump cadence is enough — we re-parse at most 4× per
# second per file across all replicas.
_PARSE_CACHE: dict[str, tuple[float, dict]] = {}
_PARSE_CACHE_LOCK = __import__("threading").Lock()


def _read_file_cache(name: str, max_age: float = 60.0) -> dict | None:
    """Read JSON from file cache. Returns None if missing or stale.

    Memoized by mtime: we keep the last parsed dict in `_PARSE_CACHE` and
    only re-parse when the file's mtime changes. The age-vs-max_age check
    still applies to the actual file, so a stale dump is treated the same
    way regardless of memoization."""
    import json as _json, os
    path = os.path.join(_FILE_CACHE_DIR, name)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("file cache stat failed: %s", name)
        return None
    mtime = st.st_mtime
    if (time.time() - mtime) > max_age:
        return None
    cached = _PARSE_CACHE.get(name)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with open(path) as f:
            data = _json.load(f)
    except Exception:
        logger.exception("file cache read failed: %s", name)
        return None
    with _PARSE_CACHE_LOCK:
        _PARSE_CACHE[name] = (mtime, data)
    return data


async def _read_file_cache_async(name: str, max_age: float = 60.0) -> dict | None:
    """Async wrapper for _read_file_cache — offloads the blocking read to a
    thread so the caller's event loop is not stalled by disk IO + JSON parse.
    Hot paths like the screener refresh loop (300 ms cadence) were blocking
    the loop for 20-50 ms per tick on the 1 MB funding.json parse, stacking
    up to multi-second event-loop stalls that timed out `asyncio.wait_for`
    on REST fetches."""
    import asyncio as _asyncio
    return await _asyncio.to_thread(_read_file_cache, name, max_age)


async def _write_file_cache_async(name: str, data: dict) -> None:
    """Async wrapper for _write_file_cache — same rationale as the read."""
    import asyncio as _asyncio
    await _asyncio.to_thread(_write_file_cache, name, data)


# ── Price sanity check (cross-exchange anomaly detection) ─────────────────────
#
# Some adapters (notoriously KuCoin) occasionally return stale or wrong
# `lastPrice` for futures symbols — e.g. RAVE at $0.64 while every other
# exchange quotes $1.2. We can't trust a single exchange to tell us it's
# wrong, but we can spot outliers by comparing to the median across all
# exchanges listing the same symbol. When an exchange deviates by > _PRICE_DEV_PCT
# from median, log it at WARNING so ops can see which feed is drifting.
#
# Also flag obviously-broken rows: zero/negative price.

_PRICE_DEV_PCT = 25.0      # % deviation from median to log
_PRICE_DROP_DEV_PCT = 20.0 # drop rows whose price deviates this much from median
_MIN_EX_FOR_MEDIAN = 3     # need at least this many exchanges to trust the median
_anomaly_counters: dict[str, int] = {}  # {"kucoin": count} running tally

# Throttle: only log each (exchange, symbol) once per N seconds.
_last_anomaly_log: dict[tuple[str, str], float] = {}
_ANOMALY_LOG_COOLDOWN = 300.0  # 5 min


def price_anomaly_counters() -> dict[str, int]:
    """Expose running anomaly tally to admin endpoints.
    Web role reads from the shared file written by fetcher; fetcher
    returns its in-memory dict.
    """
    if os.environ.get("AVALANT_ROLE", "").lower() == "web":
        fc = _read_file_cache("price_anomalies.json", max_age=300.0)
        if fc and isinstance(fc.get("by_exchange"), dict):
            return dict(fc["by_exchange"])
        return {}
    return dict(_anomaly_counters)


def _drop_price_outliers(rows: list[dict]) -> list[dict]:
    """Drop rows whose price deviates > _PRICE_DROP_DEV_PCT from cross-exchange
    median for the same symbol. Stale markPrice from a delisted contract (e.g.
    Binance still broadcasting FUN at $0.0004 vs Gate $0.035) would otherwise
    create fake 7000% arb opportunities. Requires at least _MIN_EX_FOR_MEDIAN
    listings to trust the median."""
    try:
        from statistics import median
    except Exception:
        return rows
    from collections import defaultdict
    by_sym: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for idx, r in enumerate(rows):
        try:
            p = float(r.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if p <= 0:
            continue
        by_sym[r.get("symbol", "")].append((idx, p))
    drop_idx: set[int] = set()
    for sym, pairs in by_sym.items():
        if len(pairs) < _MIN_EX_FOR_MEDIAN:
            continue
        med = median(p for _, p in pairs)
        if med <= 0:
            continue
        for idx, p in pairs:
            if abs(p - med) / med * 100.0 > _PRICE_DROP_DEV_PCT:
                drop_idx.add(idx)
    if not drop_idx:
        return rows
    return [r for i, r in enumerate(rows) if i not in drop_idx]


def _sanity_check_prices(rows: list[dict]) -> None:
    """Cross-exchange outlier + zero-price detection.
    Logs at WARNING (one entry per (exchange, symbol) per cooldown window)
    and increments `_anomaly_counters` for admin dashboard.
    """
    try:
        from statistics import median
    except Exception:
        return

    from collections import defaultdict
    by_sym: dict[str, list[tuple[str, float]]] = defaultdict(list)
    now = _mono()

    for r in rows:
        ex = r.get("exchange") or ""
        sym = r.get("symbol") or ""
        price = r.get("price")
        if price is None:
            _record_anomaly(ex, sym, "missing", None, None, now)
            continue
        try:
            p = float(price)
        except (TypeError, ValueError):
            _record_anomaly(ex, sym, "not_numeric", price, None, now)
            continue
        if p <= 0:
            _record_anomaly(ex, sym, "zero_or_neg", p, None, now)
            continue
        if ex and sym:
            by_sym[sym].append((ex, p))

    # Cross-sectional median check
    for sym, pairs in by_sym.items():
        if len(pairs) < _MIN_EX_FOR_MEDIAN:
            continue
        prices = [p for _, p in pairs]
        med = median(prices)
        if med <= 0:
            continue
        for ex, p in pairs:
            dev_pct = abs(p - med) / med * 100.0
            if dev_pct > _PRICE_DEV_PCT:
                _record_anomaly(ex, sym, "outlier", p, med, now, dev_pct=dev_pct)


def _record_anomaly(
    ex: str, sym: str, kind: str, price, median_val, now: float, *, dev_pct: float | None = None,
) -> None:
    key = (ex, sym, kind)
    last = _last_anomaly_log.get(key, 0.0)
    if now - last < _ANOMALY_LOG_COOLDOWN:
        return
    _last_anomaly_log[key] = now
    _anomaly_counters[ex] = _anomaly_counters.get(ex, 0) + 1

    if kind == "outlier":
        logger.warning(
            "price_anomaly %s %s: %.6g vs median %.6g (dev=%.1f%%)",
            ex, sym, price, median_val or 0, dev_pct or 0,
        )
    elif kind == "zero_or_neg":
        logger.warning("price_anomaly %s %s: non-positive price %.6g", ex, sym, price or 0)
    elif kind == "not_numeric":
        logger.warning("price_anomaly %s %s: non-numeric price %r", ex, sym, price)
    elif kind == "missing":
        logger.warning("price_anomaly %s %s: missing price field", ex, sym)


async def get_funding_data() -> dict:
    from backend.services import admin_settings
    disabled_ex = admin_settings.get_disabled_exchanges()
    hidden_sym = admin_settings.get_hidden_symbols()
    min_volume = admin_settings.get_arb_min_volume_usd()
    enabled_ex = [ex for ex in FETCHERS if ex not in disabled_ex]

    def _keep(row: dict) -> bool:
        """Row passes into the merged cache only if the venue actually
        reports a 24h USD volume that clears the floor AND a non-zero
        funding rate. Missing / zero volume → dropped. Rate exactly 0 →
        dropped too (no venue reports a truly-zero funding; 0 means the
        value is uninitialised / hasn't been received from the WS / REST
        feed yet, and letting it through creates fake 'free' arb pairs)."""
        if hidden_sym and row["symbol"] in hidden_sym:
            return False
        v = row.get("volume_usd")
        if v is None:
            return False
        rate = row.get("rate")
        if rate is None:
            return False
        try:
            if float(rate) == 0.0:
                return False
            return float(v) >= min_volume
        except (TypeError, ValueError):
            return False

    # Web role has no data plane — always read from shared file written by
    # the fetcher sidecar. Avoid kicking off our own REST gather.
    if os.environ.get("AVALANT_ROLE", "").lower() == "web":
        cached = await _read_file_cache_async("funding.json", max_age=30.0)
        if cached and cached.get("rows"):
            rows = [r for r in cached["rows"] if _keep(r)]
            return {"ts": cached.get("ts", int(time.time())), "exchanges": enabled_ex, "rows": rows}
        # No file yet / stale — fall through; first request after startup only.

    # Fast path: if every per-exchange cache is still warm, skip the gather.
    # Non-owner workers can fall back to the shared funding.json while the
    # owner's gather runs.
    now_m = _mono()
    any_stale = any(
        (now_m - _cache.get(ex, ([], 0.0))[1]) > CACHE_TTL
        for ex in enabled_ex
    )
    if not any_stale:
        all_rows: list[dict] = []
        for ex in enabled_ex:
            cached_rows, _ts = _cache.get(ex, ([], 0.0))
            all_rows.extend(cached_rows)
        all_rows = [r for r in all_rows if _keep(r)]
        if all_rows:
            out = {"ts": int(time.time()), "exchanges": enabled_ex, "rows": all_rows}
            # Throttled file write — web role reads this for `via=rest` freshness,
            # and without it funding.json stays stale forever when every WS is
            # healthy (fast-path never reaches the gather-then-write block).
            global _FAST_PATH_LAST_WRITE
            now_t = time.time()
            if now_t - _FAST_PATH_LAST_WRITE >= 2.0:
                await _write_file_cache_async("funding.json", out)
                _FAST_PATH_LAST_WRITE = now_t
            return out

    results = await asyncio.gather(
        *(_get_rows(ex) for ex in enabled_ex),
        return_exceptions=True,
    )

    all_rows: list[dict] = []
    for ex, result in zip(enabled_ex, results):
        if isinstance(result, list):
            for row in result:
                ivl = row.get("interval_h")
                row["apr"] = round(row["rate"] * (8760 / ivl) * 100, 4) if ivl else None
            all_rows.extend(result)

    all_rows = [r for r in all_rows if _keep(r)]
    # price-deviation outlier filter disabled by request — all rows pass through

    from collections import defaultdict
    sym_exch: dict[str, set] = defaultdict(set)
    for row in all_rows:
        sym_exch[row["symbol"]].add(row["exchange"])
    cross = {sym for sym, exs in sym_exch.items() if len(exs) >= 2}
    for row in all_rows:
        row["cross_listed"] = row["symbol"] in cross

    _sanity_check_prices(all_rows)
    # Snapshot counters so the web role (different process) can read them.
    if _anomaly_counters:
        _write_file_cache("price_anomalies.json", {
            "ts": int(time.time()),
            "total": sum(_anomaly_counters.values()),
            "by_exchange": dict(_anomaly_counters),
        })

    all_rows.sort(key=lambda r: abs(r["apr"] or 0), reverse=True)

    out = {
        "ts": int(time.time()),
        "exchanges": enabled_ex,
        "rows": all_rows,
    }
    # Write to file cache so other workers can read without refetching
    await _write_file_cache_async("funding.json", out)
    return out


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
    "paradex":     0.0003,  # taker fee per Paradex public fee schedule
}
_DEFAULT_FEE = 0.0006


def _fee(exchange: str) -> float:
    return EXCHANGE_FEES.get(exchange, _DEFAULT_FEE)


_arb_result_cache: dict = {"data": None, "ts": 0.0}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(v) if v else default
    except (TypeError, ValueError):
        return default


_ARB_CACHE_TTL = _env_float("AVALANT_ARB_CACHE_TTL", 0.7)
# match the refresh loop cadence; just a lower bound for the web-side fast path


# Hysteresis state — a (symbol, long_ex, short_ex) opp must be seen for at
# least OPP_MIN_LIFETIME_S before we expose it to the UI. Kills phantom
# rows that flash in for one cycle because a single venue's markPrice
# went stale (e.g. KuCoin RAVE at $0.64 while every other venue prices
# it at $1.25 → huge spread, net > 0 → flicker).
#   now_ts = time seen currently
#   first_seen = earliest ts the key appeared with positive net
# Purged when a key is missing for OPP_PURGE_AFTER_S.
_opp_first_seen: dict[tuple[str, str, str], float] = {}
_opp_last_seen: dict[tuple[str, str, str], float] = {}
OPP_MIN_LIFETIME_S = 3.0
OPP_PURGE_AFTER_S = 30.0

# Ticker-collision guard — if price_spread exceeds this threshold we
# cross-check the two venues' contract addresses via the token registry.
# Mismatch or unknown → drop (prevents ASTEROID-style phantom opps where
# two exchanges list DIFFERENT tokens under the same ticker).
HIGH_SPREAD_THRESHOLD = 1.00   # 100% — only the most extreme spreads
                                # trigger the contract-address verify step


def _compute_arb_sync(rows: list[dict], ts: float, *, exclude: set[str] | None = None) -> dict:
    """CPU-heavy O(n²) arb computation — runs in a thread so the event loop stays free.
    Returns every cross-exchange spread (positive AND negative net), sorted by
    net_profit descending; the frontend colours negative net red. Ticker-
    collision rows (>30% price_spread with proven contract mismatch) are still
    dropped. In/Out percentages come from the live orderbook cache when
    available, else are None.

    Filters (applied in order):
      1. Exclude exchanges listed in admin_settings.arb_exclude_exchanges —
         caller may pass `exclude` explicitly (subprocess path has no DB pool).
      2. Volume filter was already applied at the data layer by
         get_funding_data — both legs here already clear min_volume_usd.
      3. interval_h missing on either leg → skip (APR can't be normalised).
      4. net ≤ 0 → skip.
      5. Hysteresis — opp visible for < OPP_MIN_LIFETIME_S → hide
         (stabilises phantom rows from stale markPrice).

    Perf-tuned: per-symbol per-exchange lookups (top_levels, rate normalisation,
    fee) are precomputed once before the inner O(N²) permutation loop so the
    hot path is mostly dict reads and arithmetic. For ~4500 rows / ~3000 pairs
    this drops compute from ~1-2s to ~200-400ms.
    """
    if exclude is None:
        from backend.services import admin_settings
        exclude = admin_settings.get_arb_exclude_exchanges()

    # Orderbook lookup REMOVED from screener compute path — In/Out columns
    # were dropped per user policy (basis-only display). The expensive
    # _load_books_snapshot() + top_levels() calls were the ~80 % cost of
    # this function; with them gone the cycle is mostly arithmetic over
    # already-cached funding rows.
    by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        if r["exchange"] in exclude:
            continue
        # Early filter rows missing interval_h — saves an inner-loop check.
        if not r.get("interval_h"):
            continue
        by_symbol.setdefault(r["symbol"], []).append(r)

    opportunities: list[dict] = []
    for symbol, entries in by_symbol.items():
        n_entries = len(entries)
        if n_entries < 2:
            continue

        # Precompute per-entry derived values (no orderbook lookup — In/Out
        # is no longer displayed). Each entry's normalised rate / fee / mark
        # is used up to 2*(N-1) times in the inner loop, so caching helps.
        per_entry = []
        for e in entries:
            ex = e["exchange"]
            ivl = e["interval_h"]
            rate_norm = e["rate"] * (8.0 / ivl)
            fee = _fee(ex)
            mark_price = float(e.get("price") or 0)
            per_entry.append({
                "e": e, "ex": ex, "ivl": ivl,
                "rate_norm": rate_norm, "fee": fee,
                "mark": mark_price,
            })

        for i in range(n_entries):
            long_pe = per_entry[i]
            long_e = long_pe["e"]
            rate_l = long_pe["rate_norm"]
            fee_l = long_pe["fee"]
            mark_l = long_pe["mark"]
            for j in range(n_entries):
                if i == j:
                    continue
                short_pe = per_entry[j]
                short_e = short_pe["e"]
                rate_s = short_pe["rate_norm"]
                fee_s = short_pe["fee"]
                mark_s = short_pe["mark"]

                gross = rate_s - rate_l
                total_fees = 2.0 * (fee_l + fee_s)

                # Mark-based basis only — no orderbook reads. In/Out display
                # was dropped from the screener, basis is now the single
                # spread metric (matches the live spread on /arb detail).
                if mark_l <= 0 or mark_s <= 0:
                    continue
                p_l = mark_l
                p_s = mark_s
                price_spread = (p_s - p_l) / p_l

                net = gross + price_spread - total_fees
                # No net>0 filter — user wants to see every spread, even the
                # ones where fees eat the funding carry. Frontend colours
                # negative net red so the filter is visible at a glance.

                # Ticker-collision guard: abnormally large price_spread is
                # almost always a sign that the two venues list different
                # tokens under the same ticker (e.g. "ASTEROID" on Binance
                # is a different asset than "ASTEROID" on Aster). Verify
                # via on-chain contract address before we emit the row.
                # Threshold is ±30% — real funding-arb spreads rarely
                # exceed ~5%, so 30% is well past the noise floor.
                if abs(price_spread) > HIGH_SPREAD_THRESHOLD:
                    try:
                        from backend.services.token_registry import validate_pair_identity
                        ok = validate_pair_identity(
                            symbol, long_e["exchange"], short_e["exchange"],
                        )
                    except Exception:
                        ok = None
                    if ok is False:
                        # Explicit contract mismatch — drop.
                        continue
                    # ok is True  → verified, emit.
                    # ok is None → unknown (one/both venues not in registry
                    # — e.g. MEXC, Bybit, OKX, BingX, Aster). User policy:
                    # show the row anyway. The 30% threshold only filters
                    # out PROVEN ticker collisions; genuinely-unknown pairs
                    # stay visible.

                # Hysteresis: first time we see this opp, stamp first_seen
                # and skip. Subsequent cycles include it once the
                # stability window has elapsed. Keeps phantom spreads
                # (stale markPrice on one venue) from flashing in the UI.
                key = (symbol, long_e["exchange"], short_e["exchange"])
                now_ts = ts
                first = _opp_first_seen.get(key)
                if first is None:
                    _opp_first_seen[key] = now_ts
                    _opp_last_seen[key] = now_ts
                    continue
                _opp_last_seen[key] = now_ts
                if now_ts - first < OPP_MIN_LIFETIME_S:
                    continue

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
                    "long_interval_h":  long_e.get("interval_h"),
                    "short_interval_h": short_e.get("interval_h"),
                })

    opportunities.sort(key=lambda x: x["net_profit"], reverse=True)

    # Purge hysteresis entries that haven't been observed in a while —
    # keeps the dict bounded even when symbols rotate out of the feed.
    purge_cutoff = ts - OPP_PURGE_AFTER_S
    stale = [k for k, t in _opp_last_seen.items() if t < purge_cutoff]
    for k in stale:
        _opp_first_seen.pop(k, None)
        _opp_last_seen.pop(k, None)

    return {
        "ts": ts,
        "exchanges": list(FETCHERS.keys()),
        "fees": {ex: round(f * 100, 4) for ex, f in EXCHANGE_FEES.items()},
        "opportunities": opportunities,
    }


async def get_arbitrage_opportunities(force: bool = False) -> dict:
    now = time.time()
    if not force and _arb_result_cache["data"] and now - _arb_result_cache["ts"] < _ARB_CACHE_TTL:
        return _arb_result_cache["data"]

    # File cache fallback (written by broadcaster worker) — skipped on forced
    # recompute so the refresh loop always writes fresh data.
    is_web = os.environ.get("AVALANT_ROLE", "").lower() == "web"
    if not force:
        # Web role has no data plane — read with a longer staleness budget
        # and never compute locally.
        max_age = 120.0 if is_web else _ARB_CACHE_TTL * 3
        fc = _read_file_cache("arbitrage.json", max_age=max_age)
        if fc and fc.get("opportunities"):
            _arb_result_cache["data"] = fc
            _arb_result_cache["ts"] = now
            return fc
        if is_web:
            # Fetcher hasn't written yet — return an empty shell rather than
            # running a heavy compute on the web worker.
            return {"ts": int(now), "exchanges": list(FETCHERS.keys()),
                    "fees": {ex: round(f * 100, 4) for ex, f in EXCHANGE_FEES.items()},
                    "opportunities": []}

    data = await get_funding_data()
    # Run CPU-heavy computation in a thread pool so the event loop stays
    # responsive for HTTP/WS during the 1-2s crunch.
    import asyncio
    result = await asyncio.to_thread(_compute_arb_sync, data["rows"], data["ts"])
    _arb_result_cache["data"] = result
    _arb_result_cache["ts"] = time.time()
    _write_file_cache("arbitrage.json", _slim_arb_for_file(result))
    return result


# Cap the file-cache copy to the top-N opportunities to shrink disk / tmpfs
# churn. Non-owner workers still get everything they need via in-memory
# _arb_result_cache once broadcast delivers. At 4s recompute cadence this
# halves bytes written per hour (~1.5MB × 900 → ~300KB × 900).
_ARB_FILE_TOP_N = 500


def _slim_arb_for_file(result: dict) -> dict:
    opps = result.get("opportunities") or []
    if len(opps) <= _ARB_FILE_TOP_N:
        return result
    return {
        **result,
        "opportunities": opps[:_ARB_FILE_TOP_N],
        "truncated_to": _ARB_FILE_TOP_N,
        "full_count": len(opps),
    }


def get_exchange_health() -> dict[str, dict]:
    """Per-exchange freshness snapshot for the UI.

    An exchange is healthy if **either** its WS stream is delivering fresh
    rows **or** its REST/merged cache is. Reporting only-WS leads to false
    "stale" badges for venues like Bybit/OKX/KuCoin whose WS stream can
    stall (keepalive timeout, rate limits) while REST is still current —
    the arbitrage engine is using the REST data correctly in the
    background, so the UI must reflect that reality.

    Returns {exchange → {age_s, healthy, via, row_count, ws_row_count,
                         rest_row_count}} where:
      · age_s: seconds since the freshest source for this exchange updated
      · via: "ws" if WS is the live source, "rest" if we're on REST
             fallback, "none" if neither is fresh
      · healthy: True iff we have a fresh source (age ≤ 5s WS / 15s REST)
                 AND some rows.
    """
    result: dict[str, dict] = {}
    ws_info: dict[str, dict] = {}
    try:
        from backend.services.funding_ws import ws_health, is_ws_funding_supported
        ws_info = ws_health() or {}
    except Exception:
        is_ws_funding_supported = lambda _: False

    # Orderbook freshness per exchange (book-cache lives in fetcher for WS
    # and in books.json file for web). This lets the UI show a second dot
    # for "orderbook live?" independent of funding-rate freshness — users
    # were seeing green funding dots while pairs disappeared from the arb
    # grid because KuCoin/Bybit orderbook WS had dropped.
    ob_freshness: dict[str, dict] = {}
    try:
        from backend.services.orderbook_cache import freshness_by_exchange
        ob_freshness = freshness_by_exchange() or {}
    except Exception:
        pass

    is_web = os.environ.get("AVALANT_ROLE", "").lower() == "web"
    # On web: use the shared files (fetcher writes them).
    # On fetcher/monolith: use in-memory state.
    ws_dump: dict | None = None
    rest_counts: dict[str, int] = {}
    rest_age_s: float | None = None
    merged_ts_by_ex: dict[str, float] = {}
    if is_web:
        ws_dump = _read_file_cache("funding_ws.json", max_age=_ARB_CACHE_TTL * 10) or {}
        merged = _read_file_cache("funding.json", max_age=120.0) or {}
        if merged.get("rows"):
            from collections import Counter
            rest_counts = Counter(r["exchange"] for r in merged["rows"])
        if merged.get("ts"):
            rest_age_s = max(0.0, time.time() - merged["ts"])
        # Per-exchange wall-clock timestamps stamped by the refresh-loop
        # heartbeat — these track WS push delivery, not the (much rarer)
        # successful REST gather. Web role has no in-memory _cache so this
        # is the only per-venue freshness signal it can see.
        merged_ts_by_ex = merged.get("ts_by_ex") or {}

    now_m = _mono()
    now_t = time.time()
    WS_FRESH = 5.0
    REST_FRESH = 15.0
    for ex in FETCHERS:
        ws_supported = is_ws_funding_supported(ex)

        # ── WS side ──
        ws_row_count = 0
        ws_age: float | None = None
        if is_web:
            if ws_dump is not None:
                rows = (ws_dump.get("rows") or {}).get(ex) or []
                ws_row_count = len(rows)
                ts_by_ex = ws_dump.get("ts_by_ex") or {}
                per_ex_ts = ts_by_ex.get(ex)
                if per_ex_ts:
                    ws_age = max(0.0, now_t - per_ex_ts)
                elif ws_dump.get("ts"):
                    ws_age = max(0.0, now_t - ws_dump["ts"])
        else:
            # On fetcher/monolith: _cache holds rows from EITHER source
            # (WS writes via _get_rows). The ws_info from manager tells us
            # specifically about the WS adapter's health.
            if ws_supported:
                h = ws_info.get(ex) or {}
                ws_age = h.get("last_age_s")
                if h.get("healthy"):
                    ws_row_count = h.get("symbols") or 0

        # ── REST/merged side ──
        # On fetcher/monolith, _cache is the merged source of truth for
        # the screener endpoints — use it. On web, prefer the per-exchange
        # ts stamped by the refresh-loop heartbeat (tracks WS push), and
        # only fall back to the merged-file ts if it's missing.
        if is_web:
            rest_row_count = rest_counts.get(ex, 0)
            per_ex_t = merged_ts_by_ex.get(ex)
            if per_ex_t:
                rest_age = max(0.0, time.time() - per_ex_t)
            else:
                rest_age = rest_age_s
        else:
            cached_rows, cached_at = _cache.get(ex, ([], 0.0))
            rest_row_count = len(cached_rows)
            rest_age = (now_m - cached_at) if cached_at else None

        # ── Pick the freshest healthy source ──
        ws_fresh = ws_row_count > 0 and ws_age is not None and ws_age <= WS_FRESH
        rest_fresh = rest_row_count > 0 and rest_age is not None and rest_age <= REST_FRESH

        if ws_fresh:
            via, healthy, age_s = "ws", True, ws_age
        elif rest_fresh:
            via, healthy, age_s = "rest", True, rest_age
        else:
            via, healthy = "none", False
            # Report the freshest known age for diagnostics even when stale.
            candidates = [a for a in (ws_age, rest_age) if a is not None]
            age_s = min(candidates) if candidates else None

        entry = {
            "age_s": round(age_s, 2) if age_s is not None else None,
            "healthy": healthy,
            "via": via,
            # Keep row_count for backwards-compat (consumers relied on it);
            # it mirrors whichever source we picked.
            "row_count": ws_row_count if via == "ws" else rest_row_count,
            "ws_row_count": ws_row_count,
            "rest_row_count": rest_row_count,
            "ws_age_s": round(ws_age, 2) if ws_age is not None else None,
            "rest_age_s": round(rest_age, 2) if rest_age is not None else None,
        }
        if ws_supported:
            h = ws_info.get(ex) or {}
            # On web role ws_info is empty (manager runs in fetcher);
            # fall back to "do we have fresh WS rows?" which is the same
            # signal the frontend actually cares about.
            entry["ws_connected"] = (
                bool(h.get("connected"))
                if h
                else (ws_age is not None and ws_age < 15.0 and ws_row_count > 0)
            )
        ob = ob_freshness.get(ex) or {}
        entry["orderbook_min_age_s"] = ob.get("min_age_s")
        entry["orderbook_avg_age_s"] = ob.get("avg_age_s")
        entry["orderbook_median_age_s"] = ob.get("median_age_s")
        entry["orderbook_p90_age_s"] = ob.get("p90_age_s")
        entry["orderbook_max_age_s"] = ob.get("max_age_s")
        entry["orderbook_fresh"] = ob.get("fresh") or 0
        entry["orderbook_degraded"] = ob.get("degraded") or 0
        entry["orderbook_stale"] = ob.get("stale") or 0
        entry["orderbook_total"] = ob.get("total") or 0
        entry["orderbook_healthy"] = bool(ob.get("healthy"))
        result[ex] = entry
        # Feed the rolling-window stats so /api/admin/freshness-stats
        # can show averages without doing its own polling.
        try:
            from backend.services import freshness_stats as _fs
            _fs.record(ex, age_s)
        except Exception:
            pass
    return result


def get_cached_rates() -> dict[str, dict]:
    """Return flat dict {exchange:symbol → {rate, interval_h, price}} from current cache.
    Used by the alert service and the /screener/pair fallback to look up an
    arbitrary (exchange, symbol) without triggering a fresh fetch.

    On web workers the in-process `_cache` is empty (only the fetcher writes
    to it). Falls back to the shared `funding.json` dump so /arb pair
    lookups for symbols outside the top-500 opp list still resolve — without
    this, /arb showed all dashes for any non-popular pair on web role.
    """
    from backend.services import admin_settings
    disabled_ex = admin_settings.get_disabled_exchanges()
    hidden_sym = admin_settings.get_hidden_symbols()
    result: dict[str, dict] = {}

    if _cache:
        for exchange, (rows, _) in _cache.items():
            if exchange in disabled_ex:
                continue
            for row in rows:
                sym = row["symbol"]
                if sym in hidden_sym:
                    continue
                ivl = row.get("interval_h")
                if ivl is None:
                    continue
                result[f"{exchange}:{sym}"] = {
                    "rate":       row.get("rate", 0.0),
                    "interval_h": ivl,
                    "price":      row.get("price", 0.0),
                }
        return result

    # Web-role fallback: in-process cache empty (no fetcher in this process),
    # read the shared funding.json dump written by the fetcher every ~2 s.
    cached = _read_file_cache("funding.json", max_age=120.0)
    if not cached or not cached.get("rows"):
        return result
    for row in cached["rows"]:
        sym = row.get("symbol")
        ex = row.get("exchange")
        if not sym or not ex:
            continue
        if ex in disabled_ex or sym in hidden_sym:
            continue
        ivl = row.get("interval_h")
        if ivl is None:
            continue
        result[f"{ex}:{sym}"] = {
            "rate":       row.get("rate", 0.0),
            "interval_h": ivl,
            "price":      row.get("price", 0.0),
        }
    return result
