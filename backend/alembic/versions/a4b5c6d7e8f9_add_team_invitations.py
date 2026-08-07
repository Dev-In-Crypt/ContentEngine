"""add team_invitations

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-08-07

Schema only — nothing to backfill, because nothing invited anybody before this.

The unique index is PARTIAL. A plain one would forbid ever re-inviting somebody
whose invitation was revoked, which is the normal case: you mistype an address,
revoke it, and send it again. Both dialects support the predicate, so this is
one index rather than a check the application would have to race against.
"""
import sqlalchemy as sa
from alembic import op

revision = 'a4b5c6d7e8f9'
down_revision = 'f3a4b5c6d7e8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'team_invitations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('owner_user_id', sa.String(length=36), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('accepted_user_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['accepted_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_team_invitations_owner_user_id', 'team_invitations',
                    ['owner_user_id'])
    op.create_index('ux_team_invitations_pending', 'team_invitations',
                    ['owner_user_id', 'email'], unique=True,
                    sqlite_where=sa.text("status = 'pending'"),
                    postgresql_where=sa.text("status = 'pending'"))


def downgrade() -> None:
    op.drop_index('ux_team_invitations_pending', table_name='team_invitations')
    op.drop_index('ix_team_invitations_owner_user_id', table_name='team_invitations')
    op.drop_table('team_invitations')
