"""add connection_health to user_credentials

Revision ID: f7a8b9c0d1e2
Revises: a1b2c3d4e5f6
Create Date: 2026-07-26 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f7a8b9c0d1e2'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: a row with no health yet simply hasn't been checked, which the
    # code already treats as "unknown" rather than "broken".
    with op.batch_alter_table('user_credentials', schema=None) as batch_op:
        batch_op.add_column(sa.Column('connection_health', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('user_credentials', schema=None) as batch_op:
        batch_op.drop_column('connection_health')
