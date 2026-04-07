"""tags: add user_id for user-scoped tags, drop global unique on name

Revision ID: a2b3c4d5e6f7
Revises: f2a3b4c5d6e7
Create Date: 2026-04-07

"""
from alembic import op
import sqlalchemy as sa

revision = 'a2b3c4d5e6f7'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tags", recreate="always") as batch_op:
        # Drop old global unique constraint on name
        batch_op.drop_constraint("uq_tags_name", type_="unique") if False else None
        # Add user_id column (NULL = system tag)
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_tags_user_id", "users", ["user_id"], ["id"],
            ondelete="CASCADE",
        )
        # New unique constraint: (name, user_id)
        batch_op.create_unique_constraint("uq_tag_name_user", ["name", "user_id"])


def downgrade() -> None:
    with op.batch_alter_table("tags", recreate="always") as batch_op:
        batch_op.drop_constraint("uq_tag_name_user", type_="unique")
        batch_op.drop_constraint("fk_tags_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")
        batch_op.create_unique_constraint("uq_tags_name", ["name"])
