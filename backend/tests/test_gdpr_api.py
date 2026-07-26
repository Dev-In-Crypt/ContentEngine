"""The two GDPR endpoints over HTTP.

Deletion is the dangerous one. A valid JWT is not enough on its own — a stolen
token must not be able to erase somebody's account — so the current password is
required at the moment of the request.
"""
import asyncio
import io
import json
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, Post, Slide, User

PW = "password123"


@pytest.fixture
def ctx(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gdpr_api.db'}")

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
    r = c.post("/api/auth/register", json={"email": email, "password": PW})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _seed_post(SM, user_id, topic="Sourdough", image_path=None):
    pid = str(uuid.uuid4())

    async def _s():
        async with SM() as db:
            db.add(Post(id=pid, user_id=user_id, topic=topic, format="single",
                        status="draft", caption="Fresh loaves"))
            await db.flush()
            db.add(Slide(post_id=pid, slide_number=1, image_source="ai",
                         image_path=image_path))
            await db.commit()
    asyncio.run(_s())
    return pid


def _user_count(SM):
    async def _s():
        async with SM() as db:
            return len((await db.execute(select(User))).scalars().all())
    return asyncio.run(_s())


# ------------------------------------------------------------------ export


def test_export_needs_a_login(ctx):
    c, _ = ctx
    assert c.get("/api/auth/export").status_code == 401


def test_export_returns_a_zip_with_the_account_document(ctx):
    c, SM = ctx
    h = _register(c, "a@example.com")
    _seed_post(SM, c.get("/api/auth/me", headers=h).json()["id"])

    r = c.get("/api/auth/export", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers.get("content-disposition", "")

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        data = json.loads(zf.read("data.json"))
    assert data["account"]["email"] == "a@example.com"
    assert [p["topic"] for p in data["posts"]] == ["Sourdough"]


def test_export_shows_no_other_account(ctx):
    c, SM = ctx
    ha = _register(c, "a@example.com")
    hb = _register(c, "b@example.com")
    _seed_post(SM, c.get("/api/auth/me", headers=hb).json()["id"], topic="Theirs")

    r = c.get("/api/auth/export", headers=ha)
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        blob = zf.read("data.json").decode()
    assert "b@example.com" not in blob
    assert "Theirs" not in blob


def test_export_carries_the_media_it_owns(ctx, tmp_path):
    c, SM = ctx
    h = _register(c, "a@example.com")
    uid = c.get("/api/auth/me", headers=h).json()["id"]
    uploads = tmp_path / "uploads"
    pid = str(uuid.uuid4())
    d = uploads / "posts" / pid
    d.mkdir(parents=True)
    (d / "slide_1.jpg").write_bytes(b"jpeg-bytes")

    async def _s():
        async with SM() as db:
            db.add(Post(id=pid, user_id=uid, topic="T", format="single",
                        status="draft"))
            await db.flush()
            db.add(Slide(post_id=pid, slide_number=1, image_source="ai",
                         image_path=str(d / "slide_1.jpg")))
            await db.commit()
    asyncio.run(_s())

    import services.gdpr as gdpr
    from api.routes import auth as auth_routes
    old = auth_routes.UPLOADS_ROOT
    auth_routes.UPLOADS_ROOT = uploads
    try:
        r = c.get("/api/auth/export", headers=h)
    finally:
        auth_routes.UPLOADS_ROOT = old
    assert gdpr.UPLOADS_ROOT is not None      # module import sanity

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert f"media/posts/{pid}/slide_1.jpg" in zf.namelist()
        assert zf.read(f"media/posts/{pid}/slide_1.jpg") == b"jpeg-bytes"


# ------------------------------------------------------------------ delete


def test_delete_needs_a_login(ctx):
    c, _ = ctx
    assert c.post("/api/auth/delete", json={"password": PW}).status_code == 401


def test_delete_refuses_a_wrong_password(ctx):
    """A stolen token alone must not be enough to erase an account."""
    c, SM = ctx
    h = _register(c, "a@example.com")
    r = c.post("/api/auth/delete", json={"password": "not-my-password"}, headers=h)
    assert r.status_code == 403
    assert _user_count(SM) == 1


def test_delete_refuses_an_empty_password(ctx):
    c, SM = ctx
    h = _register(c, "a@example.com")
    assert c.post("/api/auth/delete", json={"password": ""},
                  headers=h).status_code in (403, 422)
    assert _user_count(SM) == 1


def test_delete_erases_the_account_and_reports_counts(ctx):
    c, SM = ctx
    h = _register(c, "a@example.com")
    _seed_post(SM, c.get("/api/auth/me", headers=h).json()["id"])

    r = c.post("/api/auth/delete", json={"password": PW}, headers=h)
    assert r.status_code == 200
    assert r.json()["deleted"]["posts"] == 1
    assert r.json()["deleted"]["slides"] == 1
    assert _user_count(SM) == 0


def test_the_token_stops_working_once_the_account_is_gone(ctx):
    c, _ = ctx
    h = _register(c, "a@example.com")
    c.post("/api/auth/delete", json={"password": PW}, headers=h)
    assert c.get("/api/auth/me", headers=h).status_code == 401


def test_deleting_one_account_leaves_the_other_intact(ctx):
    c, SM = ctx
    ha = _register(c, "a@example.com")
    hb = _register(c, "b@example.com")
    bid = c.get("/api/auth/me", headers=hb).json()["id"]
    _seed_post(SM, bid, topic="Theirs")

    c.post("/api/auth/delete", json={"password": PW}, headers=ha)

    assert _user_count(SM) == 1
    assert c.get("/api/auth/me", headers=hb).status_code == 200

    async def _s():
        async with SM() as db:
            return (await db.execute(
                select(Post).where(Post.user_id == bid))).scalars().all()
    assert len(asyncio.run(_s())) == 1


def test_the_email_can_be_reused_after_erasure(ctx):
    """Nothing of the account may linger — including the unique-email row."""
    c, _ = ctx
    h = _register(c, "a@example.com")
    c.post("/api/auth/delete", json={"password": PW}, headers=h)
    again = c.post("/api/auth/register",
                   json={"email": "a@example.com", "password": PW})
    assert again.status_code == 200
