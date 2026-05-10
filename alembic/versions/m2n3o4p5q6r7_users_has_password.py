"""users.has_password — distinguish OAuth-only users from password users

Revision ID: m2n3o4p5q6r7
Revises: l1m2n3o4p5q6
Create Date: 2026-05-11
"""
from alembic import op
import sqlalchemy as sa


revision = 'm2n3o4p5q6r7'
down_revision = 'l1m2n3o4p5q6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users') as bop:
        bop.add_column(sa.Column('has_password', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    with op.batch_alter_table('users') as bop:
        bop.drop_column('has_password')
