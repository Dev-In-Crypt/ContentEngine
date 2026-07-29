"""The media library API: list / get / delete / serve / manual upload.

The workspace_id filter in Business has its counterpart here as user_id — the
mutation guard on every route is the same shape: user B must never see, read,
or delete user A's asset.
"""
import asyncio
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, MediaAsset
from services import media_store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(media_store, "MEDIA_ROOT", tmp_path / "media")
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'media.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    SM = async_sessionmaker(eng, expire_on_commit=False)

    async def override_db():
        async with SM() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    # Cloud mode: get_current_user otherwise falls back to the single implicit
    # local user and ignores the Authorization header entirely, which would
    # make every isolation test here pass for the wrong reason.
    app.dependency_overrides[get_settings] = lambda: Settings(app_mode="cloud")
    app.state.sessionmaker = SM
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_settings, None)
    asyncio.run(eng.dispose())


def _register(client, email):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123", "account_type": "creator"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _upload(client, headers, data=b"jpeg-bytes", mime="image/jpeg", name="pic.jpg"):
    return client.post("/api/media/uploads", headers=headers,
                       files={"files": (name, io.BytesIO(data), mime)})


# ------------------------------------------------------------------ upload


def test_uploading_an_image_lands_in_the_library(client):
    h = _register(client, "a@ex.com")
    r = _upload(client, h)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["kind"] == "image"
    assert body[0]["status"] == "ready"
    assert body[0]["source"] == "upload"
    assert body[0]["url"] == f"/api/media/{body[0]['id']}/file"


def test_upload_records_a_file_path_gdpr_can_walk(client):
    """gdpr.user_media_paths() decides what goes in the export ZIP by reading
    this column directly, not by asking media_store. An asset whose file
    exists on disk but whose row never says where is invisible to that export
    — silently, since nothing else would notice the column was empty."""
    h = _register(client, "a@ex.com")
    asset_id = _upload(client, h, data=b"exportable").json()[0]["id"]

    async def _fetch():
        async with app.state.sessionmaker() as db:
            return await db.get(MediaAsset, asset_id)
    asset = asyncio.run(_fetch())
    assert asset.file_path
    assert open(asset.file_path, "rb").read() == b"exportable"


def test_uploading_a_video_is_accepted_too(client):
    h = _register(client, "a@ex.com")
    r = _upload(client, h, data=b"mp4-bytes", mime="video/mp4", name="clip.mp4")
    assert r.status_code == 200
    assert r.json()[0]["kind"] == "video"


def test_wrong_mime_is_refused(client):
    h = _register(client, "a@ex.com")
    r = _upload(client, h, mime="application/pdf")
    assert r.status_code == 415


def test_empty_upload_list_is_refused(client):
    """No `files` part at all is what an empty list actually sends over
    multipart, and FastAPI's own required-field validation catches that before
    the route body runs — 422, the same as any other missing required field."""
    h = _register(client, "a@ex.com")
    r = client.post("/api/media/uploads", headers=h, files=[])
    assert r.status_code == 422


def test_an_oversized_image_is_refused_and_leaves_no_file_or_row(client, tmp_path):
    h = _register(client, "a@ex.com")
    big = b"x" * (21 * 1024 * 1024)
    r = _upload(client, h, data=big)
    assert r.status_code == 413
    assert list((tmp_path / "media").rglob("*")) == []


def test_an_empty_file_is_refused(client):
    h = _register(client, "a@ex.com")
    r = _upload(client, h, data=b"")
    assert r.status_code == 400


def test_an_unauthenticated_upload_is_refused(client):
    r = _upload(client, headers={})
    assert r.status_code == 401


# ------------------------------------------------------------------ list


def test_list_is_scoped_to_the_uploading_tenant(client):
    a = _register(client, "a@ex.com")
    b = _register(client, "b@ex.com")
    _upload(client, a)
    assert len(client.get("/api/media", headers=a).json()) == 1
    assert client.get("/api/media", headers=b).json() == []


def test_list_can_filter_by_kind(client):
    h = _register(client, "a@ex.com")
    _upload(client, h)
    _upload(client, h, data=b"mp4-bytes", mime="video/mp4", name="clip.mp4")
    assert len(client.get("/api/media?kind=image", headers=h).json()) == 1
    assert len(client.get("/api/media?kind=video", headers=h).json()) == 1
    assert len(client.get("/api/media", headers=h).json()) == 2


def test_list_newest_first(client):
    """Seeded with explicit timestamps, not two rapid uploads: SQLite's
    CURRENT_TIMESTAMP is second-resolution, so two inserts in the same test
    tick for an identical created_at and the real ordering would be whichever
    way ties happen to fall — this asserts the ORDER BY, not the clock."""
    h = _register(client, "a@ex.com")
    user_id = _upload(client, h, name="first.jpg").json()[0]  # discarded, just to create the user row path
    asset_id = user_id["id"]

    async def _seed():
        from datetime import datetime, timedelta, timezone
        async with app.state.sessionmaker() as db:
            first = await db.get(MediaAsset, asset_id)
            first.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            db.add(MediaAsset(user_id=first.user_id, kind="image", source="upload",
                              status="ready", title="second.jpg",
                              created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
                              + timedelta(seconds=5)))
            await db.commit()
    asyncio.run(_seed())

    names = [a["title"] for a in client.get("/api/media", headers=h).json()]
    assert names == ["second.jpg", "first.jpg"]


