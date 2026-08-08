"""The free-post allowance.

Phase 5's onboarding ends on a post the app pays for, so something has to say
how many of those an account gets. That something is a counter on the user, and
this module is the only place allowed to move it.

The rule that shapes the whole design: **the allowance is spent before the model
is called, and committed there.** A counter incremented after a successful
generation is a counter that never rises when the process dies mid-call, when
two requests arrive together, or when the provider answers slowly enough that
somebody presses the button again — all three of which end with the app paying
twice for one free post. Refunding a reservation that demonstrably bought
nothing is the cheap direction to be wrong in.

The unit is the account rather than the IP: per-IP alone breaks a shared office
and is defeated by a VPN, and the account is what phase 6 will bill against, so
the column is the thing that phase reads rather than a throwaway.
"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models.database import Base, User
from services import free_generation
from services.auth import hash_password


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'free.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


def _user(sm, email="free@example.com"):
    async def _go():
        async with sm() as db:
            u = User(email=email, password_hash=hash_password("password123"),
                     account_type="creator")
            db.add(u)
            await db.commit()
            return u.id
    return asyncio.run(_go())


def _run(sm, fn, user_id):
    async def _go():
        async with sm() as db:
            u = await db.get(User, user_id)
            return await fn(db, u)
    return asyncio.run(_go())


def _remaining(sm, user_id) -> int:
    """`remaining` is a plain read and takes no session — it is the one thing in
    this module a caller may ask without being able to spend."""
    async def _go():
        async with sm() as db:
            return free_generation.remaining(await db.get(User, user_id))
    return asyncio.run(_go())


def _used(sm, user_id) -> int:
    async def _go():
        async with sm() as db:
            return (await db.execute(
                select(User.free_generations_used).where(User.id == user_id))).scalar_one()
    return asyncio.run(_go())


def test_a_new_account_starts_with_its_allowance(sm):
    uid = _user(sm)
    assert _used(sm, uid) == 0
    assert _remaining(sm, uid) == free_generation.FREE_POST_LIMIT


def test_reserving_spends_one(sm):
    uid = _user(sm)
    assert _run(sm, free_generation.reserve, uid) is True
    assert _used(sm, uid) == 1


def test_the_post_after_the_last_one_is_refused(sm):
    """The whole point of the counter. Without it every route that spends the
    application's key is an unlimited generator.

    Written against FREE_POST_LIMIT rather than a literal: the number moved from
    1 to 5 in UX phase 6.2 and will move again, and a test that hard-codes it
    stops testing the guard and starts testing the constant."""
    uid = _user(sm)
    for _ in range(free_generation.FREE_POST_LIMIT):
        assert _run(sm, free_generation.reserve, uid) is True

    assert _run(sm, free_generation.reserve, uid) is False
    # A refused attempt costs nothing — it must not push the counter past the
    # limit, or a later increase would hand back posts nobody was owed.
    assert _used(sm, uid) == free_generation.FREE_POST_LIMIT


def test_the_reservation_is_committed_not_merely_pending(sm):
    """Committed inside reserve(), before the caller goes anywhere near a
    provider. A pending increment on a session that is later rolled back — which
    is exactly what a failed request does — hands the allowance back for free."""
    uid = _user(sm)
    _run(sm, free_generation.reserve, uid)
    assert _used(sm, uid) == 1          # read on a FRESH session


def test_a_refund_hands_the_allowance_back(sm):
    """For the one case that deserves it: the model was called and gave nothing
    back, so the account paid for silence."""
    uid = _user(sm)
    _run(sm, free_generation.reserve, uid)
    _run(sm, free_generation.refund, uid)
    assert _used(sm, uid) == 0
    assert _run(sm, free_generation.reserve, uid) is True


def test_a_refund_never_goes_below_zero(sm):
    """A double refund — a retry, a stray except branch — must not mint
    allowance out of nothing."""
    uid = _user(sm)
    _run(sm, free_generation.refund, uid)
    _run(sm, free_generation.refund, uid)
    assert _used(sm, uid) == 0
    assert _remaining(sm, uid) == free_generation.FREE_POST_LIMIT


def test_two_accounts_do_not_share_an_allowance(sm):
    a = _user(sm, "a@example.com")
    b = _user(sm, "b@example.com")
    _run(sm, free_generation.reserve, a)
    assert _used(sm, b) == 0
    assert _run(sm, free_generation.reserve, b) is True


def test_remaining_never_reports_a_negative(sm):
    """The limit can be lowered in a later release while accounts already sit
    above it; "you have -2 posts left" is not a sentence to show anybody."""
    uid = _user(sm)

    async def _overspend(db, u):
        u.free_generations_used = free_generation.FREE_POST_LIMIT + 3
        await db.commit()
    _run(sm, _overspend, uid)

    assert _remaining(sm, uid) == 0
    assert _run(sm, free_generation.reserve, uid) is False
