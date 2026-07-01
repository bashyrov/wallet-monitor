"""
Hourly price cache.

Flow:
  1. CoinMarketCap /v1/cryptocurrency/listings/latest → top-100 symbols
  2. Gate.io GET /api/v4/spot/tickers (public, no auth) → *_USDT prices
  3. Build in-memory dict {SYMBOL: usd_price}
  4. Mirror to Redis so app + app2 + fetcher share one snapshot rather
     than each making its own CMC call. Read-through on every get_price()
     so a fresh app process picks up the existing cache without waiting
     for its own first refresh cycle.

Stablecoins always equal 1.0.
Falls back to CMC price data if Gate doesn't have the pair.
"""
import asyncio
import json
import logging
import os
import time as _time
from decimal import Decimal

import httpx
from backend.providers.http import RetryClient

logger = logging.getLogger("avalant.prices")

# Redis-backed shared snapshot. Key holds full {sym: price} map; we read
# it whenever the local _prices dict is empty (cold start) so a freshly-
# booted app process doesn't have to wait its own 30-min refresh cycle.
_REDIS_KEY = "avalant:prices:snapshot:v1"
_REDIS_TOP100_KEY = "avalant:prices:top100:v1"
_REDIS_TTL_S = 3600  # 1h — refresh cycle is 30min so this is double-buffer
_redis_client = None
_redis_last_failure_ts: float = 0.0
_REDIS_BACKOFF_S = 10.0


def _redis():
    global _redis_client, _redis_last_failure_ts
    url = os.environ.get("REDIS_URL") or ""
    if not url:
        return None
    if _redis_client is not None:
        return _redis_client
    if _time.time() - _redis_last_failure_ts < _REDIS_BACKOFF_S:
        return None
    try:
        import redis
        c = redis.from_url(url, decode_responses=True,
                           socket_connect_timeout=1.0, socket_timeout=1.0)
        c.ping()
        _redis_client = c
        return c
    except Exception as exc:
        _redis_last_failure_ts = _time.time()
        logger.debug("prices redis connect failed: %s", exc)
        return None


def _publish_to_redis(prices: dict[str, float], top100: list[str]) -> None:
    c = _redis()
    if not c:
        return
    try:
        c.setex(_REDIS_KEY, _REDIS_TTL_S, json.dumps(prices))
        c.setex(_REDIS_TOP100_KEY, _REDIS_TTL_S, json.dumps(top100))
    except Exception as exc:
        logger.debug("prices redis publish failed: %s", exc)


def _try_load_from_redis() -> bool:
    """Pull the shared snapshot if local cache is empty. Returns True if
    we hydrated from Redis."""
    if _prices:
        return False
    c = _redis()
    if not c:
        return False
    try:
        raw = c.get(_REDIS_KEY)
        raw_top = c.get(_REDIS_TOP100_KEY)
        if not raw:
            return False
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            return False
        _prices.update({str(k).upper(): float(v) for k, v in loaded.items()
                        if isinstance(v, (int, float))})
        if raw_top:
            try:
                top = json.loads(raw_top)
                if isinstance(top, list):
                    _top100.update(str(s).upper() for s in top)
            except Exception:
                pass
        logger.info("prices: hydrated from Redis (%d entries)", len(_prices))
        return True
    except Exception as exc:
        logger.debug("prices redis hydrate failed: %s", exc)
        return False

STABLE_PRICE = 1.0
STABLES = {
    "USD", "USDT", "USDC", "USDC.E", "USDCE", "DAI", "USDE", "USDE",
    "BUSD", "TUSD", "USDP", "USDD", "FDUSD", "PYUSD",
}

# In-memory cache: symbol (uppercase) → USD price as float
_prices: dict[str, float] = {}
# Symbols in top-100 from CMC (uppercase)
_top100: set[str] = set()

_refresh_task: asyncio.Task | None = None


# Wrapped / bridged tokens → underlying symbol for price lookup.
# Covers: native wrappers (WBTC/WETH), Binance-bridged (BTCB), Avalanche
# bridge (.E suffix), Arbitrum/Optimism bridged WETH (.A / .O), USDC
# variants across L2s, Harmony bridge (1USDC etc.), Base bridge (bETH).
WRAPPED_MAP: dict[str, str] = {
    # BTC family
    "WBTC": "BTC", "BTCB": "BTC", "BTC.B": "BTC",
    "TBTC": "BTC", "CBBTC": "BTC", "RENBTC": "BTC",
    # ETH family — including L2 bridge wrappers
    "WETH": "ETH", "ETH.E": "ETH", "WETH.E": "ETH",
    "WETH.A": "ETH", "WETH.O": "ETH", "WETH.M": "ETH",
    "BETH": "ETH", "STETH": "ETH", "CBETH": "ETH", "RETH": "ETH",
    # BNB family
    "WBNB": "BNB", "BNBB": "BNB",
    # Polygon (rebranded to POL)
    "WMATIC": "POL", "MATIC": "POL",
    # Avalanche
    "WAVAX": "AVAX",
    # Solana
    "WSOL": "SOL",
    # Tron
    "WTRX": "TRX",
    # Fantom / Sonic
    "WFTM": "FTM",
    "WS": "S",
    # Cross-chain USDC — Bridged / Native distinct on some venues
    # (users see USDC.E on Arbitrum/Optimism/Avalanche, USDbC on Base
    # etc.). Prices are pinned as stables via STABLES set — these entries
    # here are a safety net for wallets that quote them as non-stable.
    "USDC.E": "USDC", "USDCE": "USDC",
    "USDBC": "USDC", "USDB.C": "USDC",
    "USDT.E": "USDT", "USDTE": "USDT",
    "1USDC": "USDC", "1USDT": "USDT",  # Harmony bridged
    "DAI.E": "DAI",
}


