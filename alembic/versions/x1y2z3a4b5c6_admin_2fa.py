"""admin TOTP 2FA — totp_secret + verified_at on users.

Adds the columns to users:
  · totp_secret_enc    base64-Fernet-encrypted TOTP secret (only set
                       once the admin runs setup),
  · totp_verified_at   timestamp of the first successful code verify
                       (also marks the secret as "armed" — login flow
                       now requires the second factor).

Non-admin rows leave the columns NULL so regular users continue to log
in without OTP. The flow only intercepts admin logins.

Revision ID: x1y2z3a4b5c6
Revises: w0x1y2z3a4b5
Create Date: 2026-04-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'x1y2z3a4b5c6'
down_revision = 'w0x1y2z3a4b5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("totp_secret_enc", sa.String(), nullable=True))
    op.add_column("users", sa.Column("totp_verified_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("users", "totp_verified_at")
    op.drop_column("users", "totp_secret_enc")
