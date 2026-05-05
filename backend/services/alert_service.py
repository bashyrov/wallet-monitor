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

# Observability — simple running counters, readable from the admin panel or logs.
_counters = {
    "tg_sent_ok":      0,
    "tg_attempt":      0,
    "tg_retry":        0,
    "tg_failed_final": 0,    # gave up after max retries
    "tg_rate_limited": 0,    # 429 from Telegram
}


def alert_service_counters() -> dict:
    """Expose a copy of the running counters for monitoring."""
    return dict(_counters)


async def _send_tg(chat_id: str, text: str, *, max_retries: int = 3) -> bool:
    """POST sendMessage with exponential backoff.
    Returns True on success, False if we gave up after max_retries.

    Backoff: 1s, 2s, 4s (plus 0-0.4s jitter) between attempts.
    If Telegram returns 429 we honour its Retry-After header (up to 30s).
    """
    if not settings.TG_BOT_TOKEN:
        logger.error("TG_BOT_TOKEN not set — alert not sent to chat %s", chat_id)
        return False

    import random
    url = f"https://api.telegram.org/bot{settings.TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    wait = 1.0

    async with httpx.AsyncClient(timeout=15) as client:
        for attempt in range(max_retries + 1):
            _counters["tg_attempt"] += 1
            try:
                r = await client.post(url, json=payload)
                if r.status_code == 200:
                    body = r.json()
                    if body.get("ok"):
                        if attempt > 0:
                            logger.info("TG sent OK for %s after %d retries", chat_id, attempt)
                        _counters["tg_sent_ok"] += 1
                        return True
                    # ok:false — log description, don't retry (likely bad payload / chat)
                    logger.error("TG sendMessage rejected for %s: %s", chat_id, body.get("description"))
                    _counters["tg_failed_final"] += 1
                    return False
                if r.status_code == 429:
                    _counters["tg_rate_limited"] += 1
                    retry_after = 0
                    try:
                        retry_after = int(r.json().get("parameters", {}).get("retry_after", 0))
                    except Exception:
                        pass
                    wait = min(max(retry_after, wait * 2), 30.0)
                    logger.warning("TG 429 for %s — retry %d/%d in %.1fs", chat_id, attempt + 1, max_retries, wait)
                elif 500 <= r.status_code < 600:
                    logger.warning("TG %d for %s — retry %d/%d in %.1fs", r.status_code, chat_id, attempt + 1, max_retries, wait)
                else:
                    # 4xx non-recoverable (bad chat, blocked by user, bad token)
                    logger.error("TG %d (non-retryable) for %s: %s", r.status_code, chat_id, r.text[:160])
                    _counters["tg_failed_final"] += 1
                    return False
            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                logger.warning("TG transport error for %s (%s): retry %d/%d in %.1fs",
                               chat_id, type(exc).__name__, attempt + 1, max_retries, wait)
            except Exception as exc:
                # Unknown failure — log at ERROR so it doesn't hide in the noise
                logger.error("TG unexpected error for %s: %s: %s", chat_id, type(exc).__name__, exc)
                _counters["tg_failed_final"] += 1
                return False

            if attempt >= max_retries:
                break
            _counters["tg_retry"] += 1
            await asyncio.sleep(wait + random.uniform(0, 0.4))
            wait = min(wait * 2, 8.0)

    logger.error("TG sendMessage gave up for %s after %d retries", chat_id, max_retries)
    _counters["tg_failed_final"] += 1
    return False


_ANY = "*"  # stored value meaning "match any exchange"

_CACHE_DIR = "/tmp/avalant_cache"

# Map mode → (cache filename, long-exchange field name in that cache)
_MODE_META = {
    "futures": ("arbitrage.json",     "long_exchange"),
    "spot":    ("spot_arbitrage.json", "spot_exchange"),
    "dex":     ("dex_arbitrage.json",  "dex_name"),
}

# Label for TG messages
_MODE_LABEL = {
    "futures": "Futures L/S",
    "spot":    "Spot/Short",
    "dex":     "DEX/Short",
}

# URL type param for the /arb page
_MODE_URL_TYPE = {
    "futures": "long-short",
    "spot":    "spot-short",
    "dex":     "dex-short",
}


