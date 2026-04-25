"""Subscription cancel + expiry-notification throttle

Adds two columns to `users`:
  · auto_renew                — false = user clicked Cancel; we stop sending
                                expiry reminders and don't auto-bill on
                                renewal. Plan still runs until plan_expires_at.
  · expiry_notice_last_sent_at — last timestamp the expiry-notifier successfully
                                pushed a TG message for this user. Throttle key
                                so a daemon-restart doesn't re-send a fresh
                                "expires in 2 days" 30 minutes after the last.

Revision ID: a4b5c6d7e8f9
Revises: z3a4b5c6d7e8
Create Date: 2026-04-25
"""
import sqlalchemy as sa
from alembic import op


revision = 'a4b5c6d7e8f9'
down_revision = 'z3a4b5c6d7e8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("users", "auto_renew", server_default=None)
    op.add_column(
        "users",
        sa.Column("expiry_notice_last_sent_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "expiry_notice_last_sent_at")
    op.drop_column("users", "auto_renew")
