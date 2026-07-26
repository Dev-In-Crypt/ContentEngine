"""Rules-only source poller (Phase 2): snapshot-based dedup is the mutation guard."""
import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import services.source_poller as poller
from models.database import Base, Lead, Source, SourceSnapshot, Workspace
from services.sources.base import FetchedItem, SourceRateLimited


class _FakeFetcher:
    def __init__(self, items):
        self._items = items

    async def fetch(self, url, since=None):
        return self._items


def _items():
    return [
        FetchedItem(external_id="1", kind="github_releases", title="New pricing tier",
                    url="https://ex.com/1", published_at=None, body="Prices changed."),
        FetchedItem(external_id="2", kind="github_releases", title="chore: bump deps",
                    url="https://ex.com/2", published_at=None, body=""),
    ]


@pytest.fixture
def db_setup(tmp_path, monkeypatch):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'poll.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    SM = async_sessionmaker(eng, expire_on_commit=False)

    async def _seed():
        async with SM() as db:
            ws = Workspace(owner_user_id="uA", name="A")
            db.add(ws)
            await db.commit()
            await db.refresh(ws)
            src = Source(workspace_id=ws.id, url="https://github.com/o/r",
                         kind="github_releases", status="ok", active=True)
            db.add(src)
            await db.commit()
            await db.refresh(src)
            return src.id
    src_id = asyncio.run(_seed())
    monkeypatch.setattr(poller, "get_source_fetcher",
                        lambda kind, ssl_verify=True: _FakeFetcher(_items()))
    yield SM, src_id
    asyncio.run(eng.dispose())


def _count(SM, model):
    async def _c():
        async with SM() as db:
            return (await db.execute(select(func.count()).select_from(model))).scalar()
    return asyncio.run(_c())


def test_first_poll_creates_leads_and_snapshots(db_setup):
    SM, _ = db_setup
    result = asyncio.run(poller.poll_all(SM))
    assert result == {"sources": 1, "leads": 2}    # worthy + weak both kept
    assert _count(SM, Lead) == 2
    assert _count(SM, SourceSnapshot) == 2


def test_repoll_same_items_creates_no_duplicates(db_setup):
    SM, _ = db_setup
    asyncio.run(poller.poll_all(SM))
    result = asyncio.run(poller.poll_all(SM))    # same items again
    assert result["leads"] == 0                  # snapshot dedup — mutation guard
    assert _count(SM, Lead) == 2


def test_weak_kept_duplicate_skipped(db_setup):
    SM, src_id = db_setup
    # Seed a recent lead whose title matches item 1 → item 1 scores "duplicate".
    async def _seed_dup():
        async with SM() as db:
            src = await db.get(Source, src_id)
            db.add(Lead(workspace_id=src.workspace_id, source_id=src.id,
                        what_happened="New pricing tier", strength="worthy", status="new"))
            await db.commit()
    asyncio.run(_seed_dup())
    asyncio.run(poller.poll_all(SM))
    # item 1 (dup of the seeded title) skipped; item 2 (weak) kept → 1 + seeded = 2
    async def _titles():
        async with SM() as db:
            return sorted((await db.execute(select(Lead.what_happened))).scalars().all())
    titles = asyncio.run(_titles())
    assert titles.count("New pricing tier") == 1          # not duplicated
    assert "chore: bump deps" in titles                   # weak still written


def test_rate_limited_source_is_not_marked_unreachable(db_setup, monkeypatch):
    """"Try again in an hour" must not look like "this URL is dead" — a user shown
    "Unreachable" deletes a source that was working fine. Mutation guard: catch
    SourceFetchError first and the rate-limit branch becomes unreachable code."""
    class _Limited:
        async def fetch(self, url, since=None):
            raise SourceRateLimited("GitHub: rate limit reached — resets at 08:35 UTC.")

    SM, src_id = db_setup
    monkeypatch.setattr(poller, "get_source_fetcher", lambda kind, ssl_verify=True: _Limited())
    asyncio.run(poller.poll_all(SM))

    async def _status():
        async with SM() as db:
            return (await db.get(Source, src_id)).status
    assert asyncio.run(_status()) == "rate_limited"
