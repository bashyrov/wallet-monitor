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
    # 1. Drop old global unique constraint on name
    op.drop_constraint("tags_name_key", "tags", type_="unique")

    # 2. Add user_id column (NULL = system tag)
    op.add_column("tags", sa.Column("user_id", sa.Integer(), nullable=True))

    # 3. Add FK
    op.create_foreign_key(
        "fk_tags_user_id", "tags", "users", ["user_id"], ["id"],
        ondelete="CASCADE",
    )

    # 4. New unique constraint: (name, user_id)
    op.create_unique_constraint("uq_tag_name_user", "tags", ["name", "user_id"])


def downgrade() -> None:
    op.drop_constraint("uq_tag_name_user", "tags", type_="unique")
    op.drop_constraint("fk_tags_user_id", "tags", type_="foreignkey")
    op.drop_column("tags", "user_id")
    op.create_unique_constraint("tags_name_key", "tags", ["name"])