# ------------------------------------------------------------------ get


def test_get_returns_the_full_detail(client):
    h = _register(client, "a@ex.com")
    asset_id = _upload(client, h).json()[0]["id"]
    r = client.get(f"/api/media/{asset_id}", headers=h)
    assert r.status_code == 200
    assert r.json()["id"] == asset_id


def test_get_of_another_tenants_asset_is_404_not_403(client):
    """404, not 403 — don't reveal that another tenant's asset exists."""
    a = _register(client, "a@ex.com")
    b = _register(client, "b@ex.com")
    asset_id = _upload(client, a).json()[0]["id"]
    assert client.get(f"/api/media/{asset_id}", headers=b).status_code == 404


def test_get_of_an_unknown_id_is_404(client):
    h = _register(client, "a@ex.com")
    assert client.get("/api/media/does-not-exist", headers=h).status_code == 404


# ------------------------------------------------------------------ delete


def test_delete_removes_the_row_and_the_file(client, tmp_path):
    h = _register(client, "a@ex.com")
    asset_id = _upload(client, h).json()[0]["id"]
    assert client.delete(f"/api/media/{asset_id}", headers=h).status_code == 204
    assert client.get(f"/api/media/{asset_id}", headers=h).status_code == 404
    assert list((tmp_path / "media").rglob("*.jpg")) == []


def test_delete_of_another_tenants_asset_is_404_and_leaves_it_intact(client):
    a = _register(client, "a@ex.com")
    b = _register(client, "b@ex.com")
    asset_id = _upload(client, a).json()[0]["id"]
    assert client.delete(f"/api/media/{asset_id}", headers=b).status_code == 404
    assert client.get(f"/api/media/{asset_id}", headers=a).status_code == 200


# ------------------------------------------------------------------ serve bytes


def test_serving_the_file_returns_the_bytes(client):
    h = _register(client, "a@ex.com")
    asset_id = _upload(client, h, data=b"the actual pixels").json()[0]["id"]
    r = client.get(f"/api/media/{asset_id}/file")
    assert r.status_code == 200
    assert r.content == b"the actual pixels"
    assert r.headers["content-type"] == "image/jpeg"


def test_serving_the_file_needs_no_auth_header(client):
    """<img src> and <video src> cannot carry a Bearer token — same posture as
    slide images and reel video."""
    h = _register(client, "a@ex.com")
    asset_id = _upload(client, h).json()[0]["id"]
    assert client.get(f"/api/media/{asset_id}/file").status_code == 200


def test_serving_marks_the_response_uncacheable_by_shared_caches(client):
    h = _register(client, "a@ex.com")
    asset_id = _upload(client, h).json()[0]["id"]
    r = client.get(f"/api/media/{asset_id}/file")
    assert "private" in r.headers["cache-control"]


def test_a_pending_asset_serves_404_not_a_broken_file(client, tmp_path):
    """A generated video's row exists before its bytes do. Serving it before
    status=ready would hand back either nothing or, worse, a half-written file."""
    async def _seed():
        async with app.state.sessionmaker() as db:
            db.add(MediaAsset(id="11111111-1111-4111-8111-111111111111",
                              user_id="whoever", kind="video", source="ai_gen",
                              status="pending"))
            await db.commit()
    asyncio.run(_seed())
    r = client.get("/api/media/11111111-1111-4111-8111-111111111111/file")
    assert r.status_code == 404


def test_a_failed_asset_with_a_leftover_file_still_404s(client, tmp_path):
    """The ready-only gate and the file-exists gate are two different checks,
    not one: a failed or still-downloading asset can have real bytes on disk —
    a partial write from an interrupted download, or a stale file from a
    retried generation — and status must refuse it even though the file is
    right there and would otherwise serve fine."""
    asset_id = "22222222-2222-4222-8222-222222222222"

    async def _seed():
        async with app.state.sessionmaker() as db:
            db.add(MediaAsset(id=asset_id, user_id="someone", kind="video",
                              source="ai_gen", status="failed", mime="video/mp4"))
            await db.commit()
    asyncio.run(_seed())
    media_store.save("someone", asset_id, b"partial bytes", "video/mp4",
                     root=media_store.MEDIA_ROOT)

    assert client.get(f"/api/media/{asset_id}/file").status_code == 404


def test_serving_reads_the_real_file_not_a_forged_file_path_column(client, tmp_path):
    """The path is re-derived from media_store rather than trusted off the row —
    a bad write that put an unexpected path in file_path must not be servable."""
    h = _register(client, "a@ex.com")
    asset_id = _upload(client, h).json()[0]["id"]

    async def _tamper():
        async with app.state.sessionmaker() as db:
            asset = await db.get(MediaAsset, asset_id)
            asset.file_path = str(tmp_path / "outside-secret.jpg")
            await db.commit()
    (tmp_path / "outside-secret.jpg").write_bytes(b"not yours")
    asyncio.run(_tamper())

    r = client.get(f"/api/media/{asset_id}/file")
    assert r.status_code == 200
    assert r.content != b"not yours"
