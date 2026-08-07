"""The migration that gives every post a variant group (phase 4.1).

Same shape as `test_profile_migration.py`: stop at the revision before, write
the rows a live deployment already has, upgrade, and look at what happened to
them. Raw SQL on purpose — a migration test must not import the ORM models,
whose columns describe head rather than the revision under test.

The backfill is the whole point. `variant_group_id` is nullable, so nothing
raises if it stays NULL; the group of one simply stops existing and every list
that groups by it starts treating old posts as unrelated singletons — which is
what they are, but only by accident. Making the column true for every row means
no read site ever needs `COALESCE(variant_group_id, id)`.
"""
import sqlite3
import uuid

import pytest
from alembic import command
from alembic.config import Config

import main

BEFORE = "a4b5c6d7e8f9"       # team_invitations; the last revision before this one


def _cfg(db):
    cfg = Config(str(main._HERE / "alembic.ini"))
    cfg.set_main_option("script_location", str(main._HERE / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    return cfg


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "variant.db"
    command.upgrade(_cfg(path), BEFORE)
    return path


def _add_user(db):
    uid = str(uuid.uuid4())
    con = sqlite3.connect(db)
    con.execute("INSERT INTO users (id, email, token_version, created_at) "
                "VALUES (?,?,0,'2026-01-02 03:04:05')", [uid, f"{uid}@ex.com"])
    con.commit()
    con.close()
    return uid


def _add_post(db, user_id, topic, platform="instagram"):
    pid = str(uuid.uuid4())
    con = sqlite3.connect(db)
    con.execute("INSERT INTO posts (id, user_id, topic, format, status, platform) "
                "VALUES (?,?,?,'single','draft',?)", [pid, user_id, topic, platform])
    con.commit()
    con.close()
    return pid


def _rows(db, sql, args=()):
    con = sqlite3.connect(db)
    out = con.execute(sql, args).fetchall()
    con.close()
    return out


def _upgrade(db):
    command.upgrade(_cfg(db), "head")


def test_every_existing_post_becomes_its_own_group(db):
    """A post that predates the column is a group of one, and says so."""
    user = _add_user(db)
    ids = [_add_post(db, user, f"Topic {n}") for n in range(3)]

    _upgrade(db)

    rows = _rows(db, "SELECT id, variant_group_id FROM posts ORDER BY topic")
    assert [r[1] for r in rows] == [r[0] for r in rows]
    assert {r[0] for r in rows} == set(ids)


def test_no_post_is_left_without_a_group(db):
    user = _add_user(db)
    for n in range(5):
        _add_post(db, user, f"Topic {n}")

    _upgrade(db)

    assert _rows(db, "SELECT count(*) FROM posts WHERE variant_group_id IS NULL")[0][0] == 0


def test_the_tone_column_arrives_empty(db):
    """`tone` was never stored before, so there is nothing to backfill it from.
    NULL means "we don't know", and the adapt endpoint falls back rather than
    inventing a tone the post was not written in."""
    user = _add_user(db)
    _add_post(db, user, "Topic")

    _upgrade(db)

    assert _rows(db, "SELECT tone FROM posts")[0][0] is None


def test_the_group_column_is_indexed(db):
    """Every group read filters on it — the Queue, the Calendar and the tab bar.
    Without the index those are full scans on the largest table in the schema."""
    _upgrade(db)
    idx = {r[1] for r in _rows(db, "PRAGMA index_list(posts)")}
    assert "ix_posts_variant_group_id" in idx


def test_downgrade_removes_both_columns(db):
    """SQLite has no DROP COLUMN before 3.35 and Alembic needs a batch rebuild
    either way; a downgrade that raises is a rollback that cannot happen, and
    DEPLOY.md tells the operator to downgrade BEFORE redeploying the old image."""
    user = _add_user(db)
    _add_post(db, user, "Topic")
    _upgrade(db)

    command.downgrade(_cfg(db), BEFORE)

    cols = {r[1] for r in _rows(db, "PRAGMA table_info(posts)")}
    assert "variant_group_id" not in cols
    assert "tone" not in cols
    assert _rows(db, "SELECT count(*) FROM posts")[0][0] == 1   # the row survives
