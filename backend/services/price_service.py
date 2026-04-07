"""
Hourly price cache.

Flow:
  1. CoinMarketCap /v1/cryptocurrency/listings/latest → top-100 symbols
  2. Gate.io GET /api/v4/spot/tickers (public, no auth) → *_USDT prices
  3. Build in-memory dict {SYMBOL: usd_price}

Stablecoins always equal 1.0.
Falls back to CMC price data if Gate doesn't have the pair.
"""
import asyncio
import logging
from decimal import Decimal

import httpx
from backend.providers.http import RetryClient

logger = logging.getLogger("avalant.prices")

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


# Wrapped / bridged tokens → underlying symbol for price lookup
WRAPPED_MAP: dict[str, str] = {
    "WBTC": "BTC", "BTCB": "BTC", "BTC.B": "BTC",
    "WETH": "ETH", "ETH.E": "ETH", "WETH.E": "ETH",
    "WBNB": "BNB",
    "WMATIC": "POL", "MATIC": "POL",
    "WAVAX": "AVAX",
    "WSOL": "SOL",
    "WTRX": "TRX",
    "WFTM": "FTM",
}


def get_price(symbol: str) -> float | None:
    """Return USD price for symbol, or None if unknown."""
    s = symbol.upper().replace(".E", "").replace("-PERP", "").replace("_PERP", "")
    if s in STABLES:
        return STABLE_PRICE
    # Try wrapped map first
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
            if base not in symbols:
                continue
            try:
                price = float(ticker.get("last") or 0)
                if price > 0:
                    gate_prices[base] = price
            except Exception:
                pass

        logger.info("Gate prices fetched: %d / %d symbols", len(gate_prices), len(symbols))

        # ── Step 3: Merge (Gate preferred, CMC as fallback) ──────────────────
        new_prices: dict[str, float] = {}
        for sym in symbols:
            if sym in STABLES:
                new_prices[sym] = STABLE_PRICE
            elif sym in gate_prices:
                new_prices[sym] = gate_prices[sym]
            elif sym in cmc_prices:
                new_prices[sym] = cmc_prices[sym]

        _prices.clear()
        _prices.update(new_prices)
        logger.info("Price cache updated: %d entries", len(_prices))


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
