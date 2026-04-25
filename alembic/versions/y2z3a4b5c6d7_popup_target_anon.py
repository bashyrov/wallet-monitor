"""Popup target_type — split 'all' into authenticated + everyone, add anonymous

Old semantics: target_type='all' meant "all logged-in users" (anon never saw popups).
New semantics:
  - 'authenticated' = logged-in users only (replaces old 'all')
  - 'anonymous'     = logged-out visitors only (NEW)
  - 'everyone'      = both auth + anon (NEW)
  - 'user'          = specific user id (unchanged)

Existing 'all' rows migrate to 'authenticated' to preserve behaviour.

Revision ID: y2z3a4b5c6d7
Revises: x1y2z3a4b5c6
Create Date: 2026-04-25
"""
from alembic import op


revision = 'y2z3a4b5c6d7'
down_revision = 'x1y2z3a4b5c6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Re-stamp legacy 'all' to 'authenticated' so behaviour is unchanged.
    op.execute("UPDATE popups SET target_type='authenticated' WHERE target_type='all'")


def downgrade() -> None:
    # Revert to legacy spelling. 'anonymous' / 'everyone' rows collapse to 'all'
    # — closest legacy match, may broaden audience to logged-in users only.
    op.execute("UPDATE popups SET target_type='all' WHERE target_type IN ('authenticated','everyone','anonymous')")
