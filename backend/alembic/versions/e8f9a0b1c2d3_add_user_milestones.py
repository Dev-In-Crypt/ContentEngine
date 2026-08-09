"""add users.milestones

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-08-09

One JSON column holding {milestone: when} — what the product has already shown
this person (UX phase 8). Nullable rather than NOT NULL with a default: NULL is
exactly what every existing row means, "nothing shown yet", and the service
reads it that way. A backfill of '{}' would write to every row to say the same
thing the absence already says.

Not a table. The alternative — a row per event with a timestamp — buys funnel
analytics nobody has asked for, and costs a migration, a GDPR wiring and a
sweeper for six flags. Owner's decision.
"""
import sqlalchemy as sa
from alembic import op

revision = 'e8f9a0b1c2d3'
down_revision = 'd7e8f9a0b1c2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('milestones', sa.JSON(), nullable=True))


def downgrade() -> None:
    # batch_alter_table: SQLite rebuilds the table to drop a column, and a
    # downgrade that raises is a rollback that cannot happen — DEPLOY.md tells
    # the operator to downgrade BEFORE redeploying the previous image.
    with op.batch_alter_table('users') as batch:
        batch.drop_column('milestones')
