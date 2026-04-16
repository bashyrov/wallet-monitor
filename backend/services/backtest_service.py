"""Quick Backtest — simulate holding an arb position over a historical window.

Uses funding rate history from /screener/arb-history (two exchanges) and computes
the cumulative funding P&L if the user held `size_usd` notional on each leg for N days.
Fees are charged once (entry+exit) on both legs.

Best-effort simulation — assumes perfect fill at each funding tick with flat price;
price-leg P&L over the window is NOT included (that's path-dependent).
"""
from __future__ import annotations

from backend.services.arbitrage_service import EXCHANGE_FEES


async def _fetch_history_for(exchange: str, symbol: str, limit: int = 90) -> list[dict]:
    from backend.api.v1.screener import _fetch_history_for as _fn
    return await _fn(exchange, symbol, limit)


async def backtest(
    symbol: str, long_ex: str, short_ex: str, size_usd: float, days: int = 7
) -> dict:
    # fetch funding history for both exchanges
    limit = max(days * 6, 30)
    long_hist = await _fetch_history_for(long_ex, symbol, limit=limit)
    short_hist = await _fetch_history_for(short_ex, symbol, limit=limit)

    if not long_hist or not short_hist:
        return {"error": "No funding history available for one or both exchanges"}

    # clip window
    cutoff_ts = (long_hist[-1]["ts"] if long_hist else 0) - days * 86400
    long_hist = [x for x in long_hist if x["ts"] >= cutoff_ts]
    short_hist = [x for x in short_hist if x["ts"] >= cutoff_ts]

    # Funding accrual: long leg PAYS when rate > 0; short leg RECEIVES when rate > 0
    # Net per-tick = size_usd * (short_rate - long_rate)  [as decimal]
    # Funding rates from our fetchers are already decimal (e.g. 0.0001 = 0.01%).

    long_total = sum(size_usd * x["rate"] for x in long_hist)   # $ paid by long side (if positive rate)
    short_total = sum(size_usd * x["rate"] for x in short_hist)  # $ paid by short side

    net_funding = short_total - long_total  # short RECEIVES positive rate, long PAYS → net = short - long

    fees = size_usd * (EXCHANGE_FEES.get(long_ex, 0.0005) + EXCHANGE_FEES.get(short_ex, 0.0005)) * 2  # round-trip both legs
    net_pnl = net_funding - fees

    apr = (net_pnl / size_usd) * (365 / max(days, 1)) * 100

    return {
        "symbol": symbol,
        "long_exchange": long_ex,
        "short_exchange": short_ex,
        "size_usd": size_usd,
        "days": days,
        "long_funding_ticks": len(long_hist),
        "short_funding_ticks": len(short_hist),
        "long_funding_paid_usd": round(long_total, 4),
        "short_funding_received_usd": round(short_total, 4),
        "net_funding_usd": round(net_funding, 4),
        "round_trip_fees_usd": round(fees, 4),
        "net_pnl_usd": round(net_pnl, 4),
        "net_pnl_pct": round(net_pnl / size_usd * 100, 3) if size_usd else 0,
        "annualized_apr_pct": round(apr, 2),
    }
