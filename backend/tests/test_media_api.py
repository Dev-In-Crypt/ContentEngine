"""The media library API: list / get / delete / serve / manual upload.

The workspace_id filter in Business has its counterpart here as user_id — the
mutation guard on every route is the same shape: user B must never see, read,
or delete user A's asset.
"""
import asyncio
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_image_provider, get_settings
from config import Settings
from main import app
from models.database import Base, MediaAsset, User
from services import media_store
from services.ai.base import AIError


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


# ------------------------------------------------------------------ generate (AI)

# A 1x1 transparent PNG — small enough to inline, real enough for PIL to probe.
_PNG_PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082".replace("\n", ""))


class _FakeImageProvider:
    def __init__(self, data=None, error=None):
        self._data, self._error = data, error

    async def generate_image(self, model, prompt):
        if self._error:
            raise self._error
        return self._data

    async def close(self):
        pass


def _override_image_provider(fake):
    app.dependency_overrides[get_image_provider] = lambda: fake


def _set_image_model(client, headers, provider="openrouter", model="google/gemini-image"):
    r = client.put("/api/settings/ai", headers=headers,
                   json={"image_provider": provider, "image_model": model})
    assert r.status_code == 200


def test_generate_without_a_provider_configured_never_calls_a_model(client):
    """A brand-new account has no provider and no key — the real dependency
    (not overridden) must refuse before any bytes are requested."""
    h = _register(client, "a@ex.com")
    r = client.post("/api/media/images", headers=h, json={"prompt": "a cat on a windowsill"})
    assert r.status_code == 400
    assert "provider" in r.json()["detail"].lower()
    assert client.get("/api/media", headers=h).json() == []


def test_generate_without_a_model_selected(client):
    """Provider chosen, model left blank — a different refusal from "no
    provider at all", and the one a half-finished settings page produces."""
    h = _register(client, "a@ex.com")
    client.put("/api/settings/ai", headers=h, json={"image_provider": "openrouter"})
    _override_image_provider(_FakeImageProvider(data=_PNG_PIXEL))
    try:
        r = client.post("/api/media/images", headers=h, json={"prompt": "a cat on a windowsill"})
    finally:
        app.dependency_overrides.pop(get_image_provider, None)
    assert r.status_code == 400
    assert "model" in r.json()["detail"].lower()


def test_a_successful_generation_lands_in_the_library(client):
    h = _register(client, "a@ex.com")
    _set_image_model(client, h)
    _override_image_provider(_FakeImageProvider(data=_PNG_PIXEL))
    try:
        r = client.post("/api/media/images", headers=h,
                        json={"prompt": "a cat on a windowsill", "title": "cat pic"})
    finally:
        app.dependency_overrides.pop(get_image_provider, None)

    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "image"
    assert body["status"] == "ready"
    assert body["source"] == "ai_gen"
    assert body["provider"] == "openrouter"
    assert body["model"] == "google/gemini-image"
    assert body["prompt"] == "a cat on a windowsill"
    assert body["width"] == 1 and body["height"] == 1
    assert body["url"] == f"/api/media/{body['id']}/file"

    served = client.get(body["url"])
    assert served.status_code == 200
    assert served.content == _PNG_PIXEL
    assert len(client.get("/api/media", headers=h).json()) == 1


def test_a_provider_failure_creates_no_asset(client):
    """A refusal must not leave a half-made row behind for the user to find
    and be confused by — there is no post standing in front of this one that
    the pattern elsewhere in the app leans on."""
    h = _register(client, "a@ex.com")
    _set_image_model(client, h)
    _override_image_provider(_FakeImageProvider(error=AIError("The provider rejected the key.")))
    try:
        r = client.post("/api/media/images", headers=h, json={"prompt": "a cat on a windowsill"})
    finally:
        app.dependency_overrides.pop(get_image_provider, None)

    assert r.status_code == 502
    assert "rejected the key" in r.json()["detail"]
    assert client.get("/api/media", headers=h).json() == []


