"""Fix the seed plans: 'full' must have has_portfolio=True.

The previous migration s5t6u7v8w9x0 had a tuple-position bug — the 5th
element was meant to be has_portfolio but for the 'full' plan I wrote
False (the value at position 12 was True for is_full but never used).
Set the right values directly here so existing prod is correct without
needing a re-run of the seed.

Revision ID: t6u7v8w9x0y1
Revises: s5t6u7v8w9x0
Create Date: 2026-04-25
"""
from alembic import op
import sqlalchemy as sa

revision = 't6u7v8w9x0y1'
down_revision = 's5t6u7v8w9x0'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    # 'full' plan must include portfolio access. portfolio_limit was already
    # 30 from the previous seed; just flip the boolean flag.
    bind.execute(sa.text(
        "UPDATE plans SET has_portfolio = TRUE, portfolio_limit = 30 WHERE slug = 'full'"
    ))
    # 'screener' plan correctly has has_portfolio=False — but make sure the
    # portfolio_limit is 0 for it (UI shows the gate and the API returns 402).
    bind.execute(sa.text(
        "UPDATE plans SET has_portfolio = FALSE, portfolio_limit = 0 WHERE slug = 'screener'"
    ))


def downgrade():
    pass
