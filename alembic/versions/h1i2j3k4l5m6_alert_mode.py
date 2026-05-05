"""arb_alerts: add mode column (futures/spot/dex)

Revision ID: h1i2j3k4l5m6
Revises: g0a1b2c3d4e5
Create Date: 2026-05-05
"""
from alembic import op
import sqlalchemy as sa

revision = 'h1i2j3k4l5m6'
down_revision = 'g0a1b2c3d4e5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('arb_alerts', sa.Column('mode', sa.String(), nullable=True))


def downgrade():
    op.drop_column('arb_alerts', 'mode')
