"""Background service: check arb spreads against user alerts, send Telegram notifications."""
import asyncio
import logging
from datetime import datetime, timedelta

import httpx

from settings import settings

logger = logging.getLogger("avalant.alerts")

_task: asyncio.Task | None = None
_CHECK_INTERVAL = 0.5    # seconds between spread checks (matches go-fetcher arb cycle)
_DB_REFRESH_INTERVAL = 10.0  # seconds between alert list re-reads from DB
_COOLDOWN = timedelta(hours=1)  # defence-in-depth: even if auto-disable race-loses, no re-trigger within 1h

# In-memory alert cache — avoids a DB query on every 500ms tick.
_alerts_cache: list = []
_alerts_cache_ts: float = 0.0

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


async def _send_tg(chat_id: str, text: str) -> bool:
    """POST sendMessage. NO retries — at-most-once delivery.

    Why no retries: Telegram's sendMessage has no idempotency key. If our
    POST reaches TG but our READ of the response times out, retrying
    sends the message twice. We saw this in prod (5+ duplicates per
    fire). Better to silently miss one alert than spam the user.
    Alert stays disabled regardless of TG outcome (fail-closed) — user
    re-enables from the bell when they want the next attempt.

    Network connect errors (TG unreachable, TCP refused) are also not
    retried for the same reason: while *connect* refusals are safe, a
    half-open socket where bytes were written but ACK was lost is not
    distinguishable at the httpx layer.
    """
    if not settings.TG_BOT_TOKEN:
        logger.error("TG_BOT_TOKEN not set — alert not sent to chat %s", chat_id)
        return False

    import time as _t
    url = f"https://api.telegram.org/bot{settings.TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    t_start = _t.monotonic()
    _counters["tg_attempt"] += 1

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json=payload)
        elapsed_ms = int((_t.monotonic() - t_start) * 1000)
        if r.status_code == 200:
            body = r.json()
            if body.get("ok"):
                _counters["tg_sent_ok"] += 1
                logger.info("TG send OK chat=%s took=%dms", chat_id, elapsed_ms)
                return True
            logger.error("TG send rejected chat=%s desc=%s took=%dms", chat_id, body.get("description"), elapsed_ms)
            _counters["tg_failed_final"] += 1
            return False
        if r.status_code == 429:
            _counters["tg_rate_limited"] += 1
            retry_after = 0
            try:
                retry_after = int(r.json().get("parameters", {}).get("retry_after", 0))
            except Exception:
                pass
            logger.error("TG 429 rate-limited chat=%s retry_after=%ds took=%dms — alert stays disabled",
                         chat_id, retry_after, elapsed_ms)
            _counters["tg_failed_final"] += 1
            return False
        # any other status → log & fail-closed
        logger.error("TG send HTTP %d chat=%s body=%s took=%dms",
                     r.status_code, chat_id, r.text[:160], elapsed_ms)
        _counters["tg_failed_final"] += 1
        return False
    except Exception as exc:
        elapsed_ms = int((_t.monotonic() - t_start) * 1000)
        logger.error("TG send error chat=%s err=%s: %s took=%dms",
                     chat_id, type(exc).__name__, exc, elapsed_ms)
        _counters["tg_failed_final"] += 1
        return False


_ANY = "*"  # stored value meaning "match any exchange"

_CACHE_DIR = "/tmp/avalant_cache"

# Map mode → (cache filename, long-exchange field name in that cache)
# dex_spot has a special field name `cex_exchange` for the short-leg side
# (CEX spot, not perp). The lookup code (_get_spread_from_opps) compares
# against `short_exchange` for non-dex_spot rows; dex_spot rows substitute
# `cex_exchange` — see the conditional in _get_spread_from_opps.
_MODE_META = {
    "futures":  ("arbitrage.json",         "long_exchange"),
    "spot":     ("spot_arbitrage.json",     "spot_exchange"),
    "dex":      ("dex_arbitrage.json",      "dex_name"),
    "dex_spot": ("dex_spot_arbitrage.json", "dex_name"),
}

# Label for TG messages
_MODE_LABEL = {
    "futures":  "Futures L/S",
    "spot":     "Spot/Short",
    "dex":      "DEX/Short",
    "dex_spot": "DEX/Spot",
}

# URL type param for the /arb page
_MODE_URL_TYPE = {
    "futures":  "long-short",
    "spot":     "spot-short",
    "dex":      "dex-short",
    "dex_spot": "dex-spot",
}