def get_price(symbol: str) -> float | None:
    """Return USD price for symbol, or None if unknown."""
    s = symbol.upper().replace(".E", "").replace("-PERP", "").replace("_PERP", "")
    if s in STABLES:
        return STABLE_PRICE
    # Cold-start hydrate from Redis on the very first call — saves the
    # ~5s of waiting for the local refresh loop on a fresh app boot.
    if not _prices:
        _try_load_from_redis()
    underlying = WRAPPED_MAP.get(s)
    if underlying:
        return _prices.get(underlying)
    return _prices.get(s)


def get_usd_value(symbol: str, amount: str) -> float | None:
    """Return USD value for amount of symbol, or None if price unknown."""
    price = get_price(symbol)
    if price is None:
        return None
    try:
        return float(Decimal(str(amount)) * Decimal(str(price)))
    except Exception:
        return None


def price_cache_snapshot() -> dict[str, float]:
    """Return a copy of the full price cache (for the /api/prices endpoint)."""
    return dict(_prices)


def top100_symbols() -> list[str]:
    return sorted(_top100)


async def refresh_prices() -> None:
    """Fetch top-100 from CMC, then prices from Gate. Update _prices in place."""
    from settings import settings

    cmc_key = settings.CMC_API_KEY
    if not cmc_key:
        logger.warning("CMC_API_KEY not set — price cache disabled")
        return

    async with RetryClient(timeout=20) as client:
        # ── Step 1: CMC top-100 ──────────────────────────────────────────────
        try:
            r = await client.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
                headers={"X-CMC_PRO_API_KEY": cmc_key, "Accept": "application/json"},
                params={"limit": 100, "convert": "USD", "sort": "market_cap"},
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.error("CMC fetch failed: %s", e)
            return

        cmc_entries = data.get("data") or []
        if not cmc_entries:
            logger.error("CMC returned empty data")
            return

        # {symbol: cmc_usd_price} — used as fallback if Gate doesn't have the pair
        cmc_prices: dict[str, float] = {}
        symbols: set[str] = set()
        for entry in cmc_entries:
            sym = (entry.get("symbol") or "").upper()
            if not sym:
                continue
            symbols.add(sym)
            try:
                price = float(entry["quote"]["USD"]["price"])
                cmc_prices[sym] = price
            except Exception:
                pass

        _top100.clear()
        _top100.update(symbols)
        logger.info("CMC top-100 loaded: %d symbols", len(symbols))

        # ── Step 2: Gate spot tickers (public) ───────────────────────────────
        # Store ALL Gate _USDT pairs (~3000), not just top-100. Same API
        # call, no rate limit impact, gets us the long tail of alts
        # (small caps, meme tokens) that CMC top-100 misses. Portfolio
        # views previously showed amount without USD value for those.
        gate_prices: dict[str, float] = {}
        try:
            r2 = await client.get(
                "https://api.gateio.ws/api/v4/spot/tickers",
                params={"timezone": "utc"},
            )
            r2.raise_for_status()
            tickers = r2.json()
        except Exception as e:
            logger.warning("Gate tickers fetch failed: %s — using CMC prices", e)
            tickers = []

        for ticker in tickers:
            pair = ticker.get("currency_pair", "")  # e.g. "BTC_USDT"
            if not pair.endswith("_USDT"):
                continue
            base = pair[:-5].upper()  # strip _USDT
            try:
                price = float(ticker.get("last") or 0)
                if price > 0:
                    gate_prices[base] = price
            except Exception:
                pass

        logger.info("Gate prices fetched: %d USDT pairs (long tail incl.)", len(gate_prices))

        # ── Step 3: Merge — full Gate cache + CMC top-100 as authoritative ───
        # Order matters: Gate for the full universe, CMC overwrites top-100
        # so BTC/ETH/etc. use the CMC price (more accurate than a single
        # exchange's spot). Stables are pinned at $1.
        new_prices: dict[str, float] = {}
        # Gate covers the long tail (~3000 symbols)
        new_prices.update(gate_prices)
        # CMC top-100 wins for majors (more reliable than any single spot)
        for sym, px in cmc_prices.items():
            new_prices[sym] = px
        # Stables pinned
        for sym in STABLES:
            new_prices[sym] = STABLE_PRICE

        _prices.clear()
        _prices.update(new_prices)
        logger.info("Price cache updated: %d entries", len(_prices))
        # Mirror to Redis so other processes share the snapshot.
        _publish_to_redis(new_prices, sorted(_top100))


async def _price_loop() -> None:
    """Background loop: refresh every hour."""
    # First refresh immediately on startup
    try:
        await refresh_prices()
    except Exception as e:
        logger.error("Initial price refresh failed: %s", e)

    while True:
        await asyncio.sleep(1800)
        try:
            await refresh_prices()
        except Exception as e:
            logger.error("Price refresh failed: %s", e)


def start_price_loop() -> None:
    """Schedule the background price refresh loop (call from lifespan)."""
    global _refresh_task
    _refresh_task = asyncio.create_task(_price_loop())


def stop_price_loop() -> None:
    global _refresh_task
    if _refresh_task:
        _refresh_task.cancel()
        _refresh_task = None
