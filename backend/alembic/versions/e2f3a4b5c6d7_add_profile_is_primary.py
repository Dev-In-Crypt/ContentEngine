"""UX phase 2.1: managed_accounts.is_primary + one primary per owner

Schema only. The seed and the post backfill are a separate revision so that this
one can be reasoned about on its own: it adds a column nobody reads yet and a
constraint that holds trivially while every row is non-primary.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-08-05 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e2f3a4b5c6d7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('managed_accounts', schema=None) as batch_op:
        # sa.false() so the literal is right on both dialects — Postgres has no
        # 0/1 boolean and SQLite has no `false`.
        batch_op.add_column(sa.Column('is_primary', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
    # Partial, so an agency keeps as many non-primary brands as it likes. Both
    # dialects support it; each ignores the other's `_where` kwarg.
    op.create_index('ux_managed_accounts_primary', 'managed_accounts',
                    ['owner_user_id'], unique=True,
                    sqlite_where=sa.text('is_primary = 1'),
                    postgresql_where=sa.text('is_primary'))


def downgrade() -> None:
    op.drop_index('ux_managed_accounts_primary', table_name='managed_accounts')
    with op.batch_alter_table('managed_accounts', schema=None) as batch_op:
        batch_op.drop_column('is_primary')