_arb_cache_by_mode: dict[str, tuple[float, list[dict]]] = {}
_ARB_CACHE_TTL = 0.25  # seconds — fresh enough for 0.5s tick, halves disk I/O


def _load_arb_cache(mode: str) -> list[dict]:
    """Read arbitrage.json / spot_arbitrage.json / dex_arbitrage.json with a
    short in-process cache so a tick that touches every alert doesn't re-read
    + re-parse the file N times."""
    fname, _ = _MODE_META.get(mode, _MODE_META["futures"])
    import time as _t
    now = _t.monotonic()
    cached = _arb_cache_by_mode.get(mode)
    if cached and (now - cached[0]) < _ARB_CACHE_TTL:
        return cached[1]
    try:
        import json
        with open(f"{_CACHE_DIR}/{fname}", "rb") as f:
            d = json.loads(f.read())
        opps = d.get("opportunities") or []
    except Exception:
        opps = []
    _arb_cache_by_mode[mode] = (now, opps)
    return opps


def _opp_spread(opp: dict) -> float:
    """Return in_pct (preferred, orderbook-based) or net_profit as fallback.
    dex_spot has no in_pct/net_profit — it's a spot-spot delta — so we
    fall back to spread_pct which is the signed (cex - dex) / mid * 100."""
    v = opp.get("in_pct")
    if v is not None:
        return float(v)
    np = opp.get("net_profit")
    if np is not None:
        return float(np)
    sp = opp.get("spread_pct")
    if sp is not None:
        return float(sp)
    return 0.0


def _short_field_for_mode(mode: str) -> str:
    """dex_spot stores the CEX leg as `cex_exchange`; everyone else as
    `short_exchange`. Used by _get_spread_from_opps + _best_pair_from_opps
    so the same scan code handles both wire shapes."""
    return "cex_exchange" if mode == "dex_spot" else "short_exchange"


def _get_spread_from_opps(opps: list[dict], symbol: str, long_ex: str,
                           short_ex: str, long_field: str,
                           short_field: str = "short_exchange") -> float | None:
    """Find the matching opp and return its in/out spread."""
    for opp in opps:
        if opp.get("symbol") != symbol:
            continue
        if opp.get(long_field, "").lower() != long_ex.lower():
            continue
        if opp.get(short_field, "").lower() != short_ex.lower():
            continue
        return _opp_spread(opp)
    return None


def _best_pair_from_opps(opps: list[dict], symbol: str,
                          long_field: str,
                          short_field: str = "short_exchange") -> tuple[str, str, float] | None:
    """Scan all opps for `symbol` and return the one with the largest |spread|."""
    best: tuple[str, str, float] | None = None
    for opp in opps:
        if opp.get("symbol") != symbol:
            continue
        spread = _opp_spread(opp)
        if best is None or abs(spread) > abs(best[2]):
            long_ex = opp.get(long_field, "")
            short_ex = opp.get(short_field, "")
            best = (long_ex, short_ex, spread)
    return best


async def _get_spread(symbol: str, long_ex: str, short_ex: str, mode: str) -> float | None:
    try:
        _, long_field = _MODE_META.get(mode, _MODE_META["futures"])
        short_field = _short_field_for_mode(mode)
        opps = _load_arb_cache(mode)
        return _get_spread_from_opps(opps, symbol, long_ex, short_ex, long_field, short_field)
    except Exception:
        return None


async def _best_pair_for_symbol(symbol: str, mode: str) -> tuple[str, str, float] | None:
    try:
        _, long_field = _MODE_META.get(mode, _MODE_META["futures"])
        short_field = _short_field_for_mode(mode)
        opps = _load_arb_cache(mode)
        return _best_pair_from_opps(opps, symbol, long_field, short_field)
    except Exception:
        return None


_ALERT_LOCK_KEY = "avalant:alert_check_lock"
_ALERT_LOCK_TTL = 0   # unused — see leader election below


