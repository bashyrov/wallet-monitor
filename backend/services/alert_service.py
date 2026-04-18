"""Background service: check arb spreads against user alerts, send Telegram notifications."""
import asyncio
import logging
from datetime import datetime, timedelta

import httpx

from settings import settings

logger = logging.getLogger("avalant.alerts")

_task: asyncio.Task | None = None
_CHECK_INTERVAL = 60.0   # seconds between checks
_COOLDOWN = timedelta(hours=1)  # don't re-trigger the same alert within 1h


async def _send_tg(chat_id: str, text: str) -> None:
    if not settings.TG_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{settings.TG_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    except Exception as exc:
        logger.warning("Telegram send failed for %s: %s", chat_id, exc)


_ANY = "*"  # stored value meaning "match any exchange"


async def _get_spread(symbol: str, long_ex: str, short_ex: str) -> float | None:
    """Spread % = (short_fr - long_fr) for a specific pair. 8h-normalised."""
    try:
        from backend.services.arbitrage_service import get_cached_rates
        rates = get_cached_rates()
        r_long = rates.get(f"{long_ex}:{symbol}")
        r_short = rates.get(f"{short_ex}:{symbol}")
        if r_long is None or r_short is None:
            return None
        fr_long = r_long["rate"] / r_long.get("interval_h", 8) * 8
        fr_short = r_short["rate"] / r_short.get("interval_h", 8) * 8
        return fr_short - fr_long
    except Exception:
        return None


async def _best_pair_for_symbol(symbol: str) -> tuple[str, str, float] | None:
    """Scan every cross-exchange pair for `symbol` and return the one with the
    largest absolute 8h-normalised spread. Returns (long_ex, short_ex, spread)
    or None if fewer than 2 exchanges quote the symbol.
    """
    try:
        from backend.services.arbitrage_service import get_cached_rates
        rates = get_cached_rates()
        # Collect all {exchange -> rate/interval} entries for this symbol
        by_ex: dict[str, float] = {}
        for key, v in rates.items():
            ex, sym = key.split(":", 1)
            if sym != symbol:
                continue
            by_ex[ex] = v["rate"] / v.get("interval_h", 8) * 8

        if len(by_ex) < 2:
            return None

        best = None   # (long_ex, short_ex, spread)
        for long_ex, fr_long in by_ex.items():
            for short_ex, fr_short in by_ex.items():
                if long_ex == short_ex:
                    continue
                spread = fr_short - fr_long
                if best is None or abs(spread) > abs(best[2]):
                    best = (long_ex, short_ex, spread)
        return best
    except Exception:
        return None


async def _check_alerts() -> None:
    if not settings.TG_BOT_TOKEN:
        return
    from backend.db.base import SessionLocal
    from backend.db.models import ArbAlert, User

    db = SessionLocal()
    try:
        alerts = db.query(ArbAlert).filter(ArbAlert.enabled == True).all()  # noqa: E712
        now = datetime.utcnow()
        base = settings.APP_BASE_URL.rstrip("/") if hasattr(settings, "APP_BASE_URL") else "https://avalant.xyz"
        for alert in alerts:
            if alert.last_triggered_at and (now - alert.last_triggered_at) < _COOLDOWN:
                continue

            # Resolve which pair to alert on
            long_ex = alert.long_exchange
            short_ex = alert.short_exchange
            is_any = (long_ex in ("", _ANY) or short_ex in ("", _ANY))

            if is_any:
                best = await _best_pair_for_symbol(alert.symbol)
                if not best:
                    continue
                long_ex, short_ex, spread = best
            else:
                spread = await _get_spread(alert.symbol, long_ex, short_ex)
                if spread is None:
                    continue

            spread_pct = spread * 100

            triggered = False
            if alert.direction == "any" and abs(spread_pct) >= alert.threshold:
                triggered = True
            elif alert.direction == "above" and spread_pct >= alert.threshold:
                triggered = True
            elif alert.direction == "below" and spread_pct <= -alert.threshold:
                triggered = True

            if triggered:
                user = db.query(User).filter(User.id == alert.user_id).first()
                chat_id = user.tg_chat_id if user else None
                if not chat_id:
                    logger.debug("Alert %d skip: user %s has not linked TG chat yet", alert.id, alert.user_id)
                    continue
                direction_arrow = "▲" if spread_pct >= 0 else "▼"
                link = f"{base}/arb?symbol={alert.symbol}&long={long_ex}&short={short_ex}"
                title = f"🚨 Arb Alert: {alert.symbol}"
                scope = "any exchange" if is_any else "tracked pair"
                msg = (
                    f"<b>{title}</b>\n"
                    f"Best pair now: <b>{long_ex}</b> → <b>{short_ex}</b>\n"
                    f"Spread: <b>{direction_arrow} {spread_pct:+.4f}%</b> (threshold ±{alert.threshold}%, {scope})\n"
                    f"<a href=\"{link}\">Open arbitrage details →</a>"
                )
                await _send_tg(str(chat_id), msg)
                alert.last_triggered_at = now
                db.commit()
                logger.info(
                    "Alert triggered id=%d user=%d sym=%s pair=%s→%s spread=%.4f%%",
                    alert.id, alert.user_id, alert.symbol, long_ex, short_ex, spread_pct,
                )
    except Exception as exc:
        logger.error("Alert check error: %s", exc)
    finally:
        db.close()


async def _alert_loop() -> None:
    while True:
        await _check_alerts()
        await asyncio.sleep(_CHECK_INTERVAL)


def start_alert_service() -> None:
    global _task
    if _task and not _task.done():
        return
    loop = asyncio.get_event_loop()
    _task = loop.create_task(_alert_loop())
    logger.info("Alert service started (interval=%ds)", int(_CHECK_INTERVAL))


def stop_alert_service() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
