"""add users.free_generations_used

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-08-07

One column, no backfill: nobody has spent an allowance that did not exist, so
the server default of 0 is already the truth for every existing row.

NOT NULL with a server default rather than nullable. A NULL here would mean "we
don't know how many free posts this account has had", and every reader would
have to answer that question the same way anyway — as zero — which is a
COALESCE in every caller to express something the column can simply state.

The counter is the unit UX phase 6 will bill against, which is why it lives on
the user rather than in a request-scoped cache: it has to survive a restart, a
new browser and a cleared localStorage, all of which are ways somebody would
otherwise get a second free post.
"""
import sqlalchemy as sa
from alembic import op

revision = 'c6d7e8f9a0b1'
down_revision = 'b5c6d7e8f9a0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column(
        'free_generations_used', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    # batch_alter_table: SQLite rebuilds the table to drop a column, and a
    # downgrade that raises is a rollback that cannot happen — DEPLOY.md tells
    # the operator to downgrade BEFORE redeploying the previous image.
    with op.batch_alter_table('users') as batch:
        batch.drop_column('free_generations_used')
