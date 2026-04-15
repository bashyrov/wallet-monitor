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


async def _get_spread(symbol: str, long_ex: str, short_ex: str) -> float | None:
    """Return current spread % = (short_fr - long_fr) if available from cached screener data."""
    try:
        from backend.services.arbitrage_service import get_cached_rates
        rates = get_cached_rates()
        key_long = f"{long_ex}:{symbol}"
        key_short = f"{short_ex}:{symbol}"
        r_long = rates.get(key_long)
        r_short = rates.get(key_short)
        if r_long is None or r_short is None:
            return None
        # Annualize to 8h equivalent for consistent comparison
        fr_long = r_long["rate"] / r_long.get("interval_h", 8) * 8
        fr_short = r_short["rate"] / r_short.get("interval_h", 8) * 8
        return fr_short - fr_long
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
        for alert in alerts:
            # Cooldown check
            if alert.last_triggered_at and (now - alert.last_triggered_at) < _COOLDOWN:
                continue

            spread = await _get_spread(alert.symbol, alert.long_exchange, alert.short_exchange)
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
                tg = user.tg_username if user else None
                if not tg:
                    continue
                direction_arrow = "▲" if spread_pct >= 0 else "▼"
                msg = (
                    f"<b>🚨 Arb Alert: {alert.symbol}</b>\n"
                    f"Long: <b>{alert.long_exchange}</b>  Short: <b>{alert.short_exchange}</b>\n"
                    f"Spread: <b>{direction_arrow} {spread_pct:+.4f}%</b> (threshold ±{alert.threshold}%)\n"
                    f"<a href='https://t.me/{tg}'>Open Avalant</a>"
                )
                await _send_tg(f"@{tg}", msg)
                alert.last_triggered_at = now
                db.commit()
                logger.info("Alert triggered id=%d for user %d spread=%.4f%%", alert.id, alert.user_id, spread_pct)
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
