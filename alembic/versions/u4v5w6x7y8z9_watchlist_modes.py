"""watchlist_items mode + dex fields

Revision ID: u4v5w6x7y8z9
Revises: t3u4v5w6x7y8
Create Date: 2026-07-04

"""
from alembic import op
import sqlalchemy as sa

revision = 'u4v5w6x7y8z9'
down_revision = 't3u4v5w6x7y8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'watchlist_items',
        sa.Column('mode', sa.String(), nullable=False, server_default='long-short'),
    )
    op.add_column('watchlist_items', sa.Column('dex_chain', sa.String(), nullable=True))
    op.add_column('watchlist_items', sa.Column('dex_address', sa.String(), nullable=True))
    op.add_column('watchlist_items', sa.Column('dex_pair', sa.String(), nullable=True))


def downgrade():
    op.drop_column('watchlist_items', 'dex_pair')
    op.drop_column('watchlist_items', 'dex_address')
    op.drop_column('watchlist_items', 'dex_chain')
    op.drop_column('watchlist_items', 'mode')
