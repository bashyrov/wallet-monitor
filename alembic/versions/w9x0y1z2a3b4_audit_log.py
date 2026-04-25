"""audit_log table — admin / billing destructive action ledger.

Append-only, never UPDATE, never DELETE. Every row records:
  · who did it (actor_user_id)
  · against whom or what (target_type, target_id)
  · the kind of action (action) — eg 'plan.create', 'promo.delete',
    'user.block', 'wallet.archive'
  · before/after diff (delta JSON) so we can reconstruct intent
  · request context (ip, user_agent) for incident response

Revision ID: w9x0y1z2a3b4
Revises: v8w9x0y1z2a3
Create Date: 2026-04-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'w9x0y1z2a3b4'
down_revision = 'v8w9x0y1z2a3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True, index=True),
        sa.Column("actor_ip", sa.String(), nullable=True),
        sa.Column("actor_user_agent", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False, index=True),
        sa.Column("target_type", sa.String(), nullable=True, index=True),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("delta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )


def downgrade():
    op.drop_index("ix_audit_log_actor_user_id", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_target_type", table_name="audit_log")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_table("audit_log")
