"""Promo: per-user uses + target_user_id

Two new fields on promo_codes:
  · per_user_max_uses — null = unlimited (current default), 1 = "once per
    user", 2+ = "up to N times per user". Enforced at /promo/validate
    against the existing PromoCodeUsage ledger (already keyed by
    promo_code_id + user_id).
  · target_user_id — null = code applies to anyone (legacy), set = ONLY
    that user can redeem the code. Useful for one-off comp grants.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-04-26
"""
import sqlalchemy as sa
from alembic import op


revision = 'b5c6d7e8f9a0'
down_revision = 'a4b5c6d7e8f9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "promo_codes",
        sa.Column("per_user_max_uses", sa.Integer(), nullable=True),
    )
    op.add_column(
        "promo_codes",
        sa.Column("target_user_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_promo_target_user", "promo_codes", "users",
        ["target_user_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_promo_target_user", "promo_codes", ["target_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_promo_target_user", table_name="promo_codes")
    op.drop_constraint("fk_promo_target_user", "promo_codes", type_="foreignkey")
    op.drop_column("promo_codes", "target_user_id")
    op.drop_column("promo_codes", "per_user_max_uses")
