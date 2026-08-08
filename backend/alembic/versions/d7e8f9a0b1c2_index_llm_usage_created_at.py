"""index llm_usage.created_at

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-08-08

The daily ceiling on the application's own spend (UX phase 6.1) asks the same
question before every free generation: how much has `user_id IS NULL` cost since
midnight. Today `llm_usage` is indexed on `user_id` alone, which is the wrong
half of that predicate — user_id NULL selects a growing fraction of the table
while the date selects a shrinking one.

An index on `created_at` rather than a composite: `llm_usage` accumulates a row
per model call for every tenant forever, and both readers of this table — the
per-user dashboard and the ceiling — filter by date first. The dashboard already
scans by `user_id` + date and gets the same benefit.

Data is untouched; the table can be rebuilt at any time from nothing lost.
"""
from alembic import op

revision = 'd7e8f9a0b1c2'
down_revision = 'c6d7e8f9a0b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_llm_usage_created_at', 'llm_usage', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_llm_usage_created_at', table_name='llm_usage')
