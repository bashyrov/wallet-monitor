"""initial_schema

Revision ID: 014613d42a04
Revises:
Create Date: 2026-04-05

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '014613d42a04'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('username', sa.String(), nullable=False, unique=True),
        sa.Column('email', sa.String(), nullable=False, unique=True),
        sa.Column('hashed_password', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'tags',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('name', sa.String(), nullable=False, unique=True),
        sa.Column('color', sa.String(), nullable=False, server_default='#6366f1'),
    )

    op.create_table(
        'wallets',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('wallet_type', sa.String(), nullable=False),
        sa.Column('type_value', sa.String(), nullable=False),
        sa.Column('credentials', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    )

    op.create_table(
        'wallet_tags',
        sa.Column('wallet_id', sa.Integer(), sa.ForeignKey('wallets.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('tag_id', sa.Integer(), sa.ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True),
    )

    op.create_table(
        'wallet_addresses',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('wallet_id', sa.Integer(), sa.ForeignKey('wallets.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('address', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('wallet_addresses')
    op.drop_table('wallet_tags')
    op.drop_table('wallets')
    op.drop_table('tags')
    op.drop_table('users')
