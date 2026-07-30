"""add per-user Kling API key (video generation)

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-30 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c9d0e1f2a3b4'
down_revision = 'b8c9d0e1f2a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user_credentials', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kling_api_key_enc', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('user_credentials', schema=None) as batch_op:
        batch_op.drop_column('kling_api_key_enc')
