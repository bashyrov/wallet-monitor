"""add users.tg_id + tg_link_tokens table

Revision ID: l8m9n0o1p2q3
Revises: k7l8m9n0o1p2
Create Date: 2026-04-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'l8m9n0o1p2q3'
down_revision = 'k7l8m9n0o1p2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('tg_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_users_tg_id', 'users', ['tg_id'], unique=True)

    op.create_table(
        'tg_link_tokens',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('token_hash', sa.String(), nullable=False, unique=True),   # sha256 hex
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table('tg_link_tokens')
    op.drop_index('ix_users_tg_id', table_name='users')
    op.drop_column('users', 'tg_id')
