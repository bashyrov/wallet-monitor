"""balance_history.totals JSON for per-asset breakdown

Adds a JSON column to balance_history so the portfolio chart can show
the asset composition at the hovered snapshot time. Shape of totals:
  {symbol: usd_value_float, ...}  e.g. {"BTC": 3421.55, "ETH": 812.10}
Nullable — old rows stay as-is, the chart tooltip just falls back to
showing the aggregate for historical points.

Revision ID: p2q3r4s5t6u7
Revises: o1p2q3r4s5t6
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa

revision = 'p2q3r4s5t6u7'
down_revision = 'o1p2q3r4s5t6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'balance_history',
        sa.Column('totals', sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column('balance_history', 'totals')
