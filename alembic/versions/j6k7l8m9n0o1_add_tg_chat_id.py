"""add tg_chat_id to users

Revision ID: j6k7l8m9n0o1
Revises: i5j6k7l8m9n0
Create Date: 2026-04-16 11:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'j6k7l8m9n0o1'
down_revision = 'i5j6k7l8m9n0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('tg_chat_id', sa.BigInteger(), nullable=True))


def downgrade():
    op.drop_column('users', 'tg_chat_id')
