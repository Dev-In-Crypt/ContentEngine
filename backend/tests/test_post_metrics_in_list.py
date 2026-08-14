"""The newest metric snapshot, carried on the list row.

The Feed screen showed a link and a date. Everything a person actually goes
there to see — did anyone look at it — lived one post at a time behind a manual
Refresh, on a screen you had to open the post to reach.

Three things this file is careful about, because each is a way to be wrong
quietly:

  * **The NEWEST snapshot.** They accumulate; the oldest one is the one taken
    minutes after publishing, when the number is nearly zero.
  * **None, not zero.** A post nobody has fetched metrics for has no number.
    Rendering that as 0 reach says "nobody saw it", which is a different claim
    and usually a false one.
  * **Per post.** One join written slightly wrong shows every row the same
    numbers, and a screen where every post performed identically looks like a
    working screen.
"""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, Post as PostModel, PostInsight


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'metrics.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


@pytest.fixture
def client(sm, tmp_path):
    settings = Settings(app_mode="local", api_token="",
                        database_url=f"sqlite+aiosqlite:///{tmp_path / 'metrics.db'}")

    async def override_db():
        async with sm() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    for dep in (get_db, get_settings):
        app.dependency_overrides.pop(dep, None)


def _seed(sm, posts):
    """posts: list of (topic, [(day, reach, likes, saved), ...])"""
    ids = []

    async def _go():
        async with sm() as s:
            for topic, snaps in posts:
                pid = str(uuid.uuid4())
                ids.append(pid)
                s.add(PostModel(id=pid, topic=topic, format="single",
                                status="published", platform="instagram"))
                for day, reach, likes, saved in snaps:
                    when = datetime(2026, 8, day, 9, tzinfo=timezone.utc)
                    s.add(PostInsight(post_id=pid, snapshot_at=when,
                                      reach=reach, likes=likes, saved=saved))
            await s.commit()
    asyncio.run(_go())
    return ids


def _by_topic(client):
    rows = client.get("/api/posts").json()
    return {r["topic"]: r for r in rows}


def test_the_newest_snapshot_is_the_one_reported(client, sm):
    """They accumulate. The oldest was taken minutes after publishing."""
    _seed(sm, [("Grinder settings", [
        (10, 120, 4, 1),
        (13, 4100, 318, 41),
    ])])

    row = _by_topic(client)["Grinder settings"]

    assert row["metrics"]["reach"] == 4100
    assert row["metrics"]["likes"] == 318
    assert row["metrics"]["saved"] == 41


def test_a_post_nobody_has_measured_reports_nothing(client, sm):
    """Not zero. "0 reach" is a claim that nobody saw it; the truth is that
    nobody asked Instagram yet."""
    _seed(sm, [("Never refreshed", [])])

    assert _by_topic(client)["Never refreshed"]["metrics"] is None


def test_one_posts_numbers_do_not_appear_on_another(client, sm):
    """A join written slightly wrong gives every row the same numbers, and a
    screen where every post performed identically still looks like it works.

    The timestamps overlap on purpose — "Popular one" was measured twice and
    "Quiet one" once, at the same early moment.

    What this does NOT reach, said plainly: dropping the post_id half of the
    join still passes. The join then matches every row sharing a winning
    timestamp, but the result is keyed by each row's OWN post_id, so the extra
    rows land on their own posts and only the ORDER of the result decides
    whether a stale row overwrites a fresh one. That is a database's choice,
    not something a test can force, so the post_id half of the join is carried
    by the comment beside it rather than by a guard here.
    """
    _seed(sm, [
        ("Popular one", [(10, 120, 4, 1), (13, 4100, 318, 41)]),
        ("Quiet one", [(10, 90, 3, 0)]),
        ("Unmeasured one", []),
    ])

    rows = _by_topic(client)

    assert rows["Popular one"]["metrics"]["reach"] == 4100
    assert rows["Quiet one"]["metrics"]["reach"] == 90
    assert rows["Unmeasured one"]["metrics"] is None