def test_a_too_short_prompt_is_rejected(client):
    h = _register(client, "a@ex.com")
    _set_image_model(client, h)
    _override_image_provider(_FakeImageProvider(data=_PNG_PIXEL))
    try:
        r = client.post("/api/media/images", headers=h, json={"prompt": "ok"})
    finally:
        app.dependency_overrides.pop(get_image_provider, None)
    assert r.status_code == 422
    assert client.get("/api/media", headers=h).json() == []


def test_generated_images_are_isolated_by_tenant(client):
    a = _register(client, "a@ex.com")
    b = _register(client, "b@ex.com")
    _set_image_model(client, a)
    _override_image_provider(_FakeImageProvider(data=_PNG_PIXEL))
    try:
        r = client.post("/api/media/images", headers=a, json={"prompt": "a cat on a windowsill"})
    finally:
        app.dependency_overrides.pop(get_image_provider, None)
    assert r.status_code == 200          # would otherwise pass vacuously
    assert client.get("/api/media", headers=b).json() == []


# ------------------------------------------------------------------ stage (into staging)

def test_staging_a_library_image_returns_an_id_that_generation_can_use(client):
    """Composes two already-tested stores rather than teaching generation a
    third source of media: the returned id is a normal staging id."""
    from services import staging
    h = _register(client, "a@ex.com")
    asset_id = _upload(client, h, data=b"library photo").json()[0]["id"]

    r = client.post(f"/api/media/{asset_id}/stage", headers=h)
    assert r.status_code == 200
    staged = r.json()
    assert staged["bytes"] == len(b"library photo")

    async def _find_user_id():
        async with app.state.sessionmaker() as db:
            return (await db.get(MediaAsset, asset_id)).user_id
    user_id = asyncio.run(_find_user_id())
    assert staging.read(user_id, staged["id"]) == b"library photo"


def test_staging_a_video_asset_is_refused(client):
    h = _register(client, "a@ex.com")
    asset_id = _upload(client, h, data=b"mp4-bytes", mime="video/mp4", name="c.mp4").json()[0]["id"]
    r = client.post(f"/api/media/{asset_id}/stage", headers=h)
    assert r.status_code == 400


def test_staging_a_pending_asset_is_refused(client):
    h = _register(client, "a@ex.com")
    asset_id = "33333333-3333-4333-8333-333333333333"

    async def _seed():
        async with app.state.sessionmaker() as db:
            user_id = (await db.execute(
                select(User).where(User.email == "a@ex.com"))).scalar_one().id
            db.add(MediaAsset(id=asset_id, user_id=user_id, kind="image",
                              source="ai_gen", status="pending"))
            await db.commit()
    asyncio.run(_seed())
    r = client.post(f"/api/media/{asset_id}/stage", headers=h)
    assert r.status_code == 400


def test_staging_another_tenants_asset_is_404(client):
    a = _register(client, "a@ex.com")
    b = _register(client, "b@ex.com")
    asset_id = _upload(client, a).json()[0]["id"]
    assert client.post(f"/api/media/{asset_id}/stage", headers=b).status_code == 404


# ------------------------------------------------------------------ generate (video)

class _FakeVideoProvider:
    def __init__(self, task_id="text2video:abc123", error=None, create_calls=None):
        self._task_id, self._error = task_id, error
        self.create_calls = create_calls if create_calls is not None else []

    async def create_task(self, **kwargs):
        self.create_calls.append(kwargs)
        if self._error:
            raise self._error
        return self._task_id

    async def poll(self, task_id):
        raise NotImplementedError

    async def download(self, url):
        raise NotImplementedError

    async def close(self):
        pass


def _override_gen_video_provider(monkeypatch, fake):
    import api.routes.media as media_routes
    monkeypatch.setattr(media_routes, "get_gen_video_provider", lambda *a, **kw: fake)


def _set_kling_key(client, headers, key="sk-kling-test"):
    r = client.put("/api/settings/credentials", headers=headers,
                   json={"kling_api_key": key})
    assert r.status_code == 200


