"""refresh plan.features perks/limits to match the $33 gating rebase

The plan-editor perks/limits JSON drives the bullet lists on /pricing
cards. Those still carried the pre-t3u4v5w6x7y8 copy ("3 keys per
venue", no mention of TP/SL, etc.) — this migration rewrites them so
what buyers see on the marketing page matches what the trade engine
actually enforces.

Revision ID: v5w6x7y8z9a0
Revises: u4v5w6x7y8z9
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa
import json


revision = 'v5w6x7y8z9a0'
down_revision = 'u4v5w6x7y8z9'
branch_labels = None
depends_on = None


_FREE_FEATURES = {
    "perks": [
        "Live screener across every listed venue",
        "5 portfolio wallets",
        "1 API key per venue",
    ],
    "limits": [
        "500ms trade delay on every order",
        "Entry spread capped at 10%",
        "No TP / SL orders",
    ],
}

_SCREENER_FEATURES = {
    "perks": [
        "Everything in Free",
        "Unlimited API keys per venue",
        "0ms trade delay",
        "No entry-spread cap",
        "TP / SL orders",
        "Priority support",
    ],
    "limits": [
        "No portfolio (add Portfolio via the Full plan)",
    ],
}

_FULL_FEATURES = {
    "perks": [
        "Everything in Screener",
        "30 portfolio wallets",
        "Portfolio balance history + charts",
        "Cross-venue P&L reconcile",
    ],
    "limits": [],
}

_UNLIM_FEATURES = {
    "perks": [
        "Everything in Full",
        "Unlimited wallets",
        "Admin-only tier",
    ],
    "limits": [],
}


def _apply(bind, slug: str, features: dict) -> None:
    bind.execute(
        sa.text("UPDATE plans SET features = :f WHERE slug = :s"),
        {"f": json.dumps(features), "s": slug},
    )


def upgrade() -> None:
    bind = op.get_bind()
    _apply(bind, "free", _FREE_FEATURES)
    _apply(bind, "screener", _SCREENER_FEATURES)
    _apply(bind, "full", _FULL_FEATURES)
    _apply(bind, "unlim", _UNLIM_FEATURES)


def downgrade() -> None:
    # No downgrade — the pre-existing features JSON was hand-edited
    # per plan and there's no clean rollback target. Admin can restore
    # via /admin → Plans → Edit if needed.
    pass
