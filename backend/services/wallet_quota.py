"""Wallet-quota housekeeping — auto-archive surplus wallets when a user
either downgrades to a smaller plan or lets a paid plan expire.

Logic:
  · Compare the user's current wallet inventory (active + portfolio
    purpose) against `plan_service.effective_limits(...)`.
  · If a numeric portfolio_limit applies and the user has more
    active portfolio wallets than the limit, archive the oldest-first
    surplus until the count matches. We keep the newest because that's
    most likely what the user cares about right now; admin / user can
    restore from /archive afterwards.
  · The exchange-key cap is INFORMATIONAL. We never auto-archive screener
    keys because they cost the user real exchange-side state (open
    positions, orders) — leave those for the user to clean up.
  · Idempotent — calling it on a compliant account does nothing.

Triggers:
  · Called from `/api/auth/me` once per request (cheap query) so a stale
    plan downgrade gets detected lazily without needing a cron job.
  · Called from the CryptoCloud webhook AFTER plan_id flip, so an
    upgrade DOES NOT touch the archive but a downgrade kicks in
    immediately.
  · Admin can call it explicitly from /api/admin/users/{id}/enforce-quota.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from backend.db.models import User, Wallet
from backend.services import plan_service

logger = logging.getLogger("avalant.wallet_quota")


def _portfolio_wallets(db: Session, user_id: int) -> list[Wallet]:
    return (
        db.query(Wallet)
        .filter(
            Wallet.user_id == user_id,
            Wallet.is_archived == False,  # noqa: E712
            Wallet.purpose.in_(("portfolio", "both")),
        )
        .order_by(Wallet.created_at.asc())
        .all()
    )


def enforce_for_user(db: Session, user: User, *, dry_run: bool = False) -> dict:
    """Archive surplus portfolio wallets if the user is over their cap.

    Returns a small report dict for logging / API responses.
    """
    limits = plan_service.effective_limits(db, user)
    if limits.portfolio_unlimited:
        return {"changed": 0, "reason": "unlimited"}
    cap = limits.portfolio_limit
    wallets = _portfolio_wallets(db, user.id)
    if len(wallets) <= cap:
        return {"changed": 0, "reason": "within_cap", "count": len(wallets), "cap": cap}

    surplus = wallets[: len(wallets) - cap]
    if dry_run:
        return {
            "changed": 0,
            "reason": "dry_run",
            "would_archive": [w.id for w in surplus],
            "kept": cap,
        }
    for w in surplus:
        w.is_archived = True
        # `screener` purpose: leave alone (user might have the same
        # wallet doing both — unset its portfolio role instead). For
        # our current schema purpose='both' rows downgrade to
        # 'screener' and stay active; pure 'portfolio' rows just get
        # archived.
        if w.purpose == "both":
            w.purpose = "screener"
            w.is_archived = False
    db.commit()
    logger.warning(
        "auto-archived %d portfolio wallets for user_id=%s (cap=%d, plan=%s, expired=%s)",
        len([w for w in surplus if w.is_archived]),
        user.id, cap, limits.plan_slug, limits.is_expired,
    )
    return {
        "changed": len([w for w in surplus if w.is_archived]),
        "downgraded_to_screener": len([w for w in surplus if not w.is_archived]),
        "cap": cap,
        "plan": limits.plan_slug,
        "expired": limits.is_expired,
    }


def enforce_for_users(db: Session, users: Iterable[User]) -> dict:
    """Bulk variant — used by admin sweep / nightly cron."""
    summary = {"users_touched": 0, "wallets_archived": 0}
    for u in users:
        report = enforce_for_user(db, u)
        if report.get("changed"):
            summary["users_touched"] += 1
            summary["wallets_archived"] += report["changed"]
    return summary