def test_generate_video_without_a_key_never_calls_the_provider(client, monkeypatch):
    h = _register(client, "a@ex.com")
    fake = _FakeVideoProvider()
    _override_gen_video_provider(monkeypatch, fake)
    r = client.post("/api/media/videos", headers=h,
                    json={"prompt": "a cat walking on a windowsill"})
    assert r.status_code == 400
    assert fake.create_calls == []
    assert client.get("/api/media?kind=video", headers=h).json() == []


def test_generate_video_success_creates_a_pending_asset(client, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_kling_key(client, h)
    fake = _FakeVideoProvider()
    _override_gen_video_provider(monkeypatch, fake)

    r = client.post("/api/media/videos", headers=h,
                    json={"prompt": "a cat walking on a windowsill",
                          "model": "kling-v1-6", "duration_sec": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "video"
    assert body["status"] == "pending"
    assert body["url"] is None
    assert body["provider"] == "kling"
    assert body["model"] == "kling-v1-6"
    assert len(fake.create_calls) == 1
    assert fake.create_calls[0]["prompt"] == "a cat walking on a windowsill"
    assert fake.create_calls[0]["duration_sec"] == 5
    assert fake.create_calls[0].get("image_bytes") is None


def test_generate_video_records_a_cost_estimate(client, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_kling_key(client, h)
    _override_gen_video_provider(monkeypatch, _FakeVideoProvider())
    asset_id = client.post("/api/media/videos", headers=h,
                           json={"prompt": "a cat walking on a windowsill",
                                 "model": "kling-v1-6", "duration_sec": 10}).json()["id"]

    async def _fetch():
        async with app.state.sessionmaker() as db:
            return await db.get(MediaAsset, asset_id)
    asset = asyncio.run(_fetch())
    assert asset.cost_usd == pytest.approx(10 * 0.075, rel=0.01)


def test_generate_video_with_a_seed_image_uses_image_to_video(client, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_kling_key(client, h)
    image_id = _upload(client, h, data=b"seed image bytes").json()[0]["id"]
    fake = _FakeVideoProvider()
    _override_gen_video_provider(monkeypatch, fake)

    r = client.post("/api/media/videos", headers=h,
                    json={"prompt": "make it move", "image_asset_id": image_id})
    assert r.status_code == 200
    assert fake.create_calls[0]["image_bytes"] == b"seed image bytes"


def test_generate_video_seed_image_must_be_ready(client, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_kling_key(client, h)
    asset_id = "44444444-4444-4444-8444-444444444444"

    async def _seed():
        async with app.state.sessionmaker() as db:
            user_id = (await db.execute(
                select(User).where(User.email == "a@ex.com"))).scalar_one().id
            db.add(MediaAsset(id=asset_id, user_id=user_id, kind="image",
                              source="ai_gen", status="pending"))
            await db.commit()
    asyncio.run(_seed())
    _override_gen_video_provider(monkeypatch, _FakeVideoProvider())

    r = client.post("/api/media/videos", headers=h,
                    json={"prompt": "make it move", "image_asset_id": asset_id})
    assert r.status_code == 400


def test_generate_video_seed_image_must_be_an_image(client, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_kling_key(client, h)
    video_id = _upload(client, h, data=b"mp4", mime="video/mp4", name="c.mp4").json()[0]["id"]
    _override_gen_video_provider(monkeypatch, _FakeVideoProvider())

    r = client.post("/api/media/videos", headers=h,
                    json={"prompt": "make it move", "image_asset_id": video_id})
    assert r.status_code == 400


def test_generate_video_seed_image_cross_tenant_is_404(client, monkeypatch):
    a = _register(client, "a@ex.com")
    b = _register(client, "b@ex.com")
    _set_kling_key(client, b)
    image_id = _upload(client, a).json()[0]["id"]
    _override_gen_video_provider(monkeypatch, _FakeVideoProvider())

    r = client.post("/api/media/videos", headers=b,
                    json={"prompt": "make it move", "image_asset_id": image_id})
    assert r.status_code == 404


def test_generate_video_provider_failure_creates_no_asset(client, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_kling_key(client, h)
    from services.video.genai.base import VideoGenError
    fake = _FakeVideoProvider(error=VideoGenError("Kling rejected the key"))
    _override_gen_video_provider(monkeypatch, fake)

    r = client.post("/api/media/videos", headers=h,
                    json={"prompt": "a cat walking on a windowsill"})
    assert r.status_code == 502
    assert "rejected the key" in r.json()["detail"]
    assert client.get("/api/media?kind=video", headers=h).json() == []


def test_generate_video_is_isolated_by_tenant(client, monkeypatch):
    a = _register(client, "a@ex.com")
    b = _register(client, "b@ex.com")
    _set_kling_key(client, a)
    _override_gen_video_provider(monkeypatch, _FakeVideoProvider())
    r = client.post("/api/media/videos", headers=a,
                    json={"prompt": "a cat walking on a windowsill"})
    assert r.status_code == 200
    assert client.get("/api/media?kind=video", headers=b).json() == []


def test_generate_video_rejects_a_too_short_prompt(client, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_kling_key(client, h)
    fake = _FakeVideoProvider()
    _override_gen_video_provider(monkeypatch, fake)
    r = client.post("/api/media/videos", headers=h, json={"prompt": "ok"})
    assert r.status_code == 422
    assert fake.create_calls == []


def test_generate_video_rejects_an_out_of_range_duration(client, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_kling_key(client, h)
    _override_gen_video_provider(monkeypatch, _FakeVideoProvider())
    r = client.post("/api/media/videos", headers=h,
                    json={"prompt": "a cat walking on a windowsill", "duration_sec": 60})
    assert r.status_code == 422


# ------------------------------------------------------------------ suggest-idea

class _FakeTextProvider:
    supports_grounding = False

    def __init__(self, content="A paper boat drifting down a rain-soaked gutter.",
                error=None):
        self._content, self._error = content, error
        self.calls = []

    async def generate_text(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._content, []

    async def close(self):
        pass


def _override_text_provider(monkeypatch, fake):
    from api.deps import get_text_provider
    app.dependency_overrides[get_text_provider] = lambda: fake
    return get_text_provider


def _set_text_model(client, headers, provider="openrouter", model="anthropic/claude-sonnet-5"):
    r = client.put("/api/settings/ai", headers=headers,
                   json={"text_provider": provider, "text_model": model})
    assert r.status_code == 200


def test_suggest_idea_without_a_text_model_never_calls_the_provider(client):
    h = _register(client, "a@ex.com")
    r = client.post("/api/media/videos/suggest-idea", headers=h, json={})
    assert r.status_code == 400


def test_suggest_idea_returns_a_prompt(client, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_text_model(client, h)
    fake = _FakeTextProvider()
    key = _override_text_provider(monkeypatch, fake)
    try:
        r = client.post("/api/media/videos/suggest-idea", headers=h, json={})
    finally:
        app.dependency_overrides.pop(key, None)
    assert r.status_code == 200
    assert r.json()["prompt"] == "A paper boat drifting down a rain-soaked gutter."
    assert len(fake.calls) == 1


def test_suggest_idea_passes_the_niche_through(client, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_text_model(client, h)
    fake = _FakeTextProvider()
    key = _override_text_provider(monkeypatch, fake)
    try:
        r = client.post("/api/media/videos/suggest-idea", headers=h,
                        json={"niche": "artisan coffee"})
    finally:
        app.dependency_overrides.pop(key, None)
    assert r.status_code == 200
    assert "artisan coffee" in fake.calls[0]["user_prompt"]


def test_suggest_idea_provider_failure_is_a_502(client, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_text_model(client, h)
    from services.ai.base import AIError
    fake = _FakeTextProvider(error=AIError("The provider rejected the key."))
    key = _override_text_provider(monkeypatch, fake)
    try:
        r = client.post("/api/media/videos/suggest-idea", headers=h, json={})
    finally:
        app.dependency_overrides.pop(key, None)
    assert r.status_code == 502
    assert "rejected the key" in r.json()["detail"]


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
