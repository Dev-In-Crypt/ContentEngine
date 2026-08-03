"""add video_publish_jobs

Phase 8: publishing a video to X needs a chunked, resumable upload (INIT ->
APPEND x N -> FINALIZE -> STATUS -> tweet), which takes minutes and must
survive a container restart mid-upload. The row IS the job, same principle as
media_assets — chunk_index lets a poller tick resume exactly where a restart
left off instead of re-uploading from scratch.

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-08-03 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd1e2f3a4b5c6'
down_revision = 'c9d0e1f2a3b4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'video_publish_jobs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=True),
        sa.Column('platform', sa.String(length=20), nullable=False,
                  server_default='x'),
        sa.Column('post_id', sa.String(length=36), nullable=True),
        sa.Column('asset_id', sa.String(length=36), nullable=True),
        sa.Column('video_path', sa.Text(), nullable=False),
        sa.Column('total_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=20), nullable=False,
                  server_default='queued'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('media_id', sa.String(length=64), nullable=True),
        sa.Column('chunk_index', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tweet_id', sa.String(length=64), nullable=True),
        sa.Column('permalink', sa.Text(), nullable=True),
        sa.Column('caption', sa.Text(), nullable=True),
        sa.Column('thread_parts', sa.JSON(), nullable=True),
        sa.Column('alt_text', sa.Text(), nullable=True),
        sa.Column('long_form', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['asset_id'], ['media_assets.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_video_publish_jobs_user_id', 'video_publish_jobs', ['user_id'])
    op.create_index('ix_video_publish_jobs_post_id', 'video_publish_jobs', ['post_id'])
    op.create_index('ix_video_publish_jobs_asset_id', 'video_publish_jobs', ['asset_id'])
    op.create_index('ix_video_publish_jobs_status', 'video_publish_jobs', ['status'])
    op.create_index('ix_video_publish_jobs_status_next', 'video_publish_jobs',
                    ['status', 'next_attempt_at'])


def downgrade() -> None:
    op.drop_index('ix_video_publish_jobs_status_next', table_name='video_publish_jobs')
    op.drop_index('ix_video_publish_jobs_status', table_name='video_publish_jobs')
    op.drop_index('ix_video_publish_jobs_asset_id', table_name='video_publish_jobs')
    op.drop_index('ix_video_publish_jobs_post_id', table_name='video_publish_jobs')
    op.drop_index('ix_video_publish_jobs_user_id', table_name='video_publish_jobs')
    op.drop_table('video_publish_jobs')
