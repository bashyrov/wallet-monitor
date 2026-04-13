"""
Funding rate screener — fetches perpetual futures funding rates from multiple
exchanges using public (no-auth) endpoints. Per-exchange cache with 30s TTL.
"""
import asyncio
import logging
import time

import httpx

logger = logging.getLogger("avalant.screener")

_http = httpx.AsyncClient(
    timeout=10,
    headers={"User-Agent": "Mozilla/5.0"},
    follow_redirects=True,
)

# Per-exchange cache: {exchange: (rows, monotonic_fetched_at)}
_cache: dict[str, tuple[list, float]] = {}
CACHE_TTL = 30.0  # seconds


def _mono() -> float:
    return time.monotonic()


# ── Binance Futures ────────────────────────────────────────────────────────────
async def _fetch_binance() -> list[dict]:
    r = await _http.get("https://fapi.binance.com/fapi/v1/premiumIndex")
    r.raise_for_status()
    out = []
    for item in r.json():
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        token = sym[:-4]
        rate = float(item.get("lastFundingRate") or 0)
        next_ms = int(item.get("nextFundingTime") or 0)
        price = float(item.get("markPrice") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "binance",
            "price": price,
            "rate": rate,           # per 8h
            "next_ts": next_ms // 1000,
            "interval_h": 8,
        })
    return out


# ── Bybit Linear ───────────────────────────────────────────────────────────────
async def _fetch_bybit() -> list[dict]:
    r = await _http.get("https://api.bybit.com/v5/market/tickers?category=linear")
    r.raise_for_status()
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
        price = float(item.get("markPrice") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "bybit",
            "price": price,
            "rate": rate,           # per funding interval (usually 8h)
            "next_ts": next_ms // 1000,
            "interval_h": 8,
        })
    return out


# ── Gate.io Futures (USDT-settled) ─────────────────────────────────────────────
async def _fetch_gate() -> list[dict]:
    r = await _http.get("https://api.gateio.ws/api/v4/futures/usdt/contracts")
    r.raise_for_status()
    now = int(time.time())
    out = []
    for item in r.json():
        name = item.get("name", "")   # e.g. "BTC_USDT"
        if not name.endswith("_USDT"):
            continue
        token = name[:-5]
        rate = float(item.get("funding_rate") or 0)
        last_apply = int(item.get("funding_next_apply") or 0)
        interval = int(item.get("funding_interval") or 28800)  # seconds, default 8h
        # funding_next_apply is the last applied timestamp; add interval to get real next
        next_ts = last_apply + interval if last_apply else 0
        # If still in the past (clock skew / stale), advance by another interval
        while next_ts and next_ts < now:
            next_ts += interval
        price = float(item.get("mark_price") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "gate",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": round(interval / 3600, 2),
        })
    return out


# ── KuCoin Futures ─────────────────────────────────────────────────────────────
async def _fetch_kucoin() -> list[dict]:
    r = await _http.get("https://api-futures.kucoin.com/api/v1/contracts/active")
    r.raise_for_status()
    out = []
    for item in (r.json().get("data") or []):
        sym = item.get("symbol", "")   # e.g. "XBTUSDTM"
        if not sym.endswith("USDTM"):
            continue
        token = sym[:-5]               # strip "USDTM"
        if token == "XBT":
            token = "BTC"
        rate = float(item.get("fundingFeeRate") or 0)
        # nextFundingRateTime = milliseconds UNTIL next funding (relative, not absolute)
        ms_until = int(item.get("nextFundingRateTime") or 0)
        next_ts = int(time.time()) + ms_until // 1000 if ms_until else 0
        price = float(item.get("indexPrice") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "kucoin",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": 8,
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
    # Hyperliquid funds every hour; ctx.funding = hourly rate
    now_ts = int(time.time())
    next_ts = (now_ts // 3600 + 1) * 3600   # next full hour (UTC)
    out = []
    for asset_meta, ctx in zip(universe, ctxs):
        token = asset_meta.get("name", "")
        rate_1h = float(ctx.get("funding") or 0)
        price = float(ctx.get("markPx") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "hyperliquid",
            "price": price,
            "rate": rate_1h,        # per 1h (interval_h = 1)
            "next_ts": next_ts,
            "interval_h": 1,
        })
    return out


# ── OKX Linear SWAP ───────────────────────────────────────────────────────────
async def _fetch_okx() -> list[dict]:
    instr_r, tick_r = await asyncio.gather(
        _http.get("https://www.okx.com/api/v5/public/instruments?instType=SWAP"),
        _http.get("https://www.okx.com/api/v5/market/tickers?instType=SWAP"),
    )
    instr_r.raise_for_status()
    tick_r.raise_for_status()

    # USDT-settled linear SWAPs only
    inst_ids = [
        i["instId"] for i in instr_r.json().get("data", [])
        if i.get("settleCcy") == "USDT"
    ]
    # last price map
    price_map: dict[str, float] = {
        t["instId"]: float(t.get("last") or 0)
        for t in tick_r.json().get("data", [])
    }

    # Fetch funding rates concurrently (public, no auth)
    sem = asyncio.Semaphore(50)

    async def _one(inst_id: str) -> dict | None:
        async with sem:
            try:
                r = await _http.get(
                    f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}"
                )
                if r.status_code != 200:
                    return None
                d = (r.json().get("data") or [{}])[0]
                rate = float(d.get("fundingRate") or 0)
                next_ms = int(d.get("nextFundingTime") or 0)
                price = price_map.get(inst_id, 0)
                if price == 0:
                    return None
                token = inst_id.replace("-USDT-SWAP", "")
                return {
                    "symbol": token,
                    "exchange": "okx",
                    "price": price,
                    "rate": rate,
                    "next_ts": next_ms // 1000 if next_ms else 0,
                    "interval_h": 8,
                }
            except Exception:
                return None

    rows = await asyncio.gather(*[_one(i) for i in inst_ids])
    return [r for r in rows if r]


