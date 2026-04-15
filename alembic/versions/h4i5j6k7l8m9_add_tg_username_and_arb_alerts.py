"""add tg_username to users and arb_alerts table

Revision ID: h4i5j6k7l8m9
Revises: g3h4i5j6k7l8
Create Date: 2026-04-14 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'h4i5j6k7l8m9'
down_revision = 'g3h4i5j6k7l8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('tg_username', sa.String(), nullable=True))

    op.create_table(
        'arb_alerts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('long_exchange', sa.String(), nullable=False),
        sa.Column('short_exchange', sa.String(), nullable=False),
        sa.Column('threshold', sa.Float(), nullable=False),   # min spread % to trigger
        sa.Column('direction', sa.String(), nullable=False, server_default='any'),  # 'any'|'above'|'below'
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('last_triggered_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table('arb_alerts')
    op.drop_column('users', 'tg_username')
