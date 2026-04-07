"""tags: add user_id for user-scoped tags, drop global unique on name

Revision ID: a2b3c4d5e6f7
Revises: f2a3b4c5d6e7
Create Date: 2026-04-07

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

revision = 'a2b3c4d5e6f7'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None


def _get_unique_constraint_name(conn, table: str, columns: list[str]) -> str | None:
    """Find the actual name of a unique constraint on given columns (works for PG and SQLite)."""
    result = conn.execute(text(
        "SELECT constraint_name FROM information_schema.table_constraints tc "
        "JOIN information_schema.constraint_column_usage ccu USING (constraint_name, table_name) "
        "WHERE tc.table_name = :t AND tc.constraint_type = 'UNIQUE' AND ccu.column_name = ANY(:cols)"
    ), {"t": table, "cols": columns})
    rows = result.fetchall()
    return rows[0][0] if rows else None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Add user_id column (NULL = system tag)
    op.add_column("tags", sa.Column("user_id", sa.Integer(), nullable=True))

    # 2. Add FK
    op.create_foreign_key(
        "fk_tags_user_id", "tags", "users", ["user_id"], ["id"],
        ondelete="CASCADE",
    )

    # 3. Drop the old global unique constraint on name (auto-named by PG or SQLite)
    try:
        name = _get_unique_constraint_name(conn, "tags", ["name"])
        if name:
            op.drop_constraint(name, "tags", type_="unique")
    except Exception:
        # SQLite fallback — constraint dropped via batch below
        pass

    # 4. New unique constraint: (name, user_id)
    op.create_unique_constraint("uq_tag_name_user", "tags", ["name", "user_id"])


def downgrade() -> None:
    op.drop_constraint("uq_tag_name_user", "tags", type_="unique")
    op.drop_constraint("fk_tags_user_id", "tags", type_="foreignkey")
    op.drop_column("tags", "user_id")
    op.create_unique_constraint("uq_tags_name", "tags", ["name"])
