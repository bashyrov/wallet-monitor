"""API endpoints for the unified Live Trading panel.

POST   /api/trade/arb-orders          create trigger / open / TP / SL
GET    /api/trade/arb-orders          list active (pending|firing|scheduled)
GET    /api/trade/arb-orders/history  list closed (fired|failed|cancelled)
PATCH  /api/trade/arb-orders/{id}     update params (only pending/scheduled)
DELETE /api/trade/arb-orders/{id}     cancel (cascades to children)

GET    /api/trade/arb-positions       list user's arb_positions + nested orders
POST   /api/trade/arb-positions/sync  scan venue state, wrap externally-opened pairs
PATCH  /api/trade/arb-positions/{id}  attach TP/SL to existing arb_position

See DEV_PROMPT.md §7.3 for the full spec.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user, get_db
from backend.db.models import (
    ArbPosition, ArbTriggerOrder, TradePosition, User, Wallet,
)
from backend.services import trade_service
from backend.services.trigger_order_service import (
    _compute_effective_spread, _compute_exit_spread,
    _load_books_json, condition_met,
)


router = APIRouter(prefix="/trade", tags=["trade-arb"])


# ── Request / response schemas ─────────────────────────────────────────────
class TpSlSpec(BaseModel):
    trigger_spread_pct: float = Field(..., description="Absolute spread % threshold")
    portion_size_token: float | None = Field(None, ge=0, description="Null = close full qty")


class ArbOrderCreate(BaseModel):
    kind: Literal["open", "close"]
    pair_kind: Literal["long_short", "spot_short"] = "long_short"
    long_exchange: str
    long_symbol: str
    long_wallet_id: int
    short_exchange: str
    short_symbol: str
    short_wallet_id: int

    trigger_spread_pct: float | None = None        # None = "Last %" = market
    total_qty_token: float = Field(..., gt=0)
    portion_size_token: float | None = Field(None, gt=0)
    infinite_fill: bool = False
    activate_at: datetime | None = None

    leverage: int = Field(1, ge=1, le=125)
    margin_mode: Literal["isolated", "cross"] = "isolated"
    reduce_only: bool = False                      # auto-true for close/tp/sl

    tp: TpSlSpec | None = None
    sl: TpSlSpec | None = None

    force: bool = False                            # bypass immediate-execution check

    # Existing position required for kind='close'
    arb_position_id: int | None = None

    @model_validator(mode="after")
    def _validate(cls, m):
        if m.infinite_fill and not m.portion_size_token:
            raise ValueError("infinite_fill requires portion_size_token")
        if m.portion_size_token and m.portion_size_token > m.total_qty_token:
            raise ValueError("portion_size_token cannot exceed total_qty_token")
        if m.kind == "close" and m.arb_position_id is None:
            raise ValueError("kind='close' requires arb_position_id")
        if m.tp is not None and m.kind != "open":
            raise ValueError("tp can only be attached to an open trigger")
        if m.sl is not None and m.kind != "open":
            raise ValueError("sl can only be attached to an open trigger")
        return m


class ArbOrderOut(BaseModel):
    id: int
    arb_position_id: int | None
    parent_trigger_id: int | None
    kind: str
    trigger_spread_pct: float | None
    long_exchange: str | None
    long_symbol: str | None
    short_exchange: str | None
    short_symbol: str | None
    total_qty_token: float | None
    portion_size_token: float | None
    portions_filled: int
    portions_target: int | None
    infinite_fill: bool
    activate_at: datetime | None
    leverage: int | None
    margin_mode: str
    reduce_only: bool
    status: str
    last_fired_at: datetime | None
    error_kind: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ── Helpers ─────────────────────────────────────────────────────────────────
def _portions_target(total: float | None, portion: float | None) -> int | None:
    if not total or not portion or portion <= 0:
        return 1 if total else None
    return max(1, math.ceil(total / portion))


def _current_effective_spread(body: ArbOrderCreate) -> float | None:
    """Read books.json and compute the kind-appropriate spread:
       open  → in_pct  (entry side)
       close → out_pct (exit side)
    Used for the immediate-execution warning. Always size-aware via VWAP.
    """
    books = _load_books_json()
    if books is None:
        return None
    qty = body.portion_size_token or body.total_qty_token
    fn = _compute_effective_spread if body.kind == "open" else _compute_exit_spread
    return fn(
        books,
        body.long_exchange, body.long_symbol,
        body.short_exchange, body.short_symbol,
        qty,
    )


def _verify_user_owns_wallets(db: Session, user_id: int, *wallet_ids: int) -> None:
    rows = (
        db.query(Wallet.id)
        .filter(Wallet.user_id == user_id, Wallet.id.in_(wallet_ids))
        .all()
    )
    seen = {r[0] for r in rows}
    for wid in wallet_ids:
        if wid not in seen:
            raise HTTPException(404, f"wallet {wid} not found or not owned by user")


def _validate_balance_with_reservations(db: Session, user_id: int, body: ArbOrderCreate) -> None:
    """Reject the create if the wallet's available capital (balance minus
    already-reserved-by-other-pending-triggers) can't back this order's
    notional. Surfaces a 400 with which leg is short and by how much,
    rather than letting the trigger fail silently at fire time.

    Uses the cached balances from trade_service._BALANCES_CACHE — won't
    trigger live venue fetches just for a validation check, so the
    endpoint stays sub-100ms. If cache is empty (cold start, no prior
    /balances fetch) we skip validation; venue would reject at fire
    time and the user sees the structured error in trade_orders.
    """
    from backend.services.trade_service import (
        _BALANCES_CACHE, _pending_open_trigger_reservations,
    )
    from backend.services import price_service

    sym = (body.long_symbol or "").upper()
    mark = float(price_service.price_cache_snapshot().get(sym) or 0)
    if mark <= 0:
        return    # no price → skip; venue catches at fire time

    notional = body.total_qty_token * mark
    leverage = max(1, int(body.leverage or 1))
    long_lev  = 1 if body.pair_kind == "spot_short" else leverage
    long_req  = notional / long_lev
    short_req = notional / leverage

    cached = _BALANCES_CACHE.get(user_id)
    if not cached:
        return    # no cache yet → skip
    rows = cached[1]
    by_wid = {r.get("wallet_id"): r for r in rows if isinstance(r, dict)}
    res = _pending_open_trigger_reservations(db, user_id)

    def _check(side: str, wid: int, req: float):
        row = by_wid.get(wid) or {}
        bal = row.get("balance_usdt")
        if bal is None:
            return
        reserved = float(res.get(wid, 0.0))
        avail = max(0.0, float(bal) - reserved)
        if req > avail + 0.01:
            raise HTTPException(
                400,
                detail={
                    "error": "insufficient_balance",
                    "leg": side,
                    "wallet_id": wid,
                    "exchange": row.get("exchange"),
                    "balance_usdt": bal,
                    "reserved_usdt": round(reserved, 2),
                    "available_usdt": round(avail, 2),
                    "required_usdt": round(req, 2),
                },
            )

    _check("long",  body.long_wallet_id,  long_req)
    _check("short", body.short_wallet_id, short_req)


def _enforce_trigger_limit(db: Session, user_id: int) -> None:
    from backend.db.models import User as _U
    from backend.services import plan_service as _ps

    user = db.query(_U).filter(_U.id == user_id).first()
    if user is None:
        raise HTTPException(401)
    limits = _ps.effective_limits(db, user)
    cap = (getattr(limits, "features", None) or {}).get("max_active_triggers")
    if cap is None:
        cap = 50 if (limits.trade_delay_ms or 0) == 0 else 3   # paid:50, free:3
    if cap == -1:
        return
    active = (
        db.query(ArbTriggerOrder)
        .filter(
            ArbTriggerOrder.user_id == user_id,
            ArbTriggerOrder.status.in_(("pending", "firing", "scheduled")),
        )
        .count()
    )
    if active >= cap:
        raise HTTPException(
            402,
            detail={"error": "trigger_limit_exceeded", "current": active, "limit": cap},
        )


# ── Endpoints ──────────────────────────────────────────────────────────────
@router.post("/arb-orders")
def create_arb_order(
    body: ArbOrderCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verify_user_owns_wallets(db, user.id, body.long_wallet_id, body.short_wallet_id)
    _enforce_trigger_limit(db, user.id)
    # Reservation-aware balance check — only for kind='open' (close/tp/sl
    # reduce existing positions, no new margin needed). Sum existing
    # pending/firing/scheduled triggers' notional + this new request, and
    # fail fast if it exceeds the wallet's actual balance.
    if body.kind == "open":
        _validate_balance_with_reservations(db, user.id, body)

    # Immediate-execution guard — applies to all kinds with a non-null
    # trigger_spread_pct. If condition is already met by current effective
    # spread, return 200 + warning unless force=True.
    #
    # Parent kind (open|close) is checked against its own spread direction
    # via _current_effective_spread. TP/SL nested on a parent open are
    # CLOSE-side conditions, so they're checked against out_pct
    # (exit-side spread) rather than the open's in_pct.
    if not body.force and body.trigger_spread_pct is not None:
        spread = _current_effective_spread(body)
        if spread is not None:
            tmp = ArbTriggerOrder(kind=body.kind,
                                  trigger_spread_pct=body.trigger_spread_pct)
            if condition_met(tmp, spread):
                return {
                    "warning": "immediate_execution",
                    "kind": body.kind,
                    "current_spread": round(spread, 4),
                    "requested_trigger": body.trigger_spread_pct,
                }

        # TP/SL evaluate against the EXIT spread (out_pct) — they fire
        # on what we'd actually receive when closing the position.
        if (body.tp is not None or body.sl is not None):
            books = _load_books_json()
            if books is not None:
                qty = body.portion_size_token or body.total_qty_token
                exit_sp = _compute_exit_spread(
                    books,
                    body.long_exchange, body.long_symbol,
                    body.short_exchange, body.short_symbol,
                    qty,
                )
                if exit_sp is not None:
                    for which, spec in (("tp", body.tp), ("sl", body.sl)):
                        if spec is None:
                            continue
                        tmp = ArbTriggerOrder(kind=which, trigger_spread_pct=spec.trigger_spread_pct)
                        if condition_met(tmp, exit_sp):
                            return {
                                "warning": "immediate_execution",
                                "kind": which,
                                "current_spread": round(exit_sp, 4),
                                "requested_trigger": spec.trigger_spread_pct,
                            }

    initial_status = "scheduled" if body.activate_at and body.activate_at > datetime.utcnow() else "pending"

    order = ArbTriggerOrder(
        user_id=user.id,
        arb_position_id=body.arb_position_id,
        kind=body.kind,
        trigger_spread_pct=body.trigger_spread_pct,
        long_exchange=body.long_exchange,
        long_symbol=body.long_symbol,
        long_wallet_id=body.long_wallet_id,
        short_exchange=body.short_exchange,
        short_symbol=body.short_symbol,
        short_wallet_id=body.short_wallet_id,
        total_qty_token=body.total_qty_token,
        portion_size_token=body.portion_size_token,
        portions_target=_portions_target(body.total_qty_token, body.portion_size_token),
        infinite_fill=body.infinite_fill,
        activate_at=body.activate_at,
        leverage=body.leverage,
        margin_mode=body.margin_mode,
        reduce_only=body.reduce_only or body.kind == "close",
        status=initial_status,
    )
    db.add(order)
    db.flush()

    # Linked TP / SL — created in scheduled state, parent_trigger_id set
    children: list[ArbTriggerOrder] = []
    if body.tp is not None:
        if _existing_child(db, body.arb_position_id, "tp"):
            db.rollback()
            raise HTTPException(409, detail={"error": "tp_already_exists"})
        children.append(_make_child(order, "tp", body.tp))
    if body.sl is not None:
        if _existing_child(db, body.arb_position_id, "sl"):
            db.rollback()
            raise HTTPException(409, detail={"error": "sl_already_exists"})
        children.append(_make_child(order, "sl", body.sl))
    for c in children:
        db.add(c)
    db.commit()
    db.refresh(order)
    _notify_user(user.id)
    return {
        "id": order.id,
        "status": order.status,
        "children": [c.id for c in children],
    }


def _notify_user(user_id: int) -> None:
    """Push a `refresh` event to the user's /ws/positions subscribers."""
    try:
        from backend.api.v1.screener import notify_position_update
        notify_position_update(user_id)
    except Exception:
        pass


