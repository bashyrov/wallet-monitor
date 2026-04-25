"""rebase pricing: Screener $45/mo, Full $55/mo (+$10 for portfolio)

User requested cheaper entry — drop the base $10. Differential between
Screener and Full stays at $10/mo (the portfolio uplift).

Revision ID: v8w9x0y1z2a3
Revises: u7v8w9x0y1z2
Create Date: 2026-04-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'v8w9x0y1z2a3'
down_revision = 'u7v8w9x0y1z2'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE plans SET price_usd_monthly = 45 WHERE slug = 'screener'"))
    bind.execute(sa.text("UPDATE plans SET price_usd_monthly = 55 WHERE slug = 'full'"))


def downgrade():
    pass