# ── MEXC Futures ───────────────────────────────────────────────────────────────
async def _fetch_mexc() -> list[dict]:
    tick_r, fr_r = await asyncio.gather(
        _http.get("https://contract.mexc.com/api/v1/contract/ticker"),
        _http.get("https://contract.mexc.com/api/v1/contract/funding_rate/BTC_USDT"),
    )
    tick_r.raise_for_status()
    fr_r.raise_for_status()

    fr_data = fr_r.json().get("data", {})
    next_ts = int(fr_data.get("nextSettleTime") or 0) // 1000
    interval_h = int(fr_data.get("collectCycle") or 8)

    out = []
    for item in (tick_r.json().get("data") or []):
        sym = item.get("symbol", "")
        if not sym.endswith("_USDT"):
            continue
        token = sym[:-5]
        rate = float(item.get("fundingRate") or 0)
        price = float(item.get("fairPrice") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "mexc",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": interval_h,
        })
    return out


# ── Bitget USDT Futures ────────────────────────────────────────────────────────
async def _fetch_bitget() -> list[dict]:
    tick_r, fr_r = await asyncio.gather(
        _http.get("https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"),
        _http.get("https://api.bitget.com/api/v2/mix/market/current-fund-rate?symbol=BTCUSDT&productType=USDT-FUTURES"),
    )
    tick_r.raise_for_status()
    fr_r.raise_for_status()

    fr_data = ((fr_r.json().get("data") or [{}])[0])
    next_ts = int(fr_data.get("nextUpdate") or 0) // 1000
    interval_h = int(fr_data.get("fundingRateInterval") or 8)

    out = []
    for item in (tick_r.json().get("data") or []):
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        token = sym[:-4]
        rate = float(item.get("fundingRate") or 0)
        price = float(item.get("markPrice") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "bitget",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": interval_h,
        })
    return out


