"""tags: add user_id for user-scoped tags, drop global unique on name

Revision ID: a2b3c4d5e6f7
Revises: f2a3b4c5d6e7
Create Date: 2026-04-07

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text, inspect

revision = 'a2b3c4d5e6f7'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "sqlite":
        # SQLite: rebuild table via batch mode (no ALTER CONSTRAINT support)
        with op.batch_alter_table("tags", recreate="always") as batch_op:
            batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
            # Drop old unique on name, create new composite unique
            batch_op.create_unique_constraint("uq_tag_name_user", ["name", "user_id"])
        # Assign existing non-system tags to first user
        conn.execute(text(
            "UPDATE tags SET user_id = (SELECT id FROM users ORDER BY id ASC LIMIT 1) "
            "WHERE name NOT IN ('Owner')"
        ))
    else:
        # PostgreSQL: full ALTER TABLE support
        op.drop_constraint("tags_name_key", "tags", type_="unique")
        op.add_column("tags", sa.Column("user_id", sa.Integer(), nullable=True))
        conn.execute(text(
            "UPDATE tags SET user_id = (SELECT id FROM users ORDER BY id ASC LIMIT 1) "
            "WHERE name NOT IN ('Owner')"
        ))
        op.create_foreign_key(
            "fk_tags_user_id", "tags", "users", ["user_id"], ["id"],
            ondelete="CASCADE",
        )
        op.create_unique_constraint("uq_tag_name_user", "tags", ["name", "user_id"])


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "sqlite":
        with op.batch_alter_table("tags", recreate="always") as batch_op:
            batch_op.drop_column("user_id")
            batch_op.create_unique_constraint("tags_name_key", ["name"])
    else:
        op.drop_constraint("uq_tag_name_user", "tags", type_="unique")
        op.drop_constraint("fk_tags_user_id", "tags", type_="foreignkey")
        op.drop_column("tags", "user_id")
        op.create_unique_constraint("tags_name_key", "tags", ["name"])
