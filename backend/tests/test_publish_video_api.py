"""POST /api/media/{asset_id}/publish-x, POST /api/posts/{post_id}/publish-video,
and GET /api/publish-jobs — the routes that enqueue a Phase 8 video publish
job. enqueue() itself is cheap (DB writes only, no network), so these tests
run it for real; only the poller (services/x_video_publish.run_x_video_publish)
is out of scope here — it's covered end-to-end in test_x_video_publish.py.
"""
import asyncio
import io
import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, MediaAsset, Post, User, VideoPublishJob, Workspace
from services import media_store
from services.tts import ffmpeg_exe


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(media_store, "MEDIA_ROOT", tmp_path / "media")
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pub.db'}")

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
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_settings, None)
    asyncio.run(eng.dispose())


def _register(client, email) -> dict:
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123", "account_type": "creator"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _set_x_keys(client, headers, **over):
    body = {"x_api_key": "ck", "x_api_secret": "cs",
           "x_access_token": "at", "x_access_token_secret": "ats"}
    body.update(over)
    r = client.put("/api/settings/credentials", headers=headers, json=body)
    assert r.status_code == 200


def _real_clip(tmp_path, name="clip.mp4", *, seconds=1.0, audio=True) -> bytes:
    p = tmp_path / name
    args = [ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={seconds}:size=320x180:rate=30"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    args += [str(p)]
    subprocess.run(args, capture_output=True, check=True)
    return p.read_bytes()


def _upload_video(client, headers, data: bytes, name="clip.mp4") -> str:
    r = client.post("/api/media/uploads", headers=headers,
                    files={"files": (name, io.BytesIO(data), "video/mp4")})
    assert r.status_code == 200
    return r.json()[0]["id"]


async def _user_id(sm, email) -> str:
    async with sm() as db:
        return (await db.execute(select(User).where(User.email == email))).scalar_one().id


def _seed_post(sm, user_id, tmp_path, **over) -> str:
    video = tmp_path / "post_reel.mp4"
    _real_clip(tmp_path, "post_reel.mp4")
    fields = dict(id=str(uuid.uuid4()), user_id=user_id, topic="t", format="single",
                 status="preview", platform="x", video_path=str(video))
    fields.update(over)

    async def _go():
        async with sm() as db:
            db.add(Post(**fields))
            await db.commit()
    asyncio.run(_go())
    return fields["id"]


# ------------------------------------------------------------------ media.py: publish-x


def test_publish_x_404s_another_tenants_asset(client, tmp_path):
    a = _register(client, "a@ex.com")
    b = _register(client, "b@ex.com")
    _set_x_keys(client, b)
    asset_id = _upload_video(client, a, _real_clip(tmp_path))
    r = client.post(f"/api/media/{asset_id}/publish-x", headers=b,
                    json={"text": "check this out"})
    assert r.status_code == 404


def test_publish_x_rejects_a_non_video_asset(client, tmp_path):
    h = _register(client, "a@ex.com")
    _set_x_keys(client, h)
    img_id = client.post("/api/media/uploads", headers=h,
                         files={"files": ("p.jpg", io.BytesIO(b"jpeg"), "image/jpeg")}
                         ).json()[0]["id"]
    r = client.post(f"/api/media/{img_id}/publish-x", headers=h, json={"text": "hi"})
    assert r.status_code == 400


def test_publish_x_rejects_a_pending_asset(client, tmp_path):
    h = _register(client, "a@ex.com")
    _set_x_keys(client, h)
    pending_id = "55555555-5555-4555-8555-555555555555"

    async def _seed():
        async with app.state.sessionmaker() as db:
            user_id = (await db.execute(select(User).where(User.email == "a@ex.com"))
                      ).scalar_one().id
            db.add(MediaAsset(id=pending_id, user_id=user_id, kind="video",
                              source="ai_gen", status="pending"))
            await db.commit()
    asyncio.run(_seed())

    r = client.post(f"/api/media/{pending_id}/publish-x", headers=h, json={"text": "hi"})
    assert r.status_code == 400


def test_publish_x_requires_credentials(client, tmp_path):
    h = _register(client, "a@ex.com")
    asset_id = _upload_video(client, h, _real_clip(tmp_path))
    r = client.post(f"/api/media/{asset_id}/publish-x", headers=h, json={"text": "hi"})
    assert r.status_code == 400
    assert "credentials" in r.json()["detail"].lower()


def test_publish_x_rejects_an_invalid_clip(client, tmp_path):
    """A too-short clip is caught by validate_video_for_x before enqueue."""
    h = _register(client, "a@ex.com")
    _set_x_keys(client, h)
    asset_id = _upload_video(client, h, _real_clip(tmp_path, seconds=0.1))
    r = client.post(f"/api/media/{asset_id}/publish-x", headers=h, json={"text": "hi"})
    assert r.status_code == 400
    assert "short" in r.json()["detail"].lower()


def test_publish_x_succeeds_and_returns_a_queued_job(client, tmp_path):
    h = _register(client, "a@ex.com")
    _set_x_keys(client, h)
    asset_id = _upload_video(client, h, _real_clip(tmp_path))
    r = client.post(f"/api/media/{asset_id}/publish-x", headers=h,
                    json={"text": "Check this out."})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["platform"] == "x"
    assert body["asset_id"] == asset_id
    assert body["post_id"] is None
    assert body["warning"] is None       # this clip has audio


def test_publish_x_surfaces_the_silent_warning(client, tmp_path):
    h = _register(client, "a@ex.com")
    _set_x_keys(client, h)
    asset_id = _upload_video(client, h, _real_clip(tmp_path, audio=False))
    r = client.post(f"/api/media/{asset_id}/publish-x", headers=h,
                    json={"text": "Check this out."})
    assert r.status_code == 202, r.text
    assert "no audio" in r.json()["warning"].lower()


def test_publish_x_rejects_empty_body(client, tmp_path):
    h = _register(client, "a@ex.com")
    _set_x_keys(client, h)
    asset_id = _upload_video(client, h, _real_clip(tmp_path))
    r = client.post(f"/api/media/{asset_id}/publish-x", headers=h, json={"text": ""})
    assert r.status_code == 422   # PublishVideoToXRequest's own validator


# ------------------------------------------------------------------ posts.py: publish-video


def test_publish_video_404s_another_tenants_post(client, tmp_path):
    _register(client, "a@ex.com")
    b = _register(client, "b@ex.com")
    _set_x_keys(client, b)
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    post_id = _seed_post(app.state.sessionmaker, user_id, tmp_path)
    r = client.post(f"/api/posts/{post_id}/publish-video", headers=b)
    assert r.status_code == 404
    assert asyncio.run(_no_job_exists(app.state.sessionmaker, post_id))


async def _no_job_exists(sm, post_id) -> bool:
    async with sm() as db:
        rows = (await db.execute(
            select(VideoPublishJob).where(VideoPublishJob.post_id == post_id)
        )).scalars().all()
        return rows == []


def test_publish_video_rejects_an_instagram_post(client, tmp_path):
    h = _register(client, "a@ex.com")
    _set_x_keys(client, h)
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    post_id = _seed_post(app.state.sessionmaker, user_id, tmp_path, platform="instagram")
    r = client.post(f"/api/posts/{post_id}/publish-video", headers=h)
    assert r.status_code == 400
    assert "instagram" in r.json()["detail"].lower()


def test_publish_video_requires_a_rendered_reel(client, tmp_path):
    h = _register(client, "a@ex.com")
    _set_x_keys(client, h)
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    post_id = _seed_post(app.state.sessionmaker, user_id, tmp_path, video_path=None)
    r = client.post(f"/api/posts/{post_id}/publish-video", headers=h)
    assert r.status_code == 409


def test_publish_video_rejects_already_published(client, tmp_path):
    h = _register(client, "a@ex.com")
    _set_x_keys(client, h)
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    post_id = _seed_post(app.state.sessionmaker, user_id, tmp_path,
                         status="published", instagram_media_id="tw-old")
    r = client.post(f"/api/posts/{post_id}/publish-video", headers=h)
    assert r.status_code == 409


def test_publish_video_honours_the_business_approval_gate(client, tmp_path):
    h = _register(client, "a@ex.com")
    _set_x_keys(client, h)
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))

    async def _seed_ws():
        async with app.state.sessionmaker() as db:
            ws = Workspace(owner_user_id=user_id, name="Acme")
            db.add(ws)
            await db.commit()
            return ws.id
    ws_id = asyncio.run(_seed_ws())
    post_id = _seed_post(app.state.sessionmaker, user_id, tmp_path,
                         workspace_id=ws_id, status="preview")

    r = client.post(f"/api/posts/{post_id}/publish-video", headers=h)
    assert r.status_code == 409
    assert "approved" in r.json()["detail"].lower()


