"""Free landing posts spent per address.

The landing gave two free posts and counted them in the browser, where clearing
the storage bought two more, forever. This is the same allowance kept where the
visitor cannot reach it.

No address is stored — the primary key is a salted hash — and nothing joins to a
user, so the table adds no personal data anybody could ask us to export or
erase.

Revision ID: a1c2e3f4b5d6
Revises: e8f9a0b1c2d3
"""
import sqlalchemy as sa
from alembic import op

revision = 'a1c2e3f4b5d6'
down_revision = 'e8f9a0b1c2d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "anon_usage",
        sa.Column("ip_hash", sa.String(length=40), primary_key=True),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("anon_usage")