def _existing_child(db: Session, arb_position_id: int | None, kind: str) -> bool:
    if arb_position_id is None:
        return False
    return (
        db.query(ArbTriggerOrder.id)
        .filter(
            ArbTriggerOrder.arb_position_id == arb_position_id,
            ArbTriggerOrder.kind == kind,
            ArbTriggerOrder.status.in_(("pending", "firing", "scheduled")),
        )
        .first()
        is not None
    )


def _make_child(parent: ArbTriggerOrder, kind: str, spec: TpSlSpec) -> ArbTriggerOrder:
    return ArbTriggerOrder(
        user_id=parent.user_id,
        parent_trigger_id=parent.id,
        kind=kind,
        trigger_spread_pct=spec.trigger_spread_pct,
        long_exchange=parent.long_exchange,
        long_symbol=parent.long_symbol,
        long_wallet_id=parent.long_wallet_id,
        short_exchange=parent.short_exchange,
        short_symbol=parent.short_symbol,
        short_wallet_id=parent.short_wallet_id,
        portion_size_token=spec.portion_size_token,
        leverage=parent.leverage,
        margin_mode=parent.margin_mode,
        reduce_only=True,
        status="scheduled",     # promoted to 'pending' when parent's first portion fires
    )


@router.get("/arb-orders", response_model=list[ArbOrderOut])
def list_active_orders(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(ArbTriggerOrder)
        .filter(
            ArbTriggerOrder.user_id == user.id,
            ArbTriggerOrder.status.in_(("pending", "firing", "scheduled")),
        )
        .order_by(ArbTriggerOrder.created_at.desc())
        .all()
    )
    return rows