def test_publish_video_requires_credentials(client, tmp_path):
    h = _register(client, "a@ex.com")
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    post_id = _seed_post(app.state.sessionmaker, user_id, tmp_path)
    r = client.post(f"/api/posts/{post_id}/publish-video", headers=h)
    assert r.status_code == 400
    assert "credentials" in r.json()["detail"].lower()


def test_publish_video_cancels_the_pending_scheduled_job(client, tmp_path, monkeypatch):
    """Mutation guard: drop cancel_publish and a scheduled photo publish would
    fire later and double-post now that a video publish is in flight."""
    import services.scheduler as sched
    cancelled = {}
    monkeypatch.setattr(sched, "cancel_publish", lambda pid: cancelled.setdefault("pid", pid))

    h = _register(client, "a@ex.com")
    _set_x_keys(client, h)
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    post_id = _seed_post(app.state.sessionmaker, user_id, tmp_path)

    r = client.post(f"/api/posts/{post_id}/publish-video", headers=h)
    assert r.status_code == 202, r.text
    assert cancelled.get("pid") == post_id


def test_publish_video_returns_202_with_a_queued_job(client, tmp_path):
    h = _register(client, "a@ex.com")
    _set_x_keys(client, h)
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    post_id = _seed_post(app.state.sessionmaker, user_id, tmp_path,
                         caption="Fresh loaves.", hashtags=["#baking"])

    r = client.post(f"/api/posts/{post_id}/publish-video", headers=h)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["post_id"] == post_id
    assert body["asset_id"] is None


