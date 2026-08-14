"""The Insights rollup — the one screen in this phase built from nothing.

There is no aggregate anywhere in the product today. Everything on the Results
screen is a list the browser filtered. Four numbers and a chart therefore need
a route, and the route needs to be careful about a specific kind of lie:

  * **A total is only over what was measured.** Metrics arrive one post at a
    time, when somebody presses Refresh. "38.2k reach" across eighteen posts
    where four have numbers is not a total, it is a quarter of one — so the
    response says how many of the posts it actually has numbers for, and the
    screen has to print that.
  * **X reports nothing.** Not zero — nothing. An X post in the window is a
    post this screen cannot speak about, and saying so is the difference
    between a quiet week and a blind one.
  * **A delta needs the window before it.** Comparing against everything ever
    published is not a trend, and comparing against nothing at all should
    return no delta rather than "+100%".
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, LLMUsage, Post as PostModel, PostInsight

NOW = datetime.now(timezone.utc)


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ins.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


@pytest.fixture
def client(sm, tmp_path):
    settings = Settings(app_mode="local", api_token="",
                        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ins.db'}")

    async def override_db():
        async with sm() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    for dep in (get_db, get_settings):
        app.dependency_overrides.pop(dep, None)


def _post(sm, *, days_ago, status="published", platform="instagram",
          reach=None, saved=None, scheduled_at=None, topic="A post"):
    pid = str(uuid.uuid4())
    when = NOW - timedelta(days=days_ago)

    async def _go():
        async with sm() as s:
            s.add(PostModel(id=pid, topic=topic, format="single", status=status,
                            platform=platform, published_at=when if status == "published" else None,
                            scheduled_at=scheduled_at, created_at=when))
            if reach is not None:
                s.add(PostInsight(post_id=pid, snapshot_at=when, reach=reach, saved=saved))
            await s.commit()
    asyncio.run(_go())
    return pid


def _spend(sm, *, days_ago, cost):
    async def _go():
        async with sm() as s:
            s.add(LLMUsage(model="m", cost=cost, total_tokens=10,
                           created_at=NOW - timedelta(days=days_ago)))
            await s.commit()
    asyncio.run(_go())


def _get(client, days=30):
    r = client.get(f"/api/insights?days={days}")
    assert r.status_code == 200, r.text
    return r.json()


def test_only_what_went_out_is_counted(client, sm):
    """A draft is not a post that went out, and a scheduled one has not gone
    out yet. Counting either turns "posts out" into "posts made"."""
    _post(sm, days_ago=3, status="published", reach=100)
    _post(sm, days_ago=3, status="draft")
    _post(sm, days_ago=3, status="scheduled")

    assert _get(client)["posts_out"] == 1


def test_it_admits_how_much_of_the_window_it_measured(client, sm):
    """Four numbers over eighteen posts where three were measured is not a
    total. The screen cannot say so unless the route tells it."""
    _post(sm, days_ago=2, reach=4000, saved=40)
    _post(sm, days_ago=3, reach=100, saved=1)
    _post(sm, days_ago=4)                       # published, never refreshed

    body = _get(client)

    assert body["posts_out"] == 3
    assert body["measured_posts"] == 2
    assert body["reach"]["value"] == 4100


def test_a_network_that_reports_nothing_is_named(client, sm):
    """An X post in the window is a post this screen cannot speak about. Left
    unsaid, a blind week looks like a quiet one."""
    _post(sm, days_ago=2, platform="x", topic="A thread")

    assert "x" in _get(client)["networks_without_metrics"]


def test_a_network_absent_from_the_window_is_not_named(client, sm):
    """Warning about X to somebody who does not post on X is noise."""
    _post(sm, days_ago=2, platform="instagram", reach=10)

    assert _get(client)["networks_without_metrics"] == []


def test_the_delta_compares_with_the_window_before_it(client, sm):
    """Not with everything ever published — that is a ratio, not a trend.

    The old post is what makes this a test. Without something OUTSIDE the
    previous window, "the week before" and "everything before" hold the same
    rows and the mutation that swaps one for the other sails through — which is
    exactly what the first version of this did.
    """
    _post(sm, days_ago=2, reach=200)        # this week
    _post(sm, days_ago=9, reach=100)        # the week before
    _post(sm, days_ago=40, reach=900)       # older than the comparison window

    body = _get(client, days=7)

    assert body["reach"]["value"] == 200
    assert body["reach"]["delta_pct"] == pytest.approx(100.0)


def test_no_previous_window_means_no_delta_rather_than_a_hundred_percent(client, sm):
    """Everything is up infinitely from nothing, which is not information."""
    _post(sm, days_ago=2, reach=200)

    assert _get(client, days=7)["reach"]["delta_pct"] is None


def test_on_time_means_it_went_out_when_it_was_meant_to(client, sm):
    late = NOW - timedelta(days=3, hours=2)
    _post(sm, days_ago=3, scheduled_at=late)            # published ~2h after due
    _post(sm, days_ago=4, scheduled_at=NOW - timedelta(days=4))

    body = _get(client)

    assert body["posts_out"] == 2
    assert body["on_time"] == 1


def test_spend_is_the_window_not_all_of_history(client, sm):
    _spend(sm, days_ago=2, cost=1.25)
    _spend(sm, days_ago=90, cost=99.0)

    assert _get(client, days=30)["spend_usd"] == pytest.approx(1.25)
