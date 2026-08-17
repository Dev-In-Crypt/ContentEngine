"""What the application has spent on its own key today, in dollars.

UX phase 6 gives new accounts a few posts on our key before asking for theirs.
A per-account allowance bounds what any one person can take; it says nothing
about what a thousand accounts can take together, so there is also a daily
ceiling on our own spend — and a ceiling nobody can read is decoration.

Two facts about the existing accounting decide the shape of this module.

**Cost is buffered in memory and written on demand.** `record_usage` appends to
a module-level list; the only thing that ever drained it was `GET /api/usage`,
the cost dashboard. So a ceiling computed straight from `llm_usage` would read
zero all night while the tap ran, and would first notice the money at whatever
moment somebody happened to open the spend tab. Every read here flushes first.

**Our spend is the rows with no user.** `current_user_id` is set to the caller
by the auth dependency and deliberately cleared before a call we pay for
(onboarding does this already), because filing our bill under the name of
someone we told "you pay the vendor directly" is a lie in the interface. That
makes `user_id IS NULL` exactly "the application's own spend" — the ceiling
needs no second table to find it.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import services.openrouter as openrouter
from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, LLMUsage, User
from services import app_spend


@pytest.fixture(autouse=True)
def empty_buffer():
    """The usage buffer is module-level state shared by the whole process — a
    record left behind by another test would be counted as spend here."""
    openrouter.drain_usage()
    yield
    openrouter.drain_usage()


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'spend.db'}")

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


def _row(sm, *, cost: float, user_id=None, at=None) -> None:
    """A cost already written to the table, bypassing the buffer."""
    async def _go(db):
        db.add(LLMUsage(id=str(uuid.uuid4()), user_id=user_id, model="m",
                        prompt_tokens=1, completion_tokens=1, total_tokens=2,
                        cost=cost, created_at=at or datetime.now(timezone.utc)))
        await db.commit()
    _run(sm, _go)


# ── the ceiling can see the money ───────────────────────────────────────────

def test_the_cap_sees_spend_that_is_still_buffered(sm):
    """The reason this module exists. Before it, cost sat in a list in memory
    until somebody opened the dashboard, so a ceiling read straight from the
    table would let the tap run all night and report zero."""
    openrouter.record_usage("anthropic/claude-sonnet-4",
                            {"prompt_tokens": 10, "completion_tokens": 20,
                             "total_tokens": 30, "cost": 0.42})

    assert _run(sm, app_spend.flush_and_totals)[0] == pytest.approx(0.42)


def test_a_flush_writes_the_buffered_call_exactly_once(sm):
    """Draining is what makes a second read safe: the buffer is emptied by the
    write, so polling the ceiling cannot inflate it."""
    openrouter.record_usage("m", {"total_tokens": 1, "cost": 0.10})
    _run(sm, app_spend.flush_and_totals)

    assert _run(sm, app_spend.flush_and_totals)[0] == pytest.approx(0.10)
    rows = _run(sm, lambda db: db.execute(select(LLMUsage)))
    assert len(rows.scalars().all()) == 1


# ── whose money it is ───────────────────────────────────────────────────────

def test_a_users_own_spend_does_not_close_the_free_tap(sm):
    """Somebody generating hard on their OWN key must not use up the budget for
    everyone else's free posts. Their calls are attributed; ours are not."""
    _row(sm, cost=99.0, user_id="a-real-user-id")
    _row(sm, cost=0.25)

    assert _run(sm, app_spend.app_spend_today) == pytest.approx(0.25)


def test_yesterdays_spend_does_not_count_against_today(sm):
    """A daily ceiling that never forgets is a lifetime ceiling."""
    _row(sm, cost=7.0, at=datetime.now(timezone.utc) - timedelta(days=1))
    _row(sm, cost=0.50)

    assert _run(sm, app_spend.app_spend_today) == pytest.approx(0.50)


def test_a_quiet_day_costs_nothing_and_reads_zero(sm):
    assert _run(sm, app_spend.app_spend_today) == 0.0


# ── one writer ──────────────────────────────────────────────────────────────

def test_the_dashboard_and_the_ceiling_share_one_writer(sm):
    """`_flush_usage` used to live in the admin router. Two copies of "turn the
    buffer into rows" would drift, and the failure would be silent in the worst
    direction: the ceiling reading less than was spent.

    So this goes through the HTTP dashboard and then asks the ceiling. It passes
    only if the dashboard's flush wrote our un-attributed row too.
    """
    async def override_db():
        async with sm() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: Settings(app_mode="cloud")
    app.state.sessionmaker = sm
    try:
        client = TestClient(app)
        token = client.post("/api/auth/register",
                            json={"email": "dash@example.com",
                                  "password": "password123"}).json()["access_token"]
        openrouter.record_usage("m", {"total_tokens": 5, "cost": 0.33})

        r = client.get("/api/usage", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        # The dashboard is user-scoped, so it shows this person nothing…
        assert r.json()["today"]["cost"] == 0.0
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_settings, None)

    # …while the same flush put our spend where the ceiling looks for it.
    assert _run(sm, app_spend.app_spend_today) == pytest.approx(0.33)


