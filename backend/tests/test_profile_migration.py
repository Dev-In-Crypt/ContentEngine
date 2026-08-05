"""The data migration that gives every user a profile (phase 2.2).

Run against a real SQLite file through real Alembic, at the two revisions that
matter: stop at the one before, write the data a live deployment would have,
then upgrade and look at what happened to it.

This is the phase's dangerous half. The failure mode is silent — nothing
raises, posts simply stop being returned — so the assertions here are about
rows, not about status codes.
"""
import sqlite3
import uuid

import pytest
from alembic import command
from alembic.config import Config

import main

BEFORE = "e2f3a4b5c6d7"       # is_primary exists, nothing is seeded yet


def _cfg(db):
    cfg = Config(str(main._HERE / "alembic.ini"))
    cfg.set_main_option("script_location", str(main._HERE / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    return cfg


@pytest.fixture
def db(tmp_path):
    """A database at the revision just before the seed, ready to be filled."""
    path = tmp_path / "seed.db"
    command.upgrade(_cfg(path), BEFORE)
    return path


def _con(db):
    return sqlite3.connect(db)


def _add_user(db, **cols):
    """Raw SQL on purpose — a migration test must not import the ORM models,
    whose columns describe head rather than the revision under test."""
    uid = str(uuid.uuid4())
    cols.setdefault("email", f"{uid}@ex.com")
    cols.setdefault("token_version", 0)          # NOT NULL, ORM-side default only
    cols.setdefault("created_at", "2026-01-02 03:04:05")
    keys = ["id", *cols]
    con = _con(db)
    con.execute(f"INSERT INTO users ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                [uid, *cols.values()])
    con.commit()
    con.close()
    return uid


def _add_post(db, user_id, account_id, topic):
    con = _con(db)
    con.execute("INSERT INTO posts (id, user_id, managed_account_id, topic, format, "
                "status) VALUES (?,?,?,?,'single','preview')",
                [str(uuid.uuid4()), user_id, account_id, topic])
    con.commit()
    con.close()


def _add_account(db, owner, name, is_primary=0):
    aid = str(uuid.uuid4())
    con = _con(db)
    con.execute("INSERT INTO managed_accounts (id, owner_user_id, name, is_primary) "
                "VALUES (?,?,?,?)", [aid, owner, name, is_primary])
    con.commit()
    con.close()
    return aid


def _one(db, sql, args=()):
    con = _con(db)
    row = con.execute(sql, args).fetchone()
    con.close()
    return row[0] if row else None


def _topics_on(db, account_id):
    con = _con(db)
    rows = con.execute("SELECT topic FROM posts WHERE managed_account_id = ?",
                       [account_id]).fetchall()
    con.close()
    return {r[0] for r in rows}


def _upgrade(db):
    command.upgrade(_cfg(db), "head")


# ------------------------------------------------------------------ the seed

def test_every_user_gets_exactly_one_primary(db):
    for _ in range(3):
        _add_user(db)
    _upgrade(db)
    assert _one(db, "SELECT count(*) FROM managed_accounts WHERE is_primary") == 3
    assert _one(db, "SELECT count(*) FROM users WHERE active_account_id IS NULL") == 0


def test_the_brand_columns_are_copied(db):
    uid = _add_user(db, niche="artisan bakery", brand_name="Crumb",
                    slide_accent_color="#0a2540", brand_voice_preset="warm",
                    logo_path="/uploads/logos/x.png")
    _upgrade(db)
    row = _con(db).execute(
        "SELECT name, niche, brand_name, slide_accent_color, brand_voice_preset, "
        "logo_path FROM managed_accounts WHERE owner_user_id = ?", [uid]).fetchone()
    assert row == ("Crumb", "artisan bakery", "Crumb", "#0a2540", "warm",
                   "/uploads/logos/x.png")


def test_a_user_with_no_brand_name_gets_a_named_profile(db):
    uid = _add_user(db)
    _upgrade(db)
    assert _one(db, "SELECT name FROM managed_accounts WHERE owner_user_id = ?",
                [uid]) == "Personal"


def test_the_profile_is_the_active_one(db):
    uid = _add_user(db)
    _upgrade(db)
    active = _one(db, "SELECT active_account_id FROM users WHERE id = ?", [uid])
    assert active == _one(db, "SELECT id FROM managed_accounts WHERE owner_user_id = ?",
                          [uid])


# ------------------------------------------------------------------ the posts

def test_existing_posts_are_adopted(db):
    """The whole reason this is one migration and not two. list_posts filters
    on the active account, so the instant active_account_id is set, a post left
    holding NULL is gone from its owner's view. Drop this UPDATE and every user
    on the box opens the app to an empty feed."""
    uid = _add_user(db)
    _add_post(db, uid, None, "one")
    _add_post(db, uid, None, "two")
    _upgrade(db)
    pid = _one(db, "SELECT id FROM managed_accounts WHERE owner_user_id = ?", [uid])
    assert _topics_on(db, pid) == {"one", "two"}


def test_a_posts_own_client_brand_is_left_alone(db):
    """An agency's client work must not be swept into their personal profile."""
    uid = _add_user(db)
    client = _add_account(db, uid, "Client A")
    _add_post(db, uid, client, "client-work")
    _add_post(db, uid, None, "own-work")
    _upgrade(db)
    pid = _one(db, "SELECT id FROM managed_accounts WHERE owner_user_id = ? "
                   "AND is_primary", [uid])
    assert _topics_on(db, client) == {"client-work"}
    assert _topics_on(db, pid) == {"own-work"}


def test_posts_stranded_by_a_deleted_brand_are_rescued(db):
    """posts.managed_account_id never got its foreign key (c7d8e9fa0b1c added
    the column and index, not the constraint), so ondelete=SET NULL has never
    fired on any environment. Posts whose brand was deleted hold a dangling id
    and, because the filter is `== active`, have been invisible ever since.
    This is the one chance to bring them back."""
    uid = _add_user(db)
    _add_post(db, uid, "a-brand-deleted-long-ago", "stranded")
    _upgrade(db)
    pid = _one(db, "SELECT id FROM managed_accounts WHERE owner_user_id = ?", [uid])
    assert _topics_on(db, pid) == {"stranded"}


def test_posts_are_adopted_by_their_own_owner(db):
    """Mutation guard: an unscoped UPDATE would hand every post on the box to
    whichever profile the subquery happened to resolve."""
    a, b = _add_user(db), _add_user(db)
    _add_post(db, a, None, "a-post")
    _add_post(db, b, None, "b-post")
    _upgrade(db)
    pa = _one(db, "SELECT id FROM managed_accounts WHERE owner_user_id = ?", [a])
    pb = _one(db, "SELECT id FROM managed_accounts WHERE owner_user_id = ?", [b])
    assert _topics_on(db, pa) == {"a-post"}
    assert _topics_on(db, pb) == {"b-post"}


# ------------------------------------------------------------------ re-running

def test_a_user_who_already_has_a_primary_is_skipped(db):
    """Alembic's idempotence only goes as deep as the version table. A migration
    that ran, half-committed on some other database, and got run again must not
    hand the user a second profile — which the partial unique index would
    refuse anyway, so without the guard the whole upgrade fails."""
    uid = _add_user(db, niche="already-set")
    existing = _add_account(db, uid, "Mine", is_primary=1)
    _upgrade(db)
    assert _one(db, "SELECT count(*) FROM managed_accounts WHERE owner_user_id = ?",
                [uid]) == 1
    assert _one(db, "SELECT id FROM managed_accounts WHERE owner_user_id = ?",
                [uid]) == existing


def test_an_existing_active_account_is_not_overridden(db):
    """An agency mid-session, with a client brand selected, keeps it."""
    uid = _add_user(db)
    client = _add_account(db, uid, "Client A")
    con = _con(db)
    con.execute("UPDATE users SET active_account_id = ? WHERE id = ?", [client, uid])
    con.commit()
    con.close()
    _upgrade(db)
    assert _one(db, "SELECT active_account_id FROM users WHERE id = ?", [uid]) == client


def test_running_the_whole_chain_twice_is_a_no_op(db):
    _add_user(db)
    _upgrade(db)
    _upgrade(db)
    assert _one(db, "SELECT count(*) FROM managed_accounts WHERE is_primary") == 1


# ------------------------------------------------------------------ downgrade

def test_downgrade_puts_the_data_back(db):
    """The rollback story only exists if this works. Seeded rows go, the
    pointers they filled return to NULL."""
    uid = _add_user(db)
    _add_post(db, uid, None, "one")
    _upgrade(db)
    command.downgrade(_cfg(db), BEFORE)
    assert _one(db, "SELECT count(*) FROM managed_accounts WHERE is_primary") == 0
    assert _one(db, "SELECT active_account_id FROM users WHERE id = ?", [uid]) is None
    assert _one(db, "SELECT managed_account_id FROM posts WHERE topic = 'one'") is None


def test_downgrade_keeps_work_done_under_a_client_brand(db):
    """A post generated after the deploy under a real client brand is data, not
    migration residue — the downgrade must not untag it."""
    uid = _add_user(db)
    _upgrade(db)
    client = _add_account(db, uid, "Client A")
    _add_post(db, uid, client, "client-work")
    command.downgrade(_cfg(db), BEFORE)
    assert _one(db, "SELECT managed_account_id FROM posts WHERE topic = 'client-work'"
                ) == client