def _load_alerts_from_db() -> list:
    """Read enabled alerts + their users from DB. Called every _DB_REFRESH_INTERVAL seconds.

    Also writes /tmp/avalant_cache/active_alerts.json so go-fetcher can
    mark these symbols as CLASS 3 hot (event-driven /ws/book bypass).
    Without this, alerts only fire on the 2s aggregate tick — i.e. the
    same latency as the cold screener table. With this, the alert
    check sees BBO changes as they happen."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbAlert, User
    db = SessionLocal()
    try:
        alerts = db.query(ArbAlert).filter(ArbAlert.enabled == True).all()  # noqa: E712
        # Eagerly load tg_chat_id so we don't need DB again during the hot loop
        user_ids = {a.user_id for a in alerts}
        users = {u.id: u.tg_chat_id for u in db.query(User).filter(User.id.in_(user_ids)).all()}
        for a in alerts:
            a._tg_chat_id = users.get(a.user_id)
        _dump_active_alert_symbols(alerts)
        return alerts
    finally:
        db.close()


_ACTIVE_ALERTS_PATH = "/tmp/avalant_cache/active_alerts.json"


def _dump_active_alert_symbols(alerts: list) -> None:
    """Write the union of symbols carrying an active alert to a JSON file
    so go-fetcher can pick them up via its existing prewarm/touch loop.

    Atomic: write to <path>.tmp then rename. Schema kept intentionally
    small — just `{"symbols": ["BTC", "ETH", ...]}` — so the Go reader is a
    single json.Unmarshal."""
    import json as _json
    import os as _os
    syms = sorted({(a.symbol or "").strip().upper() for a in alerts if (a.symbol or "").strip()})
    payload = {"symbols": syms, "ts": int(datetime.utcnow().timestamp())}
    try:
        _os.makedirs(_os.path.dirname(_ACTIVE_ALERTS_PATH), exist_ok=True)
        tmp = _ACTIVE_ALERTS_PATH + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(payload, f)
        _os.replace(tmp, _ACTIVE_ALERTS_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.debug("active_alerts dump failed: %s", exc)


def _claim_alert_for_fire(alert_id: int, ts: datetime) -> bool:
    """Atomic claim across workers / replicas. Sets enabled=False AND
    last_triggered_at=ts iff the alert is still enabled. Returns True if
    THIS worker won the row, False if someone else already claimed it
    (or the user disabled it). Postgres serialises the UPDATE WHERE so
    only one writer can flip the row.

    Fail-closed: if TG send later fails, the alert stays off. User
    re-enables from the navbar popover. Better one missed ping than
    seven duplicate pings."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbAlert
    db = SessionLocal()
    try:
        rows = db.query(ArbAlert).filter(
            ArbAlert.id == alert_id,
            ArbAlert.enabled == True,  # noqa: E712
        ).update(
            {"last_triggered_at": ts, "enabled": False},
            synchronize_session=False,
        )
        db.commit()
        return rows > 0
    finally:
        db.close()