@router.get("/arb-orders/history", response_model=list[ArbOrderOut])
def list_history(
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    limit = max(1, min(200, limit))
    rows = (
        db.query(ArbTriggerOrder)
        .filter(
            ArbTriggerOrder.user_id == user.id,
            ArbTriggerOrder.status.in_(("fired", "failed", "cancelled")),
        )
        .order_by(ArbTriggerOrder.updated_at.desc())
        .limit(limit)
        .all()
    )
    return rows


class ArbOrderPatch(BaseModel):
    trigger_spread_pct: float | None = None
    total_qty_token: float | None = Field(None, gt=0)
    portion_size_token: float | None = Field(None, gt=0)
    infinite_fill: bool | None = None
    activate_at: datetime | None = None
    leverage: int | None = Field(None, ge=1, le=125)
    margin_mode: Literal["isolated", "cross"] | None = None
    force: bool = False


@router.patch("/arb-orders/{order_id}")
def patch_order(
    order_id: int,
    body: ArbOrderPatch,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = (
        db.query(ArbTriggerOrder)
        .filter(ArbTriggerOrder.id == order_id, ArbTriggerOrder.user_id == user.id)
        .first()
    )
    if not order:
        raise HTTPException(404)
    if order.status not in ("pending", "scheduled"):
        raise HTTPException(409, "trigger is firing or already finalized")

    # Immediate-execution check on update — match the kind's spread
    # direction (open=in_pct, tp/sl/close=out_pct).
    if not body.force and body.trigger_spread_pct is not None:
        from backend.services.trigger_order_service import (
            _load_books_json, _compute_effective_spread, _compute_exit_spread,
        )
        books = _load_books_json()
        if books is not None:
            qty = body.portion_size_token or order.portion_size_token or order.total_qty_token
            if qty:
                fn = _compute_effective_spread if order.kind == "open" else _compute_exit_spread
                spread = fn(
                    books,
                    order.long_exchange or "", order.long_symbol or "",
                    order.short_exchange or "", order.short_symbol or "",
                    qty,
                )
                if spread is not None:
                    tmp = ArbTriggerOrder(kind=order.kind, trigger_spread_pct=body.trigger_spread_pct)
                    if condition_met(tmp, spread):
                        return {
                            "warning": "immediate_execution",
                            "kind": order.kind,
                            "current_spread": round(spread, 4),
                            "requested_trigger": body.trigger_spread_pct,
                        }

    fields = body.model_dump(exclude_none=True, exclude={"force"})
    for k, v in fields.items():
        setattr(order, k, v)
    if body.total_qty_token is not None or body.portion_size_token is not None:
        order.portions_target = _portions_target(
            order.total_qty_token, order.portion_size_token,
        )
    order.updated_at = datetime.utcnow()
    db.commit()
    return {"id": order.id, "status": order.status}


@router.delete("/arb-orders/{order_id}")
def cancel_order(
    order_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = (
        db.query(ArbTriggerOrder)
        .filter(ArbTriggerOrder.id == order_id, ArbTriggerOrder.user_id == user.id)
        .first()
    )
    if not order:
        raise HTTPException(404)
    if order.status not in ("pending", "scheduled"):
        raise HTTPException(409, "trigger has already fired or finalized")
    # Cancel children too — DELETE cascade does this on the FK, but mark
    # status explicitly so audit trail is complete.
    db.query(ArbTriggerOrder).filter(
        ArbTriggerOrder.parent_trigger_id == order_id
    ).update({"status": "cancelled", "updated_at": datetime.utcnow()})
    order.status = "cancelled"
    order.updated_at = datetime.utcnow()
    db.commit()
    _notify_user(user.id)
    return {"id": order.id, "status": "cancelled"}


# ── arb_positions ──────────────────────────────────────────────────────────
@router.get("/arb-positions")
def list_arb_positions(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(ArbPosition)
        .filter(ArbPosition.user_id == user.id)
        .order_by(ArbPosition.created_at.desc())
        .all()
    )
    out = []
    for p in rows:
        triggers = (
            db.query(ArbTriggerOrder)
            .filter(ArbTriggerOrder.arb_position_id == p.id)
            .all()
        )
        out.append({
            "id": p.id,
            "kind": p.kind,
            "long_exchange": p.long_exchange, "long_symbol": p.long_symbol,
            "short_exchange": p.short_exchange, "short_symbol": p.short_symbol,
            "long_qty": p.long_qty, "short_qty": p.short_qty,
            "long_entry_price": p.long_entry_price, "short_entry_price": p.short_entry_price,
            "long_exit_price": p.long_exit_price, "short_exit_price": p.short_exit_price,
            "entry_spread_pct": p.entry_spread_pct, "exit_spread_pct": p.exit_spread_pct,
            "realized_pnl_usd": p.realized_pnl_usd,
            "leverage": p.leverage, "margin_mode": p.margin_mode,
            "status": p.status,
            "synced_externally": p.synced_externally, "closed_externally": p.closed_externally,
            "opened_at": p.opened_at, "closed_at": p.closed_at,
            "triggers": [
                {
                    "id": t.id, "kind": t.kind, "status": t.status,
                    "trigger_spread_pct": t.trigger_spread_pct,
                    "portions_filled": t.portions_filled,
                    "portions_target": t.portions_target,
                }
                for t in triggers
            ],
        })
    return out


@router.post("/arb-positions/sync")
async def sync_arb_positions(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Wrap externally-opened pairs into arb_positions so user can attach
    TP/SL. Mirrors auto_pair_internal_legs but operates on TradePosition
    rows from external sources (live venue state).

    For now we use the existing `list_user_spot_short_pairs` + auto_pair
    helpers from trigger_order_service — DEV_PROMPT.md §7.6.G describes
    the full algorithm.
    """
    from backend.services.trigger_order_service import auto_pair_internal_legs

    created = auto_pair_internal_legs(db, user.id)
    return {
        "created": [
            {"id": p.id, "long_exchange": p.long_exchange,
             "short_exchange": p.short_exchange, "symbol": p.long_symbol}
            for p in created
        ],
        "count": len(created),
    }


class AttachTpSl(BaseModel):
    tp: TpSlSpec | None = None
    sl: TpSlSpec | None = None
    force: bool = False


@router.patch("/arb-positions/{position_id}")
def attach_tp_sl(
    position_id: int,
    body: AttachTpSl,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pos = (
        db.query(ArbPosition)
        .filter(ArbPosition.id == position_id, ArbPosition.user_id == user.id)
        .first()
    )
    if not pos:
        raise HTTPException(404)
    if pos.status not in ("open", "partial"):
        raise HTTPException(409, "can only attach TP/SL to open or partial positions")
    if body.tp is None and body.sl is None:
        raise HTTPException(422, "must specify at least one of tp / sl")

    created: list[ArbTriggerOrder] = []
    if body.tp is not None:
        if _existing_child(db, pos.id, "tp"):
            raise HTTPException(409, detail={"error": "tp_already_exists"})
        created.append(_attach_child(pos, "tp", body.tp))
    if body.sl is not None:
        if _existing_child(db, pos.id, "sl"):
            raise HTTPException(409, detail={"error": "sl_already_exists"})
        created.append(_attach_child(pos, "sl", body.sl))
    for c in created:
        db.add(c)
    db.commit()
    return {"position_id": pos.id, "trigger_ids": [c.id for c in created]}


def _attach_child(pos: ArbPosition, kind: str, spec: TpSlSpec) -> ArbTriggerOrder:
    return ArbTriggerOrder(
        user_id=pos.user_id,
        arb_position_id=pos.id,
        kind=kind,
        trigger_spread_pct=spec.trigger_spread_pct,
        long_exchange=pos.long_exchange,
        long_symbol=pos.long_symbol,
        long_wallet_id=pos.long_wallet_id,
        short_exchange=pos.short_exchange,
        short_symbol=pos.short_symbol,
        short_wallet_id=pos.short_wallet_id,
        portion_size_token=spec.portion_size_token,
        leverage=pos.leverage,
        margin_mode=pos.margin_mode or "isolated",
        reduce_only=True,
        status="pending",   # arb_position is already open, no need to schedule
    )
