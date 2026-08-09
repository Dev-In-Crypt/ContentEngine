"""The Journal appears when there is a journal to look at.

UX phase 8's table says "after the tenth published post". The code says
otherwise: the journal is written on APPROVAL, one row per sign-off
(`business.py`, inside `approve_post`), and nothing about it waits for a
publication. A workspace can hold nine approvals and have published nothing.

So the tenth-publication gate would have hidden a non-empty audit trail —
complete with its Export CSV button, which is the report an agency hands a
client. That is worse than an empty tab. The trigger is the feature's own
content instead: the Journal appears the moment it has its first entry, which
is exactly what "don't put an empty screen in the menu" was asking for.

Recorded rather than counted, in the sense phase 8.0 set out: the milestone
says the journal became non-empty, an event at the moment it happened — the
same shape as the first-edit milestone, not a copy of a row count.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import AuditEntry, Base, Post, User, Workspace
from services import milestones
from services.user_settings import _CRED_FIELDS

EMAIL = "agency@example.com"


def _platform(db_url: str) -> Settings:
    fields = {f: "" for f in _CRED_FIELDS}
    fields.update(database_url=db_url, api_token="", app_mode="cloud")
    return Settings(**fields)


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'journal.db'}"


@pytest.fixture
def sm(db_url):
    eng = create_async_engine(db_url)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


@pytest.fixture
def client(db_url, sm):
    async def override_db():
        async with sm() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: _platform(db_url)
    app.state.sessionmaker = sm
    yield TestClient(app)
    for dep in (get_db, get_settings):
        app.dependency_overrides.pop(dep, None)


@pytest.fixture
def owner(client, sm):
    """A business account with one in-review draft, ready to be approved."""
    r = client.post("/api/auth/register",
                    json={"email": EMAIL, "password": "password123"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.put("/api/auth/account-type", json={"account_type": "business"},
               headers=headers)
    # A workspace is created on demand by the business routes; asking for the
    # drafts list is the cheapest way to make it exist.
    client.get("/api/business/drafts", headers=headers)

    async def _seed():
        async with sm() as db:
            user = (await db.execute(select(User).where(
                User.email == EMAIL))).scalar_one()
            ws = (await db.execute(select(Workspace))).scalars().first()
            post = Post(user_id=user.id, workspace_id=ws.id,
                        managed_account_id=user.active_account_id,
                        topic="A rate change", format="single", status="in_review",
                        platform="instagram", caption="The rate moved.",
                        ai_caption="The rate moved.")
            db.add(post)
            await db.commit()
            return post.id
    return {"headers": headers, "post_id": asyncio.run(_seed())}


def _reached(sm) -> dict:
    async def _go():
        async with sm() as db:
            user = (await db.execute(select(User).where(
                User.email == EMAIL))).scalar_one()
            return milestones.all_for(user)
    return asyncio.run(_go())


def _entries(sm) -> int:
    async def _go():
        async with sm() as db:
            return len((await db.execute(select(AuditEntry))).scalars().all())
    return asyncio.run(_go())


def _approve(client, owner):
    return client.post(f"/api/business/posts/{owner['post_id']}/approve",
                       headers=owner["headers"])


# ── the moment ──────────────────────────────────────────────────────────────

def test_a_workspace_with_no_approvals_has_no_journal_yet(client, sm, owner):
    assert milestones.JOURNAL_UNLOCKED not in _reached(sm)


def test_the_first_approval_unlocks_the_journal(client, sm, owner):
    """The row and the milestone are the same event: the journal stopped being
    empty. Gating on ten publications instead would hide this row and the
    Export button beside it."""
    r = _approve(client, owner)

    assert r.status_code == 200
    assert _entries(sm) == 1
    assert milestones.JOURNAL_UNLOCKED in _reached(sm)


def test_the_moment_is_the_first_one(client, sm, owner):
    """A second approval is not a second unlocking. Rewriting the timestamp
    would make the record say the feature appeared later than it did."""
    _approve(client, owner)
    first = _reached(sm)[milestones.JOURNAL_UNLOCKED]

    async def _another():
        async with sm() as db:
            post = await db.get(Post, owner["post_id"])
            post.status = "in_review"
            await db.commit()
    asyncio.run(_another())
    _approve(client, owner)

    assert _reached(sm)[milestones.JOURNAL_UNLOCKED] == first


def test_a_refused_approval_unlocks_nothing(client, sm, owner):
    """No audit row, so no journal. A milestone recorded on a 409 would put an
    empty tab on screen — the exact thing this phase exists to avoid."""
    async def _publish_it():
        async with sm() as db:
            post = await db.get(Post, owner["post_id"])
            post.status = "draft"           # only an in-review post can be approved
            await db.commit()
    asyncio.run(_publish_it())

    r = _approve(client, owner)

    assert r.status_code == 409
    assert _entries(sm) == 0
    assert milestones.JOURNAL_UNLOCKED not in _reached(sm)


def test_a_milestone_failure_does_not_lose_the_approval(client, sm, owner,
                                                        monkeypatch):
    """The approval is the fact worth keeping. It is also the legally
    interesting one — a sign-off that 500s because a UI hint could not be
    recorded is a bad trade in every direction."""
    async def _boom(*a, **kw):
        raise RuntimeError("milestone store is down")

    monkeypatch.setattr(milestones, "record", _boom)

    r = _approve(client, owner)

    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    assert _entries(sm) == 1