def _load_arb_cache(mode: str) -> list[dict]:
    fname, _ = _MODE_META.get(mode, _MODE_META["futures"])
    try:
        import json
        with open(f"{_CACHE_DIR}/{fname}", "rb") as f:
            d = json.loads(f.read())
        return d.get("opportunities") or []
    except Exception:
        return []


def _opp_spread(opp: dict) -> float:
    """Return in_pct (preferred, orderbook-based) or net_profit as fallback."""
    v = opp.get("in_pct")
    if v is not None:
        return float(v)
    np = opp.get("net_profit")
    if np is not None:
        return float(np)
    return 0.0


def _get_spread_from_opps(opps: list[dict], symbol: str, long_ex: str,
                           short_ex: str, long_field: str) -> float | None:
    """Find the matching opp and return its in/out spread."""
    for opp in opps:
        if opp.get("symbol") != symbol:
            continue
        if opp.get(long_field, "").lower() != long_ex.lower():
            continue
        if opp.get("short_exchange", "").lower() != short_ex.lower():
            continue
        return _opp_spread(opp)
    return None


def _best_pair_from_opps(opps: list[dict], symbol: str,
                          long_field: str) -> tuple[str, str, float] | None:
    """Scan all opps for `symbol` and return the one with the largest |spread|."""
    best: tuple[str, str, float] | None = None
    for opp in opps:
        if opp.get("symbol") != symbol:
            continue
        spread = _opp_spread(opp)
        if best is None or abs(spread) > abs(best[2]):
            long_ex = opp.get(long_field, "")
            short_ex = opp.get("short_exchange", "")
            best = (long_ex, short_ex, spread)
    return best


async def _get_spread(symbol: str, long_ex: str, short_ex: str, mode: str) -> float | None:
    try:
        _, long_field = _MODE_META.get(mode, _MODE_META["futures"])
        opps = _load_arb_cache(mode)
        return _get_spread_from_opps(opps, symbol, long_ex, short_ex, long_field)
    except Exception:
        return None


async def _best_pair_for_symbol(symbol: str, mode: str) -> tuple[str, str, float] | None:
    try:
        _, long_field = _MODE_META.get(mode, _MODE_META["futures"])
        opps = _load_arb_cache(mode)
        return _best_pair_from_opps(opps, symbol, long_field)
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

            mode = alert.mode or "futures"
            long_ex = alert.long_exchange
            short_ex = alert.short_exchange
            is_any = (long_ex in ("", _ANY) or short_ex in ("", _ANY))

            if is_any:
                best = await _best_pair_for_symbol(alert.symbol, mode)
                if not best:
                    continue
                long_ex, short_ex, spread_pct = best
            else:
                spread_pct = await _get_spread(alert.symbol, long_ex, short_ex, mode)
                if spread_pct is None:
                    continue

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
                mode_label = _MODE_LABEL.get(mode, "Futures L/S")
                url_type = _MODE_URL_TYPE.get(mode, "long-short")
                scope = "any pair" if is_any else "tracked pair"
                link = f"{base}/arb?symbol={alert.symbol}&long={long_ex}&short={short_ex}&type={url_type}"
                msg = (
                    f"🚨 <b>Arb Alert: {alert.symbol}</b> · {mode_label}\n"
                    f"Pair: <b>{long_ex}</b> → <b>{short_ex}</b>\n"
                    f"In-spread: <b>{direction_arrow} {spread_pct:+.4f}%</b>"
                    f" (threshold ±{alert.threshold}%, {scope})\n"
                    f"<a href=\"{link}\">Open arbitrage details →</a>"
                )
                ok = await _send_tg(str(chat_id), msg)
                if ok:
                    alert.last_triggered_at = now
                    db.commit()
                    logger.info(
                        "Alert triggered id=%d user=%d sym=%s mode=%s pair=%s→%s spread=%.4f%%",
                        alert.id, alert.user_id, alert.symbol, mode, long_ex, short_ex, spread_pct,
                    )
                else:
                    logger.error(
                        "Alert %d delivery FAILED — will retry next cycle (user=%d sym=%s)",
                        alert.id, alert.user_id, alert.symbol,
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
