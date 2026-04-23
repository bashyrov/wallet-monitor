"""add email_verified_at + email_verify_tokens

Revision ID: ev1a2b3c4d5f
Revises: pr1a2b3c4d5e
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa


revision = "ev1a2b3c4d5f"
down_revision = "pr1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime, nullable=True))
    op.create_table(
        "email_verify_tokens",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("token_hash", sa.String, nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime, nullable=False, index=True),
        sa.Column("used_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("email_verify_tokens")
    op.drop_column("users", "email_verified_at")
