"""Per-account failed-login counter for the auto-lockout

Adds users.failed_login_attempts (int, default 0). The login handler
increments it on every wrong password and resets it on success.
At LOGIN_LOCK_THRESHOLD (5) the account is_blocked is flipped and
the user has to contact support.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-04-26
"""
import sqlalchemy as sa
from alembic import op


revision = 'c6d7e8f9a0b1'
down_revision = 'b5c6d7e8f9a0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("users", "failed_login_attempts", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "failed_login_attempts")
