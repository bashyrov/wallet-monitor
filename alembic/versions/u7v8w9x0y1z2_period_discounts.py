"""Adjust billing-period discounts to match the original pricing page.

Old page: 0% / 10% / 18% / 25% for 1 / 3 / 6 / 12 months. We previously
seeded 0/5/15/25 — bring it back into line so the visual parity is
exact.

Revision ID: u7v8w9x0y1z2
Revises: t6u7v8w9x0y1
Create Date: 2026-04-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'u7v8w9x0y1z2'
down_revision = 't6u7v8w9x0y1'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE billing_periods SET discount_pct = 0  WHERE slug = 'monthly'"))
    bind.execute(sa.text("UPDATE billing_periods SET discount_pct = 10 WHERE slug = 'quarterly'"))
    bind.execute(sa.text("UPDATE billing_periods SET discount_pct = 18 WHERE slug = 'semi_annual'"))
    bind.execute(sa.text("UPDATE billing_periods SET discount_pct = 25 WHERE slug = 'yearly'"))


def downgrade():
    pass
