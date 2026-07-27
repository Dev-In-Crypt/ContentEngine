"""add media_assets, plus the links from slides and posts

The library is the first thing in this schema that owns media without a post.
The two link columns are nullable and SET NULL on delete, because inserting an
asset into a post copies the bytes rather than referencing them — the orphan
sweep and GDPR erasure both rmtree uploads/posts/<post_id>, so a shared file
would let post cleanup delete the library.

Revision ID: b8c9d0e1f2a3
Revises: f7a8b9c0d1e2
Create Date: 2026-07-27 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b8c9d0e1f2a3'
down_revision = 'f7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'media_assets',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=True),
        sa.Column('managed_account_id', sa.String(length=36), nullable=True),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False,
                  server_default='ready'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('provider', sa.String(length=30), nullable=True),
        sa.Column('model', sa.String(length=120), nullable=True),
        sa.Column('provider_task_id', sa.String(length=200), nullable=True),
        sa.Column('prompt', sa.Text(), nullable=True),
        sa.Column('title', sa.String(length=200), nullable=True),
        sa.Column('parent_asset_id', sa.String(length=36), nullable=True),
        sa.Column('file_path', sa.Text(), nullable=True),
        sa.Column('mime', sa.String(length=60), nullable=True),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('duration_sec', sa.Float(), nullable=True),
        sa.Column('bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost_usd', sa.Float(), nullable=True, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['managed_account_id'], ['managed_accounts.id'],
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['parent_asset_id'], ['media_assets.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_media_assets_user_id', 'media_assets', ['user_id'])
    op.create_index('ix_media_assets_managed_account_id', 'media_assets',
                    ['managed_account_id'])
    op.create_index('ix_media_assets_user_kind_created', 'media_assets',
                    ['user_id', 'kind', 'created_at'])

    with op.batch_alter_table('slides', schema=None) as batch_op:
        batch_op.add_column(sa.Column('media_asset_id', sa.String(length=36),
                                      nullable=True))
    with op.batch_alter_table('posts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('video_asset_id', sa.String(length=36),
                                      nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('posts', schema=None) as batch_op:
        batch_op.drop_column('video_asset_id')
    with op.batch_alter_table('slides', schema=None) as batch_op:
        batch_op.drop_column('media_asset_id')
    op.drop_index('ix_media_assets_user_kind_created', table_name='media_assets')
    op.drop_index('ix_media_assets_managed_account_id', table_name='media_assets')
    op.drop_index('ix_media_assets_user_id', table_name='media_assets')
    op.drop_table('media_assets')
