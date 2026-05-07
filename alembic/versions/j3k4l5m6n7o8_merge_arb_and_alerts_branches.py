"""Merge prod-only alert_mode/trigger_mode branch with sprint arb_positions branch

Revision ID: j3k4l5m6n7o8
Revises: ('i2c3d4e5f6g7', 'i2j3k4l5m6n7')
Create Date: 2026-05-07

Two heads landed simultaneously and need to merge:

  g0a1b2c3d4e5 (payment_refund)
       ├── h1b2c3d4e5f6 (perpdex_purpose_both)
       │     └── i2c3d4e5f6g7 (arb_positions_triggers)         ← sprint branch
       └── h1i2j3k4l5m6 (arb_alerts.mode)
             └── i2j3k4l5m6n7 (arb_alerts.trigger_mode)        ← prod hotfix branch

This merge has no schema changes — it only joins the two heads so
`alembic upgrade head` resolves to a single revision again.
"""
from alembic import op


revision = 'j3k4l5m6n7o8'
down_revision = ('i2c3d4e5f6g7', 'i2j3k4l5m6n7')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
