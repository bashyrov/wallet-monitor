"""Executable Spread Calculator — given trade size USD, compute real executable spread
after walking the live orderbook (VWAP fills)."""
from __future__ import annotations

import httpx

from backend.services.arbitrage_service import _http, EXCHANGE_FEES


async def _fetch_book(exchange: str, symbol: str, limit: int = 50) -> dict:
    """Minimal orderbook fetch — reuses logic from screener.orderbook endpoint.
    Returns {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}."""
    ex = exchange.lower()
    sym_upper = symbol.upper()

    try:
        if ex == "binance":
            r = await _http.get(f"https://fapi.binance.com/fapi/v1/depth?symbol={sym_upper}USDT&limit={limit}")
            j = r.json()
            return {"bids": [[float(p), float(q)] for p, q in j.get("bids", [])],
                    "asks": [[float(p), float(q)] for p, q in j.get("asks", [])]}
        elif ex == "bybit":
            r = await _http.get(f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={sym_upper}USDT&limit={limit}")
            j = r.json().get("result", {})
            return {"bids": [[float(p), float(q)] for p, q in j.get("b", [])],
                    "asks": [[float(p), float(q)] for p, q in j.get("a", [])]}
        elif ex == "okx":
            r = await _http.get(f"https://www.okx.com/api/v5/market/books?instId={sym_upper}-USDT-SWAP&sz={limit}")
            data = (r.json().get("data") or [{}])[0]
            return {"bids": [[float(p), float(q)] for p, q, *_ in data.get("bids", [])],
                    "asks": [[float(p), float(q)] for p, q, *_ in data.get("asks", [])]}
        elif ex == "gate":
            r = await _http.get(f"https://api.gateio.ws/api/v4/futures/usdt/order_book?contract={sym_upper}_USDT&limit={limit}")
            j = r.json()
            return {"bids": [[float(x["p"]), float(x["s"])] for x in j.get("bids", [])],
                    "asks": [[float(x["p"]), float(x["s"])] for x in j.get("asks", [])]}
        elif ex == "bitget":
            r = await _http.get(f"https://api.bitget.com/api/v2/mix/market/merge-depth?symbol={sym_upper}USDT&productType=USDT-FUTURES&limit={limit}")
            data = r.json().get("data", {})
            return {"bids": [[float(p), float(q)] for p, q in data.get("bids", [])],
                    "asks": [[float(p), float(q)] for p, q in data.get("asks", [])]}
        elif ex == "mexc":
            r = await _http.get(f"https://contract.mexc.com/api/v1/contract/depth/{sym_upper}_USDT?limit={limit}")
            data = r.json().get("data", {})
            return {"bids": [[float(p), float(q)] for p, q, *_ in data.get("bids", [])],
                    "asks": [[float(p), float(q)] for p, q, *_ in data.get("asks", [])]}
        elif ex == "hyperliquid":
            r = await _http.post("https://api.hyperliquid.xyz/info",
                                 json={"type": "l2Book", "coin": sym_upper},
                                 headers={"Content-Type": "application/json"})
            levels = r.json().get("levels", [[], []])
            return {"bids": [[float(x["px"]), float(x["sz"])] for x in levels[0]],
                    "asks": [[float(x["px"]), float(x["sz"])] for x in levels[1]]}
        elif ex == "aster":
            r = await _http.get(f"https://fapi.asterdex.com/fapi/v1/depth?symbol={sym_upper}USDT&limit={limit}")
            j = r.json()
            return {"bids": [[float(p), float(q)] for p, q in j.get("bids", [])],
                    "asks": [[float(p), float(q)] for p, q in j.get("asks", [])]}
    except Exception:
        pass
    return {"bids": [], "asks": []}


def _walk(levels: list[list[float]], size_usd: float) -> tuple[float, float]:
    """Walk orderbook levels to fill size_usd notional. Returns (vwap_price, filled_usd)."""
    spent = 0.0
    tokens = 0.0
    for price, qty in levels:
        level_usd = price * qty
        if spent + level_usd >= size_usd:
            needed_usd = size_usd - spent
            take_tokens = needed_usd / price
            tokens += take_tokens
            spent += needed_usd
            return (spent / tokens if tokens else price, spent)
        spent += level_usd
        tokens += qty
    return (spent / tokens if tokens else 0, spent)


async def calculate(symbol: str, long_ex: str, short_ex: str, size_usd: float) -> dict:
    """Returns executable spread + slippage breakdown."""
    book_long = await _fetch_book(long_ex, symbol)
    book_short = await _fetch_book(short_ex, symbol)

    # long-leg: BUY on long_ex → walks asks
    # short-leg: SELL on short_ex → walks bids
    buy_vwap, buy_filled = _walk(book_long.get("asks", []), size_usd)
    sell_vwap, sell_filled = _walk(book_short.get("bids", []), size_usd)

    best_ask_long = book_long["asks"][0][0] if book_long.get("asks") else 0
    best_bid_short = book_short["bids"][0][0] if book_short.get("bids") else 0

    slip_long_bps = ((buy_vwap - best_ask_long) / best_ask_long * 10000) if best_ask_long else 0
    slip_short_bps = ((best_bid_short - sell_vwap) / best_bid_short * 10000) if best_bid_short else 0

    quoted_spread_pct = ((best_bid_short - best_ask_long) / best_ask_long * 100) if best_ask_long else 0
    exec_spread_pct = ((sell_vwap - buy_vwap) / buy_vwap * 100) if buy_vwap else 0
    slippage_pct = quoted_spread_pct - exec_spread_pct

    fee_pct = (EXCHANGE_FEES.get(long_ex, 0.0005) + EXCHANGE_FEES.get(short_ex, 0.0005)) * 2 * 100
    net_pct = exec_spread_pct - fee_pct

    return {
        "symbol": symbol,
        "long_exchange": long_ex,
        "short_exchange": short_ex,
        "size_usd": size_usd,
        "long_buy_vwap": round(buy_vwap, 6),
        "short_sell_vwap": round(sell_vwap, 6),
        "long_filled_usd": round(buy_filled, 2),
        "short_filled_usd": round(sell_filled, 2),
        "long_filled_pct": round(buy_filled / size_usd * 100, 1) if size_usd else 0,
        "short_filled_pct": round(sell_filled / size_usd * 100, 1) if size_usd else 0,
        "best_ask_long": best_ask_long,
        "best_bid_short": best_bid_short,
        "quoted_spread_pct": round(quoted_spread_pct, 4),
        "executable_spread_pct": round(exec_spread_pct, 4),
        "slippage_pct": round(slippage_pct, 4),
        "slip_long_bps": round(slip_long_bps, 2),
        "slip_short_bps": round(slip_short_bps, 2),
        "round_trip_fees_pct": round(fee_pct, 4),
        "net_spread_pct": round(net_pct, 4),
        "net_usd": round(net_pct / 100 * size_usd, 2),
        "book_long_depth_levels": len(book_long.get("asks", [])),
        "book_short_depth_levels": len(book_short.get("bids", [])),
    }
