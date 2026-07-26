"""add publish_attempts for scheduled-publish retries

Revision ID: a1b2c3d4e5f6
Revises: d8e9fa0b1c2d
Create Date: 2026-07-26 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = 'd8e9fa0b1c2d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so rows written by an older app version (and any in flight
    # during the deploy) get 0 rather than NULL, which would break the counter.
    with op.batch_alter_table('posts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('publish_attempts', sa.Integer(),
                                      nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('posts', schema=None) as batch_op:
        batch_op.drop_column('publish_attempts')
