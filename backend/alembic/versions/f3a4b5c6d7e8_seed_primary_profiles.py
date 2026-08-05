"""UX phase 2.2: a primary profile for every user, and their posts moved onto it

The three writes here are the same three `services.managed_account.
ensure_primary_profile` performs for one user at a time. They must stay
identical: a user seeded by this migration and a user seeded lazily on their
next request have to be indistinguishable afterwards.

The post backfill is not housekeeping and cannot be split into its own
revision. `list_posts` filters `managed_account_id == active_account_id`, so
between setting the pointer and moving the posts, every user's feed is empty.
alembic/env.py wraps the upgrade in one transaction, so on Postgres this is
all-or-nothing; keeping both statements here is what makes that guarantee
worth anything.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-08-05 12:30:00.000000
"""
import uuid

from alembic import op
import sqlalchemy as sa


revision = 'f3a4b5c6d7e8'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None

#: Copied verbatim, not reset: the profile is a new home for identity the user
#: already had. Anything left out is a field they silently lose.
BRAND_FIELDS = (
    "brand_voice_preset", "brand_voice_custom", "niche", "target_audience",
    "brand_name", "slide_accent_color", "slide_text_box_color", "logo_path",
)

_ACCOUNTS = sa.table(
    "managed_accounts",
    sa.column("id", sa.String), sa.column("owner_user_id", sa.String),
    sa.column("name", sa.String), sa.column("is_primary", sa.Boolean),
    *[sa.column(f, sa.String) for f in BRAND_FIELDS],
)


def upgrade() -> None:
    bind = op.get_bind()

    # Posts whose brand was deleted hold a dangling id: posts.managed_account_id
    # never got its foreign key (c7d8e9fa0b1c added the column and the index,
    # not the constraint), so ondelete=SET NULL has never fired anywhere. Since
    # the view filters on `== active`, those rows have been invisible rather
    # than merely untagged. Reset them here so the backfill below adopts them.
    op.execute("""
        UPDATE posts SET managed_account_id = NULL
        WHERE managed_account_id IS NOT NULL
          AND managed_account_id NOT IN (SELECT id FROM managed_accounts)
    """)

    # NOT IN, so a re-run is a no-op rather than an IntegrityError against the
    # partial unique index. Alembic's own idempotence only goes as deep as the
    # version table.
    rows = bind.execute(sa.text(
        "SELECT id, " + ", ".join(BRAND_FIELDS) + " FROM users "
        "WHERE id NOT IN (SELECT owner_user_id FROM managed_accounts "
        "                 WHERE is_primary AND owner_user_id IS NOT NULL)"
    )).mappings().all()

    if rows:
        # uuid4 in Python is the one part SQL can't do portably — Postgres has
        # gen_random_uuid(), SQLite has nothing.
        op.bulk_insert(_ACCOUNTS, [{
            "id": str(uuid.uuid4()),
            "owner_user_id": r["id"],
            "name": (r["brand_name"] or "").strip() or "Personal",
            "is_primary": True,
            **{f: r[f] for f in BRAND_FIELDS},
        } for r in rows])

    # created_at is copied in SQL rather than round-tripped through Python:
    # SQLite hands a raw timestamp back as a string, which the DateTime column
    # then refuses on the way in. The user's own creation time, not now() —
    # for an existing agency the seeded profile really is their oldest brand,
    # so ordering by created_at puts it first without a special case.
    op.execute("""
        UPDATE managed_accounts SET created_at =
          (SELECT u.created_at FROM users u WHERE u.id = managed_accounts.owner_user_id)
        WHERE is_primary
    """)

    # Correlated subqueries rather than a loop: three statements regardless of
    # how many users there are, and valid on both dialects.
    op.execute("""
        UPDATE users SET active_account_id =
          (SELECT ma.id FROM managed_accounts ma
            WHERE ma.owner_user_id = users.id AND ma.is_primary)
        WHERE active_account_id IS NULL
    """)
    op.execute("""
        UPDATE posts SET managed_account_id =
          (SELECT ma.id FROM managed_accounts ma
            WHERE ma.owner_user_id = posts.user_id AND ma.is_primary)
        WHERE managed_account_id IS NULL AND user_id IS NOT NULL
    """)


def downgrade() -> None:
    # Pointers first, rows last — nothing in the database would stop us leaving
    # dangling ids behind (see the missing foreign key above).
    #
    # Keyed on is_primary throughout, so a post generated after the deploy under
    # a real client brand keeps its tag. That is data, not migration residue.
    op.execute("""
        UPDATE posts SET managed_account_id = NULL
        WHERE managed_account_id IN (SELECT id FROM managed_accounts WHERE is_primary)
    """)
    op.execute("""
        UPDATE users SET active_account_id = NULL
        WHERE active_account_id IN (SELECT id FROM managed_accounts WHERE is_primary)
    """)
    op.execute("DELETE FROM managed_accounts WHERE is_primary")
