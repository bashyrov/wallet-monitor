"""arb_alerts: add trigger_mode column (speed/protected)

Revision ID: i2j3k4l5m6n7
Revises: h1i2j3k4l5m6
Create Date: 2026-05-05
"""
from alembic import op
import sqlalchemy as sa

revision = 'i2j3k4l5m6n7'
down_revision = 'h1i2j3k4l5m6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('arb_alerts', sa.Column('trigger_mode', sa.String(), nullable=True))


def downgrade():
    op.drop_column('arb_alerts', 'trigger_mode')