# ------------------------------------------------------------------ publish_jobs.py


def _seed_job(sm, user_id, **over) -> str:
    fields = dict(id=str(uuid.uuid4()), user_id=user_id, platform="x", status="uploading",
                 video_path="/tmp/x.mp4", total_bytes=100)
    fields.update(over)

    async def _go():
        async with sm() as db:
            db.add(VideoPublishJob(**fields))
            await db.commit()
    asyncio.run(_go())
    return fields["id"]


def test_publish_jobs_list_is_scoped_to_the_caller(client):
    a = _register(client, "a@ex.com")
    _register(client, "b@ex.com")
    user_a = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    user_b = asyncio.run(_user_id(app.state.sessionmaker, "b@ex.com"))
    _seed_job(app.state.sessionmaker, user_a)
    _seed_job(app.state.sessionmaker, user_b)

    jobs = client.get("/api/publish-jobs", headers=a).json()
    assert len(jobs) == 1


def test_publish_jobs_get_404s_for_another_tenants_job(client):
    a = _register(client, "a@ex.com")
    b = _register(client, "b@ex.com")
    user_b = asyncio.run(_user_id(app.state.sessionmaker, "b@ex.com"))
    job_id = _seed_job(app.state.sessionmaker, user_b)

    assert client.get(f"/api/publish-jobs/{job_id}", headers=a).status_code == 404
    assert client.get(f"/api/publish-jobs/{job_id}", headers=b).status_code == 200


def test_publish_jobs_active_filter_excludes_terminal_jobs(client):
    h = _register(client, "a@ex.com")
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    _seed_job(app.state.sessionmaker, user_id, status="published")
    active_id = _seed_job(app.state.sessionmaker, user_id, status="uploading")

    jobs = client.get("/api/publish-jobs?active=1", headers=h).json()
    assert [j["id"] for j in jobs] == [active_id]


def test_publish_jobs_filter_by_asset_id(client):
    h = _register(client, "a@ex.com")
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    _seed_job(app.state.sessionmaker, user_id, asset_id="other")
    mine_id = _seed_job(app.state.sessionmaker, user_id, asset_id="mine")

    jobs = client.get("/api/publish-jobs?asset_id=mine", headers=h).json()
    assert [j["id"] for j in jobs] == [mine_id]


# ------------------------------------------------------------------ posts.py: the preview's video_url
#
# The composer's video preview — and the publish button inside it — used to
# exist only in the session that ran the render: renderPreview hid it on every
# load because the payload gave it no way to know a video was already there.
# GET /api/posts/{id} now says so.


def test_the_preview_exposes_a_rendered_video(client, tmp_path):
    h = _register(client, "a@ex.com")
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    post_id = _seed_post(app.state.sessionmaker, user_id, tmp_path)

    body = client.get(f"/api/posts/{post_id}", headers=h).json()
    assert body["video_url"].startswith(f"/api/posts/{post_id}/reel/video?t=")
    # …and it resolves, rather than merely looking plausible.
    assert client.get(body["video_url"], headers=h).status_code == 200


def test_the_preview_has_no_video_url_before_a_render(client, tmp_path):
    h = _register(client, "a@ex.com")
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    post_id = _seed_post(app.state.sessionmaker, user_id, tmp_path, video_path=None)

    assert client.get(f"/api/posts/{post_id}", headers=h).json()["video_url"] is None


def test_a_video_path_whose_file_is_gone_is_not_offered(client, tmp_path):
    """The column outlives the file — an uploads volume that wasn't persisted, a
    restored database. Reporting the URL anyway would put a permanent 404 in the
    composer with a publish button under it."""
    h = _register(client, "a@ex.com")
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    post_id = _seed_post(app.state.sessionmaker, user_id, tmp_path,
                         video_path=str(tmp_path / "never_written.mp4"))

    assert client.get(f"/api/posts/{post_id}", headers=h).json()["video_url"] is None


def test_the_video_url_changes_when_the_video_is_re_rendered(client, tmp_path):
    """A fixed URL would let the browser serve the previous render from cache,
    which is the whole reason the render endpoints cache-bust their own reply."""
    import os
    import time

    h = _register(client, "a@ex.com")
    user_id = asyncio.run(_user_id(app.state.sessionmaker, "a@ex.com"))
    post_id = _seed_post(app.state.sessionmaker, user_id, tmp_path)

    before = client.get(f"/api/posts/{post_id}", headers=h).json()["video_url"]
    # mtime, not wall clock: the buster has to move because the FILE changed.
    video = tmp_path / "post_reel.mp4"
    os.utime(video, (time.time() + 60, time.time() + 60))
    after = client.get(f"/api/posts/{post_id}", headers=h).json()["video_url"]

    assert before != after