async def _check_alerts() -> None:
    global _alerts_cache, _alerts_cache_ts

    if not settings.TG_BOT_TOKEN:
        return

    # Leader election: only one uvicorn worker runs the hot loop.
    try:
        import redis as _redis
        _r = _redis.from_url(settings.REDIS_URL, socket_connect_timeout=1)
        if not _r.set("avalant:alert_leader", "1", nx=True, ex=1):
            return
    except Exception:
        pass  # no Redis → all workers run (acceptable rare double-send)

    import time
    now_ts = time.monotonic()

    # Refresh alert list from DB every _DB_REFRESH_INTERVAL seconds
    if now_ts - _alerts_cache_ts >= _DB_REFRESH_INTERVAL:
        try:
            _alerts_cache = _load_alerts_from_db()
            _alerts_cache_ts = now_ts
        except Exception as exc:
            logger.error("Alert DB refresh failed: %s", exc)
            return

    alerts = _alerts_cache
    if not alerts:
        return

    now = datetime.utcnow()
    base = settings.APP_BASE_URL.rstrip("/") if hasattr(settings, "APP_BASE_URL") else "https://avalant.xyz"

    try:
        for alert in alerts:
            # One-shot semantics: a fired alert is disabled in DB right after
            # a successful TG send. Skip locally-disabled alerts in case the
            # 10s DB cache is stale; the cooldown check is a defence-in-depth
            # for the same window.
            if not getattr(alert, "enabled", True):
                continue
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

            if not triggered:
                continue

            # Protected mode: wait 3s and re-verify the condition still holds.
            trigger_mode = alert.trigger_mode or "speed"
            if trigger_mode == "protected":
                await asyncio.sleep(3)
                if is_any:
                    best2 = await _best_pair_for_symbol(alert.symbol, mode)
                    if not best2:
                        logger.debug("Alert %d protected: condition gone after re-check", alert.id)
                        continue
                    long_ex, short_ex, spread_pct = best2
                else:
                    spread2 = await _get_spread(alert.symbol, long_ex, short_ex, mode)
                    if spread2 is None:
                        continue
                    spread_pct = spread2

                still_on = False
                if alert.direction == "any" and abs(spread_pct) >= alert.threshold:
                    still_on = True
                elif alert.direction == "above" and spread_pct >= alert.threshold:
                    still_on = True
                elif alert.direction == "below" and spread_pct <= -alert.threshold:
                    still_on = True
                if not still_on:
                    logger.debug("Alert %d protected: spread %.4f%% no longer meets threshold — skip",
                                 alert.id, spread_pct)
                    continue

            chat_id = getattr(alert, "_tg_chat_id", None)
            if not chat_id:
                logger.debug("Alert %d skip: user has not linked TG chat yet", alert.id)
                continue

            direction_arrow = "▲" if spread_pct >= 0 else "▼"
            mode_label = _MODE_LABEL.get(mode, "Futures L/S")
            url_type = _MODE_URL_TYPE.get(mode, "long-short")
            scope = "any pair" if is_any else "tracked pair"
            tmode_label = "⚡" if trigger_mode == "speed" else "🛡"
            link = f"{base}/arb?symbol={alert.symbol}&long={long_ex}&short={short_ex}&type={url_type}"
            msg = (
                f"🚨 <b>Arb Alert: {alert.symbol}</b> · {mode_label} {tmode_label}\n"
                f"Pair: <b>{long_ex}</b> → <b>{short_ex}</b>\n"
                f"In-spread: <b>{direction_arrow} {spread_pct:+.4f}%</b>"
                f" (threshold ±{alert.threshold}%, {scope})\n"
                f"<a href=\"{link}\">Open arbitrage details →</a>\n"
                f"<i>Alert auto-disabled — re-enable from the bell to get the next ping.</i>"
            )
            # Latency: how stale was the arb data when this fire decision
            # was made?
            import os as _os, time as _t
            cache_age_ms = -1
            try:
                cache_path = f"{_CACHE_DIR}/{_MODE_META.get(mode, _MODE_META['futures'])[0]}"
                cache_age_ms = int((_t.time() - _os.path.getmtime(cache_path)) * 1000)
            except Exception:
                pass

            # Atomically claim the alert BEFORE sending. If another worker
            # already claimed (or user disabled it) — skip silently.
            t_claim_start = _t.monotonic()
            won = _claim_alert_for_fire(alert.id, now)
            claim_ms = int((_t.monotonic() - t_claim_start) * 1000)
            alert.enabled = False
            alert.last_triggered_at = now
            _alerts_cache_ts = 0.0  # force DB refresh next tick
            if not won:
                logger.info("Alert id=%d claim LOST (another worker already fired it)", alert.id)
                continue

            t_send_start = _t.monotonic()
            ok = await _send_tg(str(chat_id), msg)
            send_ms = int((_t.monotonic() - t_send_start) * 1000)
            if ok:
                logger.info(
                    "Alert FIRED id=%d user=%d sym=%s mode=%s pair=%s→%s spread=%.4f%% cache_age=%dms claim=%dms send=%dms",
                    alert.id, alert.user_id, alert.symbol, mode, long_ex, short_ex, spread_pct,
                    cache_age_ms, claim_ms, send_ms,
                )
            else:
                # Fail-closed: alert is already off in DB. User re-enables
                # from the popover when they want the next attempt.
                logger.error(
                    "Alert %d delivery FAILED after claim (user=%d sym=%s) — alert stays disabled, user must re-enable",
                    alert.id, alert.user_id, alert.symbol,
                )
    except Exception as exc:
        logger.error("Alert check error: %s", exc)


async def _alert_loop() -> None:
    while True:
        await _check_alerts()
        await asyncio.sleep(_CHECK_INTERVAL)  # 500ms — yield to event loop, match go-fetcher cycle


def start_alert_service() -> None:
    global _task
    if _task and not _task.done():
        return
    loop = asyncio.get_event_loop()
    _task = loop.create_task(_alert_loop())
    logger.info("Alert service started (check=%.1fs, db_refresh=%.0fs)", _CHECK_INTERVAL, _DB_REFRESH_INTERVAL)


def stop_alert_service() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
