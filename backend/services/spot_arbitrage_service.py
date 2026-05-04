"""Spot-vs-Perp cash-and-carry arbitrage.

Logic:
  - Buy spot on exchange A (spot market, no leverage/funding).
  - Short perp on exchange B (perpetual futures, pays/receives funding).
Earn:
  - Short funding flips sign: if perp funding is NEGATIVE, shorts RECEIVE payments.
  - Basis: (perp_price - spot_price) / spot_price. Positive = perp trades above spot.
Cost:
  - Spot taker fee round-trip (open + close).
  - Perp taker fee round-trip.

Freshness: 6s cache per exchange, matching the futures cache.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

from . import arbitrage_service as _arb

logger = logging.getLogger("avalant.spot_arb")

# Dedicated client — shares no pool with the futures arb loop so concurrent
# refreshes don't starve each other.
_http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0),
    headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=64, max_keepalive_connections=16, keepalive_expiry=30),
    http2=False,
)

_spot_cache: dict[str, tuple[list[dict], float]] = {}
SPOT_CACHE_TTL = 6.0


# ── Spot fee config (taker, as fraction) ──────────────────────────────────────
SPOT_FEES: dict[str, float] = {
    "binance":  0.001,
    "bybit":    0.001,
    "okx":      0.001,
    "gate":     0.001,
    "kucoin":   0.001,
    "mexc":     0.0,
    "bitget":   0.001,
    "bingx":    0.001,
}
_DEFAULT_SPOT_FEE = 0.001


def _spot_fee(exchange: str) -> float:
    return SPOT_FEES.get(exchange, _DEFAULT_SPOT_FEE)


# ── Per-exchange spot fetchers ────────────────────────────────────────────────

# /api/v3/ticker/24hr keeps returning delisted/halted symbols (e.g. NTRN
# stayed in the feed with status=BREAK long after spot trading was paused),
# so we cross-check every ticker against /api/v3/exchangeInfo's
# status=="TRADING" set. exchangeInfo changes a few times per day; cache it
# for 10 min so the spot fetch stays cheap.
_binance_trading_cache: tuple[set[str], float] = (set(), 0.0)
_BINANCE_INFO_TTL = 600.0


async def _binance_trading_set() -> set[str]:
    global _binance_trading_cache
    syms, ts = _binance_trading_cache
    if syms and (time.time() - ts) < _BINANCE_INFO_TTL:
        return syms
    try:
        r = await _http.get("https://api.binance.com/api/v3/exchangeInfo")
        if r.status_code != 200:
            return syms  # fall back to whatever we cached last
        fresh = {
            s["symbol"]
            for s in (r.json().get("symbols") or [])
            if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed")
        }
        if fresh:
            _binance_trading_cache = (fresh, time.time())
            return fresh
    except Exception as exc:
        logger.debug("binance exchangeInfo fetch failed: %s", exc)
    return syms


async def _fetch_binance_spot() -> list[dict]:
    r = await _http.get("https://api.binance.com/api/v3/ticker/24hr")
    if r.status_code != 200:
        return []
    trading = await _binance_trading_set()
    out: list[dict] = []
    for x in r.json():
        s = x.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        # Drop delisted / halted symbols. If the trading-set fetch failed and
        # we have no cached snapshot yet, fall back to the raw ticker list so
        # the feed doesn't go empty during a Binance API hiccup.
        if trading and s not in trading:
            continue
        try:
            price = float(x.get("lastPrice") or 0)
            vol = float(x.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-4], "price": price, "volume_usd": vol})
    return out


async def _fetch_bybit_spot() -> list[dict]:
    r = await _http.get("https://api.bybit.com/v5/market/tickers", params={"category": "spot"})
    if r.status_code != 200:
        return []
    j = r.json()
    out: list[dict] = []
    for x in (j.get("result", {}).get("list") or []):
        s = x.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        try:
            price = float(x.get("lastPrice") or 0)
            vol = float(x.get("turnover24h") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-4], "price": price, "volume_usd": vol})
    return out


async def _fetch_okx_spot() -> list[dict]:
    r = await _http.get("https://www.okx.com/api/v5/market/tickers", params={"instType": "SPOT"})
    if r.status_code != 200:
        return []
    j = r.json()
    out: list[dict] = []
    for x in (j.get("data") or []):
        s = x.get("instId", "")  # "BTC-USDT"
        if not s.endswith("-USDT"):
            continue
        try:
            price = float(x.get("last") or 0)
            vol_ccy = float(x.get("volCcy24h") or 0)  # quoted in base ccy — convert via price
        except (TypeError, ValueError):
            continue
        vol = vol_ccy * price  # volCcy24h is in base asset; multiply by price → USDT
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-5], "price": price, "volume_usd": vol})
    return out


async def _fetch_gate_spot() -> list[dict]:
    r = await _http.get("https://api.gateio.ws/api/v4/spot/tickers")
    if r.status_code != 200:
        return []
    out: list[dict] = []
    for x in r.json():
        s = x.get("currency_pair", "")
        if not s.endswith("_USDT"):
            continue
        try:
            price = float(x.get("last") or 0)
            vol = float(x.get("quote_volume") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-5], "price": price, "volume_usd": vol})
    return out


async def _fetch_kucoin_spot() -> list[dict]:
    r = await _http.get("https://api.kucoin.com/api/v1/market/allTickers")
    if r.status_code != 200:
        return []
    j = r.json()
    out: list[dict] = []
    for x in (j.get("data", {}).get("ticker") or []):
        s = x.get("symbol", "")
        if not s.endswith("-USDT"):
            continue
        try:
            price = float(x.get("last") or 0)
            vol = float(x.get("volValue") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-5], "price": price, "volume_usd": vol})
    return out


async def _fetch_mexc_spot() -> list[dict]:
    r = await _http.get("https://api.mexc.com/api/v3/ticker/24hr")
    if r.status_code != 200:
        return []
    out: list[dict] = []
    for x in r.json():
        s = x.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        try:
            price = float(x.get("lastPrice") or 0)
            vol = float(x.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-4], "price": price, "volume_usd": vol})
    return out


async def _fetch_bitget_spot() -> list[dict]:
    r = await _http.get("https://api.bitget.com/api/v2/spot/market/tickers")
    if r.status_code != 200:
        return []
    j = r.json()
    out: list[dict] = []
    for x in (j.get("data") or []):
        s = x.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        try:
            price = float(x.get("lastPr") or 0)
            vol = float(x.get("usdtVolume") or x.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-4], "price": price, "volume_usd": vol})
    return out


async def _fetch_bingx_spot() -> list[dict]:
    r = await _http.get("https://open-api.bingx.com/openApi/spot/v1/ticker/24hr")
    if r.status_code != 200:
        return []
    j = r.json()
    out: list[dict] = []
    for x in (j.get("data") or []):
        s = x.get("symbol", "")
        if not s.endswith("-USDT"):
            continue
        try:
            price = float(x.get("lastPrice") or 0)
            vol = float(x.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-5], "price": price, "volume_usd": vol})
    return out


async def _fetch_htx_spot() -> list[dict]:
    r = await _http.get("https://api.huobi.pro/market/tickers")
    if r.status_code != 200:
        return []
    j = r.json()
    out: list[dict] = []
    for x in (j.get("data") or []):
        s = (x.get("symbol") or "").lower()
        if not s.endswith("usdt"):
            continue
        try:
            price = float(x.get("close") or 0)
            vol = float(x.get("vol") or 0)  # quote volume (USDT) on HTX
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-4].upper(), "price": price, "volume_usd": vol})
    return out


SPOT_FETCHERS = {
    "binance": _fetch_binance_spot,
    "bybit":   _fetch_bybit_spot,
    "okx":     _fetch_okx_spot,
    "gate":    _fetch_gate_spot,
    "kucoin":  _fetch_kucoin_spot,
    "mexc":    _fetch_mexc_spot,
    "bitget":  _fetch_bitget_spot,
    "bingx":   _fetch_bingx_spot,
    "htx":     _fetch_htx_spot,
}

SPOT_EXCHANGES = list(SPOT_FETCHERS.keys())


async def get_spot_rows(exchange: str) -> list[dict]:
    """Cached per-exchange spot tickers with circuit-breaker protection."""
    from backend.services._circuit import circuit
    now = _arb._mono()
    cached = _spot_cache.get(exchange)
    if cached and (now - cached[1]) < SPOT_CACHE_TTL:
        return cached[0]
    # Skip the fetch entirely if this exchange is in cooldown — serve the
    # cached rows (if any) so downstream arb compute still has data.
    if not circuit.allow(f"spot:{exchange}"):
        return cached[0] if cached else []
    fn = SPOT_FETCHERS.get(exchange)
    if not fn:
        return []
    try:
        rows = await asyncio.wait_for(fn(), timeout=15.0)
        circuit.ok(f"spot:{exchange}")
    except Exception as e:
        msg = str(e)
        if "418" in msg or "I'm a teapot" in msg or "Client Error (418)" in msg:
            circuit.hard_fail(f"spot:{exchange}", cooldown_s=180.0)
            logger.warning("spot %s: HTTP 418 — opening circuit 180s", exchange)
        elif "429" in msg or "Too Many Requests" in msg:
            circuit.hard_fail(f"spot:{exchange}", cooldown_s=60.0)
            logger.warning("spot %s: HTTP 429 — opening circuit 60s", exchange)
        else:
            circuit.fail(f"spot:{exchange}")
            logger.warning("spot fetch %s failed: %s", exchange, type(e).__name__)
        rows = cached[0] if cached else []
    _spot_cache[exchange] = (rows, now)
    return rows


async def get_spot_arbitrage_opportunities(min_vol_usd: float = 10_000.0) -> dict:
    """Cross-exchange spot-short cash-and-carry opportunities.

    Returns every spread (positive AND negative gross) sorted by net
    profit descending — the frontend colours negative net red. Only
    obvious ticker collisions (|basis| > 30%) are still dropped.

    Web role reads from the shared file cache that the fetcher writes every
    2 s — same pattern as the futures arbitrage feed.
    """
    if os.environ.get("AVALANT_ROLE", "").lower() == "web":
        # Web NEVER computes — always serves whatever the fetcher wrote.
        cached = _arb._read_file_cache("spot_arbitrage.json", max_age=120.0)
        if cached and isinstance(cached, dict):
            return cached
        # Cold-start: block briefly so the page doesn't flash an empty table
        # when the fetcher is about to land its first write. Up to 500 ms.
        for _ in range(10):
            await asyncio.sleep(0.05)
            cached = _arb._read_file_cache("spot_arbitrage.json", max_age=120.0)
            if cached and isinstance(cached, dict):
                return cached
        return {"opportunities": [], "generated_at": int(time.time()), "spot_exchanges": SPOT_EXCHANGES, "cold": True}

    # Fetch only spot tickers in this loop — perp rows come from the
    # screener's _cache (kept warm by the refresh-loop in the main
    # fetcher loop). Calling _arb._get_rows here used to RuntimeError
    # ("attached to different loop") because _arb._http is bound to
    # the main loop and we run in a worker thread's loop. Reading
    # _cache directly is loop-agnostic and ~100× faster.
    spot_results = await asyncio.gather(
        *[get_spot_rows(ex) for ex in SPOT_EXCHANGES],
        return_exceptions=True,
    )
    spot_map: dict[str, dict[str, dict]] = {}
    for ex, rows in zip(SPOT_EXCHANGES, spot_results):
        if not isinstance(rows, list):
            continue
        for r in rows:
            sym = r["symbol"]
            spot_map.setdefault(sym, {})[ex] = r

    # Read perp rows from the screener cache (in-memory) and the
    # heartbeat file (cross-process, written by the refresh-loop).
    perp_exs = [ex for ex in _arb.FETCHERS.keys() if ex != "lighter"]
    perp_results: list = []
    for ex in perp_exs:
        cached_rows, _ts = _arb._cache.get(ex, ([], 0.0))
        perp_results.append(cached_rows if cached_rows else [])
    if not any(perp_results):
        # Cold-start fallback — read funding.json once.
        try:
            shared = _arb._read_file_cache("funding.json", max_age=120.0) or {}
            by_ex: dict[str, list] = {}
            for r in shared.get("rows", []) or []:
                by_ex.setdefault(r.get("exchange", ""), []).append(r)
            perp_results = [by_ex.get(ex, []) for ex in perp_exs]
        except Exception:
            pass
    perp_map: dict[str, dict[str, dict]] = {}
    for ex, rows in zip(perp_exs, perp_results):
        if not isinstance(rows, list):
            continue
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            perp_map.setdefault(sym, {})[ex] = r

    opps: list[dict] = []
    for sym, spot_by_ex in spot_map.items():
        perp_by_ex = perp_map.get(sym)
        if not perp_by_ex:
            continue
        for spot_ex, sdata in spot_by_ex.items():
            spot_price = sdata.get("price") or 0
            spot_vol = sdata.get("volume_usd") or 0
            if spot_price <= 0 or spot_vol < min_vol_usd:
                continue
            for perp_ex, pdata in perp_by_ex.items():
                perp_price = pdata.get("price") or 0
                perp_vol = pdata.get("volume_usd") or 0
                raw_rate = pdata.get("rate")
                if perp_price <= 0 or raw_rate is None:
                    continue
                try:
                    rate_f = float(raw_rate)
                except (TypeError, ValueError):
                    continue
                if rate_f == 0.0 or perp_vol < min_vol_usd:
                    continue
                interval_h = pdata.get("interval_h") or 8.0
                # Normalize to 8h window
                rate_8h = rate_f * (8.0 / interval_h) * 100  # percent
                # Standard exchange convention (Binance / Bybit / OKX docs):
                #   funding rate POSITIVE → longs pay shorts → short PnL = +rate
                #   funding rate NEGATIVE → shorts pay longs → short PnL = +rate
                # i.e. for a short position the funding-leg PnL just IS the
                # signed rate. Earlier code had `short_funding = -rate_8h`,
                # which flipped the sign and made negative-rate shorts look
                # profitable when in reality we'd be paying funding.
                short_funding = rate_8h
                basis_pct = (perp_price - spot_price) / spot_price * 100
                # Collision guard: 100% catches only the most extreme cases
                # (genuinely-different tokens with 2×+ price gaps). Everything
                # below that gets a look — the token-registry contract check
                # below is what actually rejects verified collisions.
                if abs(basis_pct) > 100.0:
                    continue
                # For suspicious basis (>5%, typical cash-and-carry is ±2%),
                # cross-check the token's contract address across the two
                # venues via token_registry. If registry says they're
                # demonstrably different tokens → drop. Unknown → pass.
                if abs(basis_pct) > 5.0:
                    try:
                        from backend.services.token_registry import validate_pair_identity
                        verdict = validate_pair_identity(sym, spot_ex, perp_ex)
                    except Exception:
                        verdict = None
                    if verdict is False:
                        continue
                gross = short_funding + basis_pct
                fee_spot_rt = _spot_fee(spot_ex) * 100 * 2  # round-trip, %
                fee_perp_rt = _arb._fee(perp_ex) * 100 * 2
                total_fees = fee_spot_rt + fee_perp_rt
                net = gross - total_fees
                # APR is funding-only — sustainable annual return that
                # doesn't include the one-off entry-basis pickup. 8h
                # window repeats 3 × 365 = 1095 times/year.
                funding_only = short_funding - total_fees
                net_apr = funding_only * (365.0 * 3) if funding_only > 0 else 0.0
                # No gross<=0 filter — show every spread, frontend styles
                # negatives differently.

                # In/Out compute REMOVED — screener now displays basis only,
                # detail page reads basis_pct directly. No orderbook lookup
                # in the spot-arb hot path.

                opps.append({
                    "type": "spot_short",
                    "symbol": sym,
                    "spot_exchange": spot_ex,
                    "short_exchange": perp_ex,
                    "spot_price": spot_price,
                    "perp_price": perp_price,
                    "spot_volume_usd": spot_vol,
                    "perp_volume_usd": perp_vol,
                    "funding_rate": rate_f,
                    "short_funding_8h": short_funding,
                    "basis_pct": basis_pct,
                    "gross": gross,
                    "fee_spot": fee_spot_rt,
                    "fee_perp": fee_perp_rt,
                    "total_fees": total_fees,
                    "net_profit": net,
                    "net_apr": net_apr,
                    "interval_h": interval_h,
                    "next_ts": pdata.get("next_ts", 0),
                })

    opps.sort(key=lambda x: x["net_profit"], reverse=True)
    return {
        "opportunities": opps[:200],
        "generated_at": int(time.time()),
        "spot_exchanges": SPOT_EXCHANGES,
    }


# ── Background refresh loop (dedicated daemon thread, own event loop) ────────
# Live-mode cadence after the 12-core upgrade: spot-short ticks every 1 s
# instead of 2 s, matching funding WS REST backstops. Cycle usually takes
# ~0.5-1 s; we just skip writes if it overruns (remaining >= 0.2s floor in
# the worker loop).
SPOT_REFRESH_INTERVAL = 1.0

_spot_thread: Any | None = None
_spot_stop = None          # threading.Event — created lazily
_spot_refresh_lock_fd = None


def _spot_worker_loop() -> None:
    """Persistent event-loop worker.

    Earlier version recreated the asyncio loop AND a fresh httpx.AsyncClient
    every tick. That made every venue's REST call go through a cold TLS
    handshake (Contabo path adds 8-12s on average per crypto-exchange
    edge), so a single cycle ballooned to 100-150 s. We now own one loop +
    one client for the lifetime of the thread; warm keepalive connections
    are reused tick-to-tick and the cycle drops to ~1-2 s on a healthy
    network and ~5-15 s when one venue lags.
    """
    import asyncio as _asyncio
    global _http
    logger.info("Spot worker thread running (interval=%.1fs)", SPOT_REFRESH_INTERVAL)
    last_good_count: int = 0
    last_write_ts: float = 0.0
    _FLICKER_WINDOW_S = 30.0
    _MIN_RETAIN_RATIO = 0.20

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    _http = httpx.AsyncClient(
        # connect=15s: Contabo→exchange TLS handshake regularly hits 8-12s
        # under typical conditions; the previous 5s ceiling guaranteed
        # ConnectTimeout bursts that pushed cycle time into the minutes.
        timeout=httpx.Timeout(connect=15.0, read=12.0, write=5.0, pool=5.0),
        headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16, keepalive_expiry=30),
        http2=False,
    )

    try:
        while not _spot_stop.is_set():
            t0 = time.time()
            try:
                result = loop.run_until_complete(get_spot_arbitrage_opportunities())
                current = len(result.get("opportunities") or [])
                now = time.time()
                too_thin = (
                    last_good_count > 10
                    and (now - last_write_ts) < _FLICKER_WINDOW_S
                    and (current == 0 or current < last_good_count * _MIN_RETAIN_RATIO)
                )
                if too_thin:
                    logger.info(
                        "spot refresh skipped (flicker guard): %d vs last_good=%d (%.1fs)",
                        current, last_good_count, time.time() - t0,
                    )
                else:
                    _arb._write_file_cache("spot_arbitrage.json", result)
                    if current > 0:
                        last_good_count = current
                        last_write_ts = now
                    logger.info(
                        "spot refresh: %d opps, %.1fs",
                        current, time.time() - t0,
                    )
            except Exception as exc:
                logger.warning("spot refresh failed: %s", exc)
            remaining = max(0.2, SPOT_REFRESH_INTERVAL - (time.time() - t0))
            _spot_stop.wait(remaining)
    finally:
        try:
            loop.run_until_complete(_http.aclose())
        except Exception:
            pass
        loop.close()


def start_spot_refresh_loop() -> None:
    """Start the spot refresh worker in a dedicated daemon thread."""
    import fcntl
    import threading
    global _spot_thread, _spot_stop, _spot_refresh_lock_fd
    if _spot_thread is not None and _spot_thread.is_alive():
        return
    try:
        _spot_refresh_lock_fd = open("/tmp/avalant_spot_refresh.lock", "w")
        fcntl.flock(_spot_refresh_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        logger.info("Spot refresh: another worker holds the lock — skipping")
        return
    _spot_stop = threading.Event()
    _spot_thread = threading.Thread(target=_spot_worker_loop, name="spot-refresh", daemon=True)
    _spot_thread.start()
    logger.info("Spot refresh thread started (every %.1fs)", SPOT_REFRESH_INTERVAL)


def stop_spot_refresh_loop() -> None:
    global _spot_thread, _spot_stop, _spot_refresh_lock_fd
    if _spot_stop is not None:
        _spot_stop.set()
    if _spot_thread is not None:
        _spot_thread.join(timeout=5.0)
    _spot_thread = None
    if _spot_refresh_lock_fd is not None:
        try:
            _spot_refresh_lock_fd.close()
        except Exception:
            pass
        _spot_refresh_lock_fd = None
