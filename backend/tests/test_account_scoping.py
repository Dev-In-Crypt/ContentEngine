"""Active-account scoping (Phase 7): the composer view is filtered to the active
managed account. The list filter is the mutation target — dropping it leaks one
client's posts into another's view. Security stays on user_id throughout."""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, ManagedAccount, Post, User
from services.managed_account import resolve_active_account


@pytest.fixture
def ctx(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scope.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    SM = async_sessionmaker(eng, expire_on_commit=False)

    async def override_db():
        async with SM() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: Settings(app_mode="cloud")
    app.state.sessionmaker = SM
    yield TestClient(app), SM
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_settings, None)
    asyncio.run(eng.dispose())


def _register(c, email):
    r = c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _me_id(c, h):
    return c.get("/api/auth/me", headers=h).json()["id"]


def _seed_post(SM, user_id, managed_account_id, topic):
    async def _s():
        async with SM() as db:
            db.add(Post(id=str(uuid.uuid4()), user_id=user_id,
                        managed_account_id=managed_account_id, topic=topic,
                        format="single", status="preview"))
            await db.commit()
    asyncio.run(_s())


def _topics(c, h):
    return {p["topic"] for p in c.get("/api/posts", headers=h).json()}


def _primary(c, h):
    return c.get("/api/accounts", headers=h).json()["active_account_id"]


def test_list_scoped_to_active_account(ctx):
    c, SM = ctx
    h = _register(c, "a@ex.com")
    uid = _me_id(c, h)
    own = _primary(c, h)
    aid = c.post("/api/accounts", headers=h, json={"name": "Client A"}).json()["id"]
    _seed_post(SM, uid, own, "own-post")
    _seed_post(SM, uid, aid, "clientA-post")

    assert _topics(c, h) == {"own-post"}
    # switch to Client A: only its post (mutation guard: drop filter → both leak)
    c.post("/api/accounts/switch", headers=h, json={"account_id": aid})
    assert _topics(c, h) == {"clientA-post"}
    # back to the user's own profile
    c.post("/api/accounts/switch", headers=h, json={"account_id": own})
    assert _topics(c, h) == {"own-post"}


def test_a_solo_creator_sees_their_posts(ctx):
    """Was `test_solo_creator_unaffected`, which seeded posts with NULL because
    "Personal" meant the absence of a row. Since UX phase 2 a solo creator has a
    profile like everyone else and their posts carry its id — the migration and
    ensure_primary_profile are what put it there."""
    c, SM = ctx
    h = _register(c, "solo@ex.com")
    uid = _me_id(c, h)
    own = _primary(c, h)
    _seed_post(SM, uid, own, "p1")
    _seed_post(SM, uid, own, "p2")
    assert _topics(c, h) == {"p1", "p2"}


def test_a_post_left_untagged_is_adopted_not_lost(ctx):
    """The pre-phase-2 shape, arriving through a restore or a stale session: a
    post with no profile at all. The lazy repair adopts it on the next request
    rather than letting it fall out of the filter forever."""
    c, SM = ctx
    h = _register(c, "legacy@ex.com")
    uid = _me_id(c, h)
    _seed_post(SM, uid, None, "from-before")

    async def _strip():
        async with SM() as db:
            await db.execute(text("DELETE FROM managed_accounts WHERE owner_user_id = :u"),
                             {"u": uid})
            await db.execute(text("UPDATE users SET active_account_id = NULL "
                                  "WHERE id = :u"), {"u": uid})
            await db.commit()
    asyncio.run(_strip())

    assert _topics(c, h) == {"from-before"}


def test_cross_user_still_isolated(ctx):
    c, SM = ctx
    ha = _register(c, "x@ex.com")
    hb = _register(c, "y@ex.com")
    _seed_post(SM, _me_id(c, ha), _primary(c, ha), "a-post")
    _seed_post(SM, _me_id(c, hb), _primary(c, hb), "b-post")
    assert _topics(c, ha) == {"a-post"}     # user_id boundary intact
    assert _topics(c, hb) == {"b-post"}


def test_resolve_active_account_unit(ctx):
    c, SM = ctx
    h = _register(c, "r@ex.com")
    uid = _me_id(c, h)
    own = _primary(c, h)
    aid = c.post("/api/accounts", headers=h, json={"name": "Brand"}).json()["id"]

    async def _check():
        async with SM() as db:
            user = await db.get(User, uid)
            assert (await resolve_active_account(db, user)).id == own
            user.active_account_id = aid
            acct = await resolve_active_account(db, user)
            assert acct is not None and acct.id == aid
            # a foreign/stale id must never resolve to another user's account
            user.active_account_id = "does-not-exist"
            assert await resolve_active_account(db, user) is None
    asyncio.run(_check())
