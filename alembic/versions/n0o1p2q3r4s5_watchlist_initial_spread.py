"""watchlist_items.initial_spread_pct

Revision ID: n0o1p2q3r4s5
Revises: m9n0o1p2q3r4
Create Date: 2026-04-19

"""
from alembic import op
import sqlalchemy as sa

revision = 'n0o1p2q3r4s5'
down_revision = 'm9n0o1p2q3r4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'watchlist_items',
        sa.Column('initial_spread_pct', sa.Float(), nullable=True),
    )


def downgrade():
    op.drop_column('watchlist_items', 'initial_spread_pct')