# ── Aster DEX (Binance Futures-compatible API) ─────────────────────────────────
async def _fetch_aster() -> list[dict]:
    r = await _http.get("https://fapi.asterdex.com/fapi/v1/premiumIndex")
    r.raise_for_status()
    out = []
    for item in r.json():
        sym = item.get("symbol", "")
        # Aster uses "GNSUSD", "BTCUSDT" — keep USDT, skip pure USD perpetuals
        if sym.endswith("USDT"):
            token = sym[:-4]
        elif sym.endswith("USD"):
            token = sym[:-3]
        else:
            continue
        rate = float(item.get("lastFundingRate") or 0)
        next_ms = int(item.get("nextFundingTime") or 0)
        price = float(item.get("markPrice") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "aster",
            "price": price,
            "rate": rate,
            "next_ts": next_ms // 1000,
            "interval_h": 8,
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
        products = await client.list_products()
        ids = [p.id for p in products]
        prices = await client.list_market_prices(product_ids=ids)
        price_map = {str(p.product_id): float(p.oracle_price) for p in prices}

        now = int(time.time())
        next_ts = (now // 3600 + 1) * 3600   # next full hour boundary

        out = []
        for p in products:
            if str(p.status) != "Status1.active":
                continue
            token = p.base_token_name
            rate_1h = float(p.funding_rate1h or 0)
            price = price_map.get(str(p.id), 0)
            if price == 0:
                continue
            out.append({
                "symbol": token,
                "exchange": "ethereal",
                "price": price,
                "rate": rate_1h,
                "next_ts": next_ts,
                "interval_h": 1,
            })
        return out
    finally:
        await client.close()


# ── Dispatcher ─────────────────────────────────────────────────────────────────
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
        return cached_rows   # serve stale on error


async def get_funding_data() -> dict:
    """
    Returns:
      {
        "ts": <unix seconds>,
        "exchanges": ["binance", ...],
        "rows": [
          {
            "symbol": "BTC",
            "exchange": "binance",
            "price": 103500.0,
            "rate": 0.0001,       # per interval
            "next_ts": 1234567890,
            "interval_h": 8,
            "apr": 10.95          # annualised %
          },
          ...
        ]
      }
    """
    results = await asyncio.gather(
        *(_get_rows(ex) for ex in FETCHERS),
        return_exceptions=True,
    )

    all_rows: list[dict] = []
    for ex, result in zip(FETCHERS.keys(), results):
        if isinstance(result, list):
            for row in result:
                # APR = rate * (8760 / interval_h) * 100
                row["apr"] = round(row["rate"] * (8760 / row["interval_h"]) * 100, 4)
            all_rows.extend(result)

    # Mark tokens that appear on 2+ exchanges (cross-listed futures)
    from collections import defaultdict
    sym_exch: dict[str, set] = defaultdict(set)
    for row in all_rows:
        sym_exch[row["symbol"]].add(row["exchange"])
    cross = {sym for sym, exs in sym_exch.items() if len(exs) >= 2}
    for row in all_rows:
        row["cross_listed"] = row["symbol"] in cross

    # Sort by |APR| descending — highest opportunity first
    all_rows.sort(key=lambda r: abs(r["apr"]), reverse=True)

    return {
        "ts": int(time.time()),
        "exchanges": list(FETCHERS.keys()),
        "rows": all_rows,
    }


# ── Fee config (taker, as fraction) ───────────────────────────────────────────
EXCHANGE_FEES: dict[str, float] = {
    "binance":     0.0004,    # 0.04%
    "bybit":       0.00055,   # 0.055%
    "okx":         0.0005,    # 0.05%
    "gate":        0.0005,    # 0.05%
    "kucoin":      0.0006,    # 0.06%
    "mexc":        0.0002,    # 0.02%
    "bitget":      0.0006,    # 0.06%
    "hyperliquid": 0.00035,   # 0.035%
    "aster":       0.0005,    # 0.05%
    "ethereal":    0.0003,    # 0.03% (takerFee from API)
}
_DEFAULT_FEE = 0.0006  # fallback if exchange not in map


def _fee(exchange: str) -> float:
    return EXCHANGE_FEES.get(exchange, _DEFAULT_FEE)


async def get_arbitrage_opportunities() -> dict:
    """
    Find cross-exchange funding arbitrage opportunities.

    Strategy: Long (more negative funding) + Short (less negative / positive funding).
    gross_funding = funding_short_8h - funding_long_8h
    price_spread  = (price_short - price_long) / price_long
    total_fees    = 2 * (fee_long + fee_short)   # open + close both legs
    net_profit    = gross_funding + price_spread - total_fees
    """
    data = await get_funding_data()
    rows = data["rows"]

    # Group by symbol; keep only USDT-denominated (no _PERP suffix noise)
    by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        sym = r["symbol"]
        by_symbol.setdefault(sym, []).append(r)

    opportunities: list[dict] = []
    for symbol, entries in by_symbol.items():
        if len(entries) < 2:
            continue

        # For each ordered pair (long_ex, short_ex)
        for i in range(len(entries)):
            for j in range(len(entries)):
                if i == j:
                    continue
                long_e = entries[i]
                short_e = entries[j]

                # Normalise rates to per-8h
                rate_l = long_e["rate"] * (8.0 / long_e["interval_h"])
                rate_s = short_e["rate"] * (8.0 / short_e["interval_h"])

                gross = rate_s - rate_l  # must be > 0 for viable trade
                if gross <= 0:
                    continue

                fee_l = _fee(long_e["exchange"])
                fee_s = _fee(short_e["exchange"])
                total_fees = 2.0 * (fee_l + fee_s)  # round-trip both legs

                p_l = long_e["price"]
                p_s = short_e["price"]
                price_spread = (p_s - p_l) / p_l if p_l > 0 else 0.0

                net = gross + price_spread - total_fees
                gross_apr = round(gross * (8760 / 8) * 100, 4)
                net_apr = round(net * (8760 / 8) * 100, 4)

                opportunities.append({
                    "symbol": symbol,
                    "long_exchange":  long_e["exchange"],
                    "short_exchange": short_e["exchange"],
                    "long_rate":      round(rate_l * 100, 6),   # % per 8h
                    "short_rate":     round(rate_s * 100, 6),
                    "long_price":     p_l,
                    "short_price":    p_s,
                    "gross_funding":  round(gross * 100, 6),     # %
                    "price_spread":   round(price_spread * 100, 4),  # %
                    "fee_long":       round(fee_l * 100, 4),
                    "fee_short":      round(fee_s * 100, 4),
                    "total_fees":     round(total_fees * 100, 4),
                    "net_profit":     round(net * 100, 6),        # %
                    "gross_apr":      gross_apr,
                    "net_apr":        net_apr,
                    "valid_price":    p_l <= p_s,
                    "next_ts_long":   long_e.get("next_ts", 0),
                    "next_ts_short":  short_e.get("next_ts", 0),
                })

    # Sort by net_profit descending
    opportunities.sort(key=lambda x: x["net_profit"], reverse=True)

    return {
        "ts": data["ts"],
        "exchanges": list(FETCHERS.keys()),
        "fees": {ex: round(f * 100, 4) for ex, f in EXCHANGE_FEES.items()},
        "opportunities": opportunities,
    }
