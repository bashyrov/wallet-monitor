"""Promo bonus_days — promo can grant extra subscription days

Old semantics: promo always discounted the cart price by `discount_pct`.
New: a promo can ALSO (or INSTEAD) grant N bonus days that extend the
user's plan_expires_at when the payment activates. e.g. EARLY7 = 0%
discount + 7 bonus days.

Validation moves to: discount_pct > 0 OR bonus_days > 0 (both is fine).

Revision ID: z3a4b5c6d7e8
Revises: y2z3a4b5c6d7
Create Date: 2026-04-25
"""
import sqlalchemy as sa
from alembic import op


revision = 'z3a4b5c6d7e8'
down_revision = 'y2z3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "promo_codes",
        sa.Column("bonus_days", sa.Integer(), nullable=False, server_default="0"),
    )
    # Ditch the server_default once the column is populated — fresh inserts
    # use the SQLAlchemy default in the model (0).
    op.alter_column("promo_codes", "bonus_days", server_default=None)


def downgrade() -> None:
    op.drop_column("promo_codes", "bonus_days")
