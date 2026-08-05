"""Managed accounts (Phase 7): CRUD + switch + owner isolation.

The owner_user_id filter is the mutation target — one agency must never see or
switch into another's client accounts.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, Post


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'acct.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


def _bind(SM, mode):
    async def override_db():
        async with SM() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: Settings(app_mode=mode)
    app.state.sessionmaker = SM
    return TestClient(app)


@pytest.fixture
def client(sm):
    yield _bind(sm, "cloud")
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
def local_client(sm):
    yield _bind(sm, "local")
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_settings, None)


def _register(c, email):
    r = c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_account_crud_and_switch(client):
    h = _register(client, "a@ex.com")
    # Since UX phase 2 a fresh account already owns its primary profile, and
    # that profile is what's active. "Personal" as an absence no longer exists.
    listing = client.get("/api/accounts", headers=h).json()
    assert len(listing["accounts"]) == 1
    assert listing["accounts"][0]["is_primary"] is True
    assert listing["active_account_id"] == listing["accounts"][0]["id"]

    aid = client.post("/api/accounts", headers=h, json={"name": "Client A"}).json()["id"]
    client.put(f"/api/accounts/{aid}", headers=h,
               json={"niche": "Fitness", "slide_accent_color": "#ff751f"})
    got = client.get(f"/api/accounts/{aid}", headers=h).json()
    assert got["name"] == "Client A" and got["niche"] == "Fitness"
    assert got["slide_accent_color"] == "#ff751f"

    lst = client.get("/api/accounts", headers=h).json()
    # The primary leads regardless of when the client brand was made, so the SPA
    # can render the list in order without knowing anything about it.
    assert [a["name"] for a in lst["accounts"]] == ["Personal", "Client A"]
    assert [a["is_primary"] for a in lst["accounts"]] == [True, False]

    # switch → /me reflects it
    client.post("/api/accounts/switch", headers=h, json={"account_id": aid})
    assert client.get("/api/auth/me", headers=h).json()["active_account_id"] == aid


def test_delete_falls_back_to_the_primary(client):
    """Deleting the brand you were working in leaves active_account_id empty,
    and the next request repairs it to the primary rather than leaving the user
    with no brand at all — which, now that the list filters on the active
    account, would mean an empty feed."""
    h = _register(client, "d@ex.com")
    primary = client.get("/api/accounts", headers=h).json()["active_account_id"]
    aid = client.post("/api/accounts", headers=h, json={"name": "X"}).json()["id"]
    client.post("/api/accounts/switch", headers=h, json={"account_id": aid})
    client.delete(f"/api/accounts/{aid}", headers=h)
    assert client.get("/api/auth/me", headers=h).json()["active_account_id"] == primary
    assert client.get(f"/api/accounts/{aid}", headers=h).status_code == 404


def test_the_primary_profile_cannot_be_deleted(client):
    """It is the one row the legacy User columns mirror and the fallback every
    other invariant leans on. Nothing in the product offers this, so a 409 here
    is a backstop against a hand-rolled request, not a UI affordance."""
    h = _register(client, "keep@ex.com")
    primary = client.get("/api/accounts", headers=h).json()["active_account_id"]
    assert client.delete(f"/api/accounts/{primary}", headers=h).status_code == 409
    assert client.get(f"/api/accounts/{primary}", headers=h).status_code == 200


def test_deleting_a_brand_moves_its_posts_to_the_primary(client, sm):
    """NULL used to mean "Personal" and was visible. Now it is visible to
    nobody, so a delete that merely untags is a delete that silently destroys
    the work done under that brand."""
    h = _register(client, "move@ex.com")
    uid = client.get("/api/auth/me", headers=h).json()["id"]
    primary = client.get("/api/accounts", headers=h).json()["active_account_id"]
    aid = client.post("/api/accounts", headers=h, json={"name": "Client A"}).json()["id"]

    async def _seed():
        async with sm() as db:
            db.add(Post(id="p-client", user_id=uid, managed_account_id=aid,
                        topic="client-work", format="single", status="preview"))
            await db.commit()
    asyncio.run(_seed())

    assert client.delete(f"/api/accounts/{aid}", headers=h).status_code == 200
    client.post("/api/accounts/switch", headers=h, json={"account_id": primary})
    assert {p["topic"] for p in client.get("/api/posts", headers=h).json()} == {
        "client-work"}


def test_switching_to_nothing_selects_the_primary(client):
    """A browser holding a cached index.html keeps posting {"account_id": null}
    for as long as its cache lives. Reading it as "my own profile" is what the
    person meant by Personal anyway; a 422 would turn a stale cache into a
    broken switcher."""
    h = _register(client, "null@ex.com")
    primary = client.get("/api/accounts", headers=h).json()["active_account_id"]
    aid = client.post("/api/accounts", headers=h, json={"name": "Client A"}).json()["id"]
    client.post("/api/accounts/switch", headers=h, json={"account_id": aid})

    r = client.post("/api/accounts/switch", headers=h, json={"account_id": None})
    assert r.status_code == 200 and r.json()["active_account_id"] == primary


def test_register_creates_a_primary_profile(client):
    """Registration is one of the two seeding doors — without it a brand-new
    user would depend on the lazy repair to get a brand at all."""
    h = _register(client, "new@ex.com")
    lst = client.get("/api/accounts", headers=h).json()
    assert len(lst["accounts"]) == 1 and lst["accounts"][0]["is_primary"]


def test_the_primary_reports_the_logo_settings_uploaded(client):
    """has_logo used to be derived from a file named acct_<id>, while the logo
    actually rendered comes from the logo_path column. For a seeded primary
    those disagree by construction — its file is stored under the user's id —
    so the Brands list said "no logo" while the slides carried one."""
    import io

    from PIL import Image

    h = _register(client, "logo@ex.com")
    primary = client.get("/api/accounts", headers=h).json()["active_account_id"]
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    r = client.post("/api/settings/logo", headers=h,
                    files={"file": ("logo.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200, r.text

    assert client.get(f"/api/accounts/{primary}", headers=h).json()["has_logo"] is True
    assert client.get(f"/api/accounts/{primary}/logo/image",
                      headers=h).status_code == 200


def test_invalid_hex_rejected(client):
    h = _register(client, "hex@ex.com")
    aid = client.post("/api/accounts", headers=h, json={"name": "X"}).json()["id"]
    assert client.put(f"/api/accounts/{aid}", headers=h,
                      json={"slide_accent_color": "red"}).status_code == 422


def test_a_user_without_a_profile_gets_one_on_first_request(client, tmp_path):
    """The lazy repair. It covers the deploy window (old container still
    registering users while the new one migrates), a restore from a pg_dump
    predating the migration, and the local desktop user created on demand. It is
    what makes "every user has a profile" total rather than "true as long as the
    migration ran"."""
    h = _register(client, "lazy@ex.com")
    uid = client.get("/api/auth/me", headers=h).json()["id"]

    # Rewind this user to the pre-phase-2 world: no profile, nothing active.
    async def _strip():
        async with app.state.sessionmaker() as db:
            await db.execute(text("DELETE FROM managed_accounts WHERE owner_user_id = :u"),
                             {"u": uid})
            await db.execute(text("UPDATE users SET active_account_id = NULL "
                                  "WHERE id = :u"), {"u": uid})
            await db.commit()
    asyncio.run(_strip())

    assert client.get("/api/auth/me", headers=h).json()["active_account_id"]
    assert len(client.get("/api/accounts", headers=h).json()["accounts"]) == 1


def test_the_local_desktop_user_gets_a_profile(local_client):
    """No login, no registration — the only door is the request itself."""
    lst = local_client.get("/api/accounts").json()
    assert len(lst["accounts"]) == 1 and lst["accounts"][0]["is_primary"]
    assert lst["active_account_id"] == lst["accounts"][0]["id"]


def test_owner_isolation(client):
    ha = _register(client, "own-a@ex.com")
    hb = _register(client, "own-b@ex.com")
    aid = client.post("/api/accounts", headers=ha, json={"name": "A's client"}).json()["id"]
    # B sees their own profile and none of A's, and can't fetch/switch/delete them
    assert [a["name"] for a in
            client.get("/api/accounts", headers=hb).json()["accounts"]] == ["Personal"]
    assert client.get(f"/api/accounts/{aid}", headers=hb).status_code == 404
    assert client.post("/api/accounts/switch", headers=hb, json={"account_id": aid}).status_code == 404
    assert client.delete(f"/api/accounts/{aid}", headers=hb).status_code == 404
