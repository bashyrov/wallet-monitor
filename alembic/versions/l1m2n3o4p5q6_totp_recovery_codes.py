"""totp recovery codes + last used

Revision ID: l1m2n3o4p5q6
Revises: k0p1q2r3s4t5
Create Date: 2026-05-10
"""
from alembic import op
import sqlalchemy as sa


revision = 'l1m2n3o4p5q6'
down_revision = 'k0p1q2r3s4t5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users') as bop:
        bop.add_column(sa.Column('totp_recovery_codes', sa.JSON(), nullable=True))
        bop.add_column(sa.Column('totp_last_used_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('users') as bop:
        bop.drop_column('totp_last_used_at')
        bop.drop_column('totp_recovery_codes')
