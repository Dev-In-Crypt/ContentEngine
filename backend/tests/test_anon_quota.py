"""Two free landing posts per address, counted where the visitor cannot reach it.

The landing's allowance used to live in localStorage, which the code called what
it was: a polite request. Clearing the browser bought two more, and again, and
again. This moves the count server-side.

What it is not is a wall — a different network defeats it in ten seconds — and
the tests say so rather than implying otherwise. What it stops is the free
version of unlimited.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models.database import AnonUsage, Base
from services import anon_quota

SECRET = "test-secret"


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'anon.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


def _run(sm, coro_fn):
    async def _go():
        async with sm() as db:
            return await coro_fn(db)
    return asyncio.run(_go())


def test_an_address_gets_two_and_then_is_asked_for_an_account(sm):
    assert _run(sm, lambda db: anon_quota.reserve(db, "203.0.113.5", SECRET)) is True
    assert _run(sm, lambda db: anon_quota.reserve(db, "203.0.113.5", SECRET)) is True
    assert _run(sm, lambda db: anon_quota.reserve(db, "203.0.113.5", SECRET)) is False


def test_a_different_address_starts_fresh(sm):
    _run(sm, lambda db: anon_quota.reserve(db, "203.0.113.5", SECRET))
    _run(sm, lambda db: anon_quota.reserve(db, "203.0.113.5", SECRET))

    assert _run(sm, lambda db: anon_quota.reserve(db, "203.0.113.6", SECRET)) is True


def test_the_address_itself_is_never_written_down(sm):
    """A table of addresses is a list of who visited. A salted hash is not — and
    the salt matters: there are only four billion IPv4 addresses, so an unsalted
    hash is an afternoon's work to reverse."""
    ip = "203.0.113.5"
    _run(sm, lambda db: anon_quota.reserve(db, ip, SECRET))

    async def _rows(db):
        return (await db.execute(select(AnonUsage))).scalars().all()

    rows = _run(sm, _rows)
    assert len(rows) == 1
    assert ip not in rows[0].ip_hash
    assert rows[0].ip_hash != anon_quota.fingerprint(ip, "another-secret")


def test_the_count_is_forgotten_after_a_month(sm):
    """Addresses get handed around — a carrier reassigns them hourly. A count
    that lasted forever would quietly retire a whole office from ever seeing the
    product work, years after the one person who used it up moved on."""
    _run(sm, lambda db: anon_quota.reserve(db, "203.0.113.5", SECRET))
    _run(sm, lambda db: anon_quota.reserve(db, "203.0.113.5", SECRET))
    assert _run(sm, lambda db: anon_quota.reserve(db, "203.0.113.5", SECRET)) is False

    async def _age(db):
        row = (await db.execute(select(AnonUsage))).scalar_one()
        row.last_seen = datetime.now(timezone.utc) - anon_quota.WINDOW - timedelta(days=1)
        await db.commit()
    _run(sm, _age)

    assert _run(sm, lambda db: anon_quota.reserve(db, "203.0.113.5", SECRET)) is True


def test_the_try_is_spent_before_the_model_is_called(sm):
    """Committed inside reserve, not after the post comes back. A generation that
    dies halfway must not hand the try back, or a crash loop is free posts
    forever — the same rule as the account-level allowance."""
    _run(sm, lambda db: anon_quota.reserve(db, "203.0.113.5", SECRET))

    async def _used(db):
        return (await db.execute(select(AnonUsage.used))).scalar_one()

    assert _run(sm, _used) == 1


def test_remaining_reports_what_is_left(sm):
    assert _run(sm, lambda db: anon_quota.remaining(db, "203.0.113.9", SECRET)) == 2
    _run(sm, lambda db: anon_quota.reserve(db, "203.0.113.9", SECRET))
    assert _run(sm, lambda db: anon_quota.remaining(db, "203.0.113.9", SECRET)) == 1