def test_an_empty_buffer_is_not_a_write(sm):
    """Both callers poll: the dashboard on a timer, the ceiling before every free
    generation. A flush that committed regardless would turn reading into
    writing, on the busier of the two paths."""
    from unittest.mock import AsyncMock, patch

    db = AsyncMock()
    with patch("services.app_spend.drain_usage", return_value=[]):
        asyncio.run(app_spend.flush_usage(db))
    db.commit.assert_not_awaited()


def test_a_buffered_record_is_committed(sm):
    """The other half, and the reason the one above is not simply "never write"."""
    from unittest.mock import AsyncMock, patch

    db = AsyncMock()
    rec = {"model": "m", "prompt_tokens": 1, "completion_tokens": 1,
           "total_tokens": 2, "cost": 0.01}
    with patch("services.app_spend.drain_usage", return_value=[rec]):
        asyncio.run(app_spend.flush_usage(db))
    db.commit.assert_awaited_once()


# ── the dial ────────────────────────────────────────────────────────────────

def test_the_ceiling_is_configurable_and_has_a_finite_default():
    """A default of 0 would mean "no free posts, ever" and an unset one would
    mean "no ceiling" — both are decisions nobody made on purpose."""
    assert Settings().app_daily_spend_usd > 0
    assert Settings(app_daily_spend_usd=2.5).app_daily_spend_usd == 2.5


def test_the_user_row_is_left_alone_by_the_flush(sm):
    """Attributed spend keeps its owner: the same flush serves the per-user
    dashboard, and dropping user_id would empty every tenant's cost page."""
    async def _seed(db):
        user = User(email="owner@example.com")
        db.add(user)
        await db.commit()
        return user.id
    uid = _run(sm, _seed)

    openrouter.current_user_id.set(uid)
    try:
        openrouter.record_usage("m", {"total_tokens": 1, "cost": 0.07})
    finally:
        openrouter.current_user_id.set(None)
    _run(sm, app_spend.flush_and_totals)

    rows = _run(sm, lambda db: db.execute(select(LLMUsage)))
    assert [r.user_id for r in rows.scalars().all()] == [uid]
    assert _run(sm, app_spend.app_spend_today) == 0.0


# ── the day's budget, spread so one bad hour cannot take it all ─────────────
#
# The daily ceiling bounds the money and nothing else: a script can reach it in
# ten minutes, and then the landing — the page's whole argument — answers every
# honest visitor with "come back tomorrow" for the rest of the day. The failure
# is not the spend, it is that the door closes and stays closed.
#
# So the same budget also has an hourly share. A burst takes its hour and stops;
# the next hour opens by itself. There is still exactly ONE number to set —
# `app_daily_spend_usd` — because two settings that can disagree are a bug
# waiting for the day one of them is right.


def test_an_hour_is_a_share_of_the_day_not_a_second_setting(sm):
    """The divisor is a constant with a name, and the daily figure stays the
    only thing anybody configures."""
    assert app_spend.hourly_ceiling(12.0) == pytest.approx(12.0 / app_spend.BURST_HOURS)
    assert app_spend.BURST_HOURS > 1, "an hour cannot be allowed the whole day"
    assert app_spend.BURST_HOURS < 24, (
        "spreading the budget evenly over 24 hours refuses a genuinely busy "
        "hour while the day's budget sits untouched")


def test_spending_an_hour_ago_does_not_count_against_this_hour(sm):
    """The window has to move, or the first busy hour of the day closes every
    hour after it."""
    now = datetime.now(timezone.utc)
    _row(sm, cost=5.0, at=now - timedelta(hours=2))
    _row(sm, cost=0.10, at=now)

    day, hour = _run(sm, app_spend.flush_and_totals)
    assert day == pytest.approx(5.10)
    assert hour == pytest.approx(0.10)


def test_a_users_own_spend_does_not_close_the_hour_either(sm):
    """Same rule as the day: their key, their bill, our ceiling untouched."""
    _row(sm, cost=99.0, user_id="a-real-user-id")

    _day, hour = _run(sm, app_spend.flush_and_totals)
    assert hour == pytest.approx(0.0)


def test_the_hourly_read_flushes_like_the_daily_one(sm):
    """A ceiling that cannot see money still in memory is the bug this whole
    module was written for; the hour must not reintroduce it."""
    openrouter.record_usage("m", {"total_tokens": 1, "cost": 0.31})

    _day, hour = _run(sm, app_spend.flush_and_totals)
    assert hour == pytest.approx(0.31)


def test_the_day_is_still_the_hard_stop(sm):
    """The hour spreads the budget; it does not enlarge it. Twelve busy hours
    must not spend twelve times the daily figure."""
    caps = app_spend.ceilings_hit(day=10.0, hour=0.0, daily_cap=10.0)
    assert caps == "day"
    assert app_spend.ceilings_hit(day=1.0, hour=app_spend.hourly_ceiling(10.0),
                                  daily_cap=10.0) == "hour"
    assert app_spend.ceilings_hit(day=1.0, hour=0.0, daily_cap=10.0) is None
