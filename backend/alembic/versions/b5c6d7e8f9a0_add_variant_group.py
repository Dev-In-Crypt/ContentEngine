"""add posts.variant_group_id and posts.tone

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-08-07

Two columns and one backfill, for UX phase 4 — one idea published to several
networks.

`variant_group_id` ties sibling posts together. It is a column rather than a
join table on purpose: a sibling stays an ordinary `Post`, so publishing,
scheduling, business approval and the X video pipeline keep working without a
line changed. The alternative — a child table holding per-network text — would
have meant rewriting all twelve places that write publish state.

The backfill sets every existing post to its own id. The column is nullable, so
skipping it raises nothing; it just leaves old posts outside the grouping the
Queue and the tab bar read, forever. Doing it here means no read site ever needs
`COALESCE(variant_group_id, id)`.

Order matters only for cost: add the column, backfill, then build the index, so
the index is built once over final values rather than maintained per row.

`tone` was never persisted — `_persist` accepted it from the composer and
dropped it — so there is nothing to backfill it from, and NULL honestly means
"this post predates the column". Without it, adapting a post to a second network
would rewrite it as "professional" whatever the author chose.

Migrations run inside app startup wrapped in a single transaction (alembic/env.py).
The UPDATE is a full-table write; on this codebase's scale that is nothing, but
it is a full-table write, so say so rather than let someone rediscover it.
"""
import sqlalchemy as sa
from alembic import op

revision = 'b5c6d7e8f9a0'
down_revision = 'a4b5c6d7e8f9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('posts', sa.Column('variant_group_id', sa.String(length=36),
                                     nullable=True))
    op.add_column('posts', sa.Column('tone', sa.String(length=30), nullable=True))
    op.execute("UPDATE posts SET variant_group_id = id WHERE variant_group_id IS NULL")
    op.create_index('ix_posts_variant_group_id', 'posts', ['variant_group_id'])


def downgrade() -> None:
    op.drop_index('ix_posts_variant_group_id', table_name='posts')
    # batch_alter_table: SQLite rebuilds the table to drop a column. A downgrade
    # that raises is a rollback that cannot happen, and DEPLOY.md tells the
    # operator to downgrade BEFORE redeploying the previous image.
    with op.batch_alter_table('posts') as batch:
        batch.drop_column('tone')
        batch.drop_column('variant_group_id')
