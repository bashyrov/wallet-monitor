"""add provider_error_logs table

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-04-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'e1f2a3b4c5d6'
down_revision = 'd0e1f2a3b4c5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'provider_error_logs',
        sa.Column('id',          sa.Integer(),  primary_key=True),
        sa.Column('wallet_type', sa.String(),   nullable=False),
        sa.Column('type_value',  sa.String(),   nullable=False),
        sa.Column('error_type',  sa.String(),   nullable=False),
        sa.Column('created_at',  sa.DateTime(), nullable=True),
    )
    op.create_index('ix_provider_error_logs_created_at', 'provider_error_logs', ['created_at'])


def downgrade():
    op.drop_index('ix_provider_error_logs_created_at', 'provider_error_logs')
    op.drop_table('provider_error_logs')
