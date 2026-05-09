"""Live trading endpoints — real order placement on supported exchanges."""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,16}$")


def _vsym(v):
    s = str(v or "").strip().upper()
    if not _SYMBOL_RE.match(s):
        raise ValueError("symbol must be 1-16 alphanumeric uppercase")
    return s

from backend.api.deps import get_current_user, get_db
from backend.db.models import User, Wallet
from backend.services import trade_service
from backend.services.trade_adapters import SUPPORTED_EXCHANGES

router = APIRouter(prefix="/trade", tags=["trade"])
logger = logging.getLogger("avalant.trade")


# ── Read ──────────────────────────────────────────────────────────────────────
@router.get("/status")
async def pair_status(
    symbol: str = Query(..., pattern=r"^[A-Za-z0-9]{1,16}$"),
    long_ex: str = Query(..., min_length=2, max_length=24),
    short_ex: str = Query(..., min_length=2, max_length=24),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await trade_service.get_pair_status(db, user.id, symbol.upper(), long_ex.lower(), short_ex.lower())


@router.get("/positions")
async def positions(
    symbol: str | None = Query(None, pattern=r"^[A-Za-z0-9]{1,16}$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await trade_service.list_user_positions(db, user.id, symbol.upper() if symbol else None)


@router.get("/balances")
async def balances(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """USDT balance per screener-attached exchange wallet — drives the
    Balances tab on /arb. Portfolio-only wallets aren't included; this
    is the per-key trading view, not a portfolio aggregate."""
    return await trade_service.list_user_balances(db, user.id)


@router.get("/orders")
async def orders(
    symbol: str | None = Query(None, pattern=r"^[A-Za-z0-9]{1,16}$"),
    limit: int = Query(50, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Order History — every order our service sent to a venue for this
    user. Used by the Order History tab on /arb. Internal errors are
    sanitized; exchange errors are surfaced verbatim."""
    return await trade_service.list_user_orders(db, user.id, limit=limit, symbol=symbol)


@router.get("/pnl")
def pnl(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """P&L tab — closed positions over the last `days` days. Pair-eligible
    closed singles are grouped via the user's pair decisions or the
    spread%±5% / 5-min-window auto rule. Partial-closed pairs are filtered
    out — those still belong on the live Positions tab."""
    return trade_service.list_user_pnl(db, user.id, days=days)


# ── Fills backfill — pulls closed positions from each venue ────────────────
@router.post("/pnl/sync")
async def pnl_sync(
    user: User = Depends(get_current_user),
):
    """Kick off a fills backfill across every trade-enabled wallet for the
    last 7 days. Returns immediately; the actual sync runs in the
    background. A Redis lock prevents concurrent syncs across replicas.
    Poll GET /pnl/sync to check status."""
    import asyncio as _asyncio
    from backend.services import fills_backfill_service
    from backend.services.rate_limit import _get_redis

    rds = _get_redis()
    lock_key = f"pnl_sync_lock:{user.id}"
    if rds is not None:
        # Try to acquire — TTL 5 min so a crashed worker doesn't permanently
        # block the user. NX = only set if not exists.
        try:
            ok = rds.set(lock_key, "1", nx=True, ex=300)
        except Exception:
            ok = True  # treat redis blip as "lock free" — best effort
    else:
        ok = True
    if not ok:
        return {"syncing": True, "in_progress": True}

    async def _run():
        try:
            res = await fills_backfill_service.sync_user(user.id)
            logger.info("pnl_sync: user=%s result=%s", user.id, res)
        except Exception as exc:  # noqa: BLE001
            logger.exception("pnl_sync: user=%s failed: %s", user.id, exc)
        finally:
            if rds is not None:
                try:
                    rds.delete(lock_key)
                except Exception:
                    pass

    _asyncio.create_task(_run())
    return {"syncing": True, "in_progress": False}


@router.get("/pnl/sync")
def pnl_sync_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Status of the user's last fills backfill. Returns the most recent
    `last_synced_at` across cursors and whether a sync is currently
    holding the Redis lock."""
    from backend.db.models import FillsSyncCursor
    from backend.services.rate_limit import _get_redis

    rds = _get_redis()
    in_progress = False
    if rds is not None:
        try:
            in_progress = bool(rds.get(f"pnl_sync_lock:{user.id}"))
        except Exception:
            in_progress = False

    last = (
        db.query(FillsSyncCursor.last_synced_at)
        .filter(FillsSyncCursor.user_id == user.id)
        .order_by(FillsSyncCursor.last_synced_at.desc())
        .first()
    )
    return {
        "in_progress": in_progress,
        "last_synced_at": last[0].isoformat() if last and last[0] else None,
    }


@router.get("/spot-short-pairs")
async def spot_short_pairs(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Spot/short pair candidates — for every open short futures position,
    surface matching spot holdings (notional within ±5%) so basis traders
    see the implicit pair as a single position. Frontend renders these
    alongside long/short pairs on /arb."""
    return await trade_service.list_user_spot_short_pairs(db, user.id)


class SpotShortPairIn(BaseModel):
    symbol: str
    spot_wallet_id: int = Field(..., gt=0)
    short_exchange: str = Field(..., min_length=2, max_length=24)
    short_wallet_id: int = Field(..., gt=0)

    @field_validator("symbol", mode="before")
    @classmethod
    def _v(cls, v): return _vsym(v)


@router.post("/pair/spot-short/sync")
def spot_short_pair_sync(
    body: SpotShortPairIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        trade_service.set_spot_short_pair_decision(
            db, user.id, body.symbol,
            body.spot_wallet_id, body.short_exchange, body.short_wallet_id,
            decision="paired",
        )
    except trade_service.TradeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/pair/spot-short/unsync")
def spot_short_pair_unsync(
    body: SpotShortPairIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        trade_service.set_spot_short_pair_decision(
            db, user.id, body.symbol,
            body.spot_wallet_id, body.short_exchange, body.short_wallet_id,
            decision="unpaired",
        )
    except trade_service.TradeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ── Pair decisions ──────────────────────────────────────────────────────────
class PairIn(BaseModel):
    symbol: str
    long_exchange: str = Field(..., min_length=2, max_length=24)
    short_exchange: str = Field(..., min_length=2, max_length=24)

    @field_validator("symbol", mode="before")
    @classmethod
    def _v(cls, v): return _vsym(v)


@router.get("/pair/decisions")
async def pair_decisions(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Active 'paired' decisions for the user — drives the manual-pair list
    in the Sync ⇆ modal, replacing the legacy localStorage cache.

    Pairs whose underlying legs are both closed are filtered out so the
    Sync dialog doesn't make the user click 'Unpair' on a position that
    no longer exists. We pull live positions (cached) and pass them down
    for the cross-reference."""
    live = await trade_service.list_user_positions(db, user.id)
    return trade_service.list_pair_decisions(db, user.id, live_positions=live)


@router.post("/pair/sync")
def pair_sync(
    body: PairIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        trade_service.set_pair_decision(
            db, user.id, body.symbol, body.long_exchange, body.short_exchange,
            decision="paired",
        )
    except trade_service.TradeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/pair/unsync")
def pair_unsync(
    body: PairIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        trade_service.set_pair_decision(
            db, user.id, body.symbol, body.long_exchange, body.short_exchange,
            decision="unpaired",
        )
    except trade_service.TradeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/supported")
def supported_exchanges():
    from backend.services.trade_adapters import TRADE_SUPPORTED
    return {
        "trade": sorted(TRADE_SUPPORTED),
        "read_only": sorted(SUPPORTED_EXCHANGES - TRADE_SUPPORTED),
    }


@router.get("/leverage-limits")
async def leverage_limits(
    symbol: str = Query(...),
    long_ex: str = Query(...),
    short_ex: str = Query(...),
    _: User = Depends(get_current_user),
):
    """Public-only max leverage + qty limits per leg — no API key required.
    Trading panel uses this to:
      • cap the leverage stepper at venue's real max
      • render an inline "min: 0.01 SPACEX, step: 0.001" hint under qty
      • client-side reject sub-min orders before they hit the API
    """
    import asyncio
    from backend.services.trade_adapters import ADAPTERS, SUPPORTED_EXCHANGES

    async def _probe_lev(ex: str) -> int | None:
        if ex not in SUPPORTED_EXCHANGES:
            return None
        adapter = ADAPTERS[ex]
        if not hasattr(adapter, "get_public_max_leverage"):
            return None
        try:
            return await adapter.get_public_max_leverage(symbol)
        except Exception:
            return None

    async def _probe_qty(ex: str) -> dict | None:
        if ex not in SUPPORTED_EXCHANGES:
            return None
        adapter = ADAPTERS[ex]
        if not hasattr(adapter, "get_public_qty_limits"):
            return None
        try:
            return await adapter.get_public_qty_limits(symbol)
        except Exception:
            return None

    long_max, short_max, long_qty, short_qty = await asyncio.gather(
        _probe_lev(long_ex.lower()),  _probe_lev(short_ex.lower()),
        _probe_qty(long_ex.lower()),  _probe_qty(short_ex.lower()),
    )
    return {
        "symbol": symbol,
        "long":  {"exchange": long_ex.lower(),  "max_leverage": long_max,  "qty_limits": long_qty},
        "short": {"exchange": short_ex.lower(), "max_leverage": short_max, "qty_limits": short_qty},
    }


class OpenArbIn(BaseModel):
    symbol: str
    long_wallet_id: int
    long_quantity: float = Field(..., gt=0, le=1_000_000)
    long_leverage: int = Field(3, ge=1, le=125)
    long_margin_mode: str = Field("isolated", pattern="^(isolated|cross)$")
    short_wallet_id: int
    short_quantity: float = Field(..., gt=0, le=1_000_000)
    short_leverage: int = Field(3, ge=1, le=125)
    short_margin_mode: str = Field("isolated", pattern="^(isolated|cross)$")

    @field_validator("symbol", mode="before")
    @classmethod
    def _v(cls, v): return _vsym(v)


@router.post("/open-arb")
async def open_arb(
    body: OpenArbIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fires both legs (long on long_wallet, short on short_wallet) in parallel.
    Returns per-leg success/error so the client can show exactly which side
    landed if one fails."""
    import asyncio
    if body.long_wallet_id == body.short_wallet_id:
        raise HTTPException(400, "long_wallet_id and short_wallet_id must differ")

    async def _one(leg: str, wid: int, qty: float, lev: int, mode: str, side: str):
        try:
            r = await trade_service.place_open_order(
                db, user.id, wid, body.symbol, side, qty, lev, mode,
            )
            return {"leg": leg, "ok": True, **r}
        except trade_service.TradeError as e:
            msg = "Unexpected error — see Order History" if e.kind == "internal" else str(e)
            return {"leg": leg, "ok": False, "error": msg}
        except Exception as e:
            logger.exception("open-arb %s unexpected: %s", leg, e)
            return {"leg": leg, "ok": False, "error": "Unexpected error — see Order History"}

    long_res, short_res = await asyncio.gather(
        _one("long",  body.long_wallet_id,  body.long_quantity,
             body.long_leverage,  body.long_margin_mode,  "buy"),
        _one("short", body.short_wallet_id, body.short_quantity,
             body.short_leverage, body.short_margin_mode, "sell"),
    )
    return {"long": long_res, "short": short_res,
            "fully_filled": long_res["ok"] and short_res["ok"]}


# ── Write ─────────────────────────────────────────────────────────────────────
class OpenOrderIn(BaseModel):
    wallet_id: int
    symbol: str
    side: str = Field(..., pattern="^(buy|sell)$")
    quantity: float = Field(..., gt=0, le=1_000_000)
    leverage: int = Field(3, ge=1, le=125)
    margin_mode: str = Field("isolated", pattern="^(isolated|cross)$")

    @field_validator("symbol", mode="before")
    @classmethod
    def _v(cls, v): return _vsym(v)


@router.post("/open")
async def open_order(
    body: OpenOrderIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return await trade_service.place_open_order(
            db, user.id, body.wallet_id, body.symbol, body.side, body.quantity,
            body.leverage, body.margin_mode,
        )
    except trade_service.TradeError as e:
        # Internal errors are sanitized so we don't leak internals to the
        # client. The truth is in trade_orders for support to see.
        msg = "Unexpected error — see Order History" if e.kind == "internal" else str(e)
        raise HTTPException(400, msg)
    except Exception as e:
        logger.exception("open order unexpected uid=%s wid=%s: %s", user.id, body.wallet_id, e)
        raise HTTPException(500, "Unexpected error — see Order History")


class CloseIn(BaseModel):
    wallet_id: int
    symbol: str
    side: str | None = Field(None, pattern="^(buy|sell)$")

    @field_validator("symbol", mode="before")
    @classmethod
    def _v(cls, v): return _vsym(v)


@router.post("/close")
async def close_order(
    body: CloseIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return await trade_service.close_position(db, user.id, body.wallet_id, body.symbol, body.side)
    except trade_service.TradeError as e:
        msg = "Unexpected error — see Order History" if e.kind == "internal" else str(e)
        raise HTTPException(400, msg)
    except Exception as e:
        logger.exception("close unexpected uid=%s wid=%s: %s", user.id, body.wallet_id, e)
        raise HTTPException(500, "Unexpected error — see Order History")


# ── Enable/disable trading on a wallet (switch purpose) ──────────────────────
class ToggleIn(BaseModel):
    can_trade: bool


@router.patch("/wallets/{wallet_id}")
def toggle_can_trade(
    wallet_id: int,
    body: ToggleIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from backend.db.models import Wallet as W
    w = db.query(Wallet).filter(Wallet.id == wallet_id, Wallet.user_id == user.id).first()
    if not w:
        raise HTTPException(404, "Wallet not found")
    if w.wallet_type not in ("exchange", "perpdex"):
        raise HTTPException(400, "Trading can only be enabled on exchange or perpdex wallets")
    if body.can_trade:
        from backend.services.trade_adapters import TRADE_SUPPORTED
        if w.type_value not in TRADE_SUPPORTED:
            raise HTTPException(400, f"Trading on {w.type_value} is not yet supported.")
        # If user is enabling trade on a perpdex wallet that lacks the
        # private-key creds, we fail fast with a clear message rather than
        # let the first trade silently fail at signing time.
        if w.wallet_type == "perpdex":
            from backend.crypto import decrypt_credentials
            creds = decrypt_credentials(w.credentials or {})
            tv = (w.type_value or "").lower()
            missing = []
            if tv in ("hyperliquid", "ethereal") and not (creds.get("api_secret") or creds.get("private_key")):
                missing.append("private_key")
            if tv == "paradex" and not creds.get("private_key"):
                missing.append("l2_private_key")
            if tv == "lighter":
                if not creds.get("api_key"):
                    missing.append("account_index")
                if not creds.get("api_secret"):
                    missing.append("private_key")
            if missing:
                raise HTTPException(
                    400,
                    f"{w.type_value} trade requires {', '.join(missing)} — edit the wallet to add them"
                )
        # Only one trading-eligible (screener|both) key per venue per user
        dup = (
            db.query(W)
            .filter(
                W.user_id == user.id,
                W.wallet_type == w.wallet_type,
                W.type_value == w.type_value,
                W.purpose.in_(("screener", "both")),
                W.id != w.id,
                W.is_archived == False,  # noqa: E712
            )
            .first()
        )
        if dup:
            raise HTTPException(409, f"A screener-eligible key for {w.type_value} already exists. Switch it off first.")
        # If it was 'portfolio' before, preserve the portfolio role too → 'both'.
        # If it was already 'screener' (or 'both'), nothing to do.
        w.purpose = "both" if w.purpose == "portfolio" else ("both" if w.purpose == "both" else "screener")
        w.can_trade = True
    else:
        # Flipping screener off: if key was 'both', keep portfolio; else plain portfolio
        w.purpose = "portfolio"
        w.can_trade = False
    db.commit()
    return {"id": w.id, "can_trade": w.can_trade, "purpose": w.purpose}
