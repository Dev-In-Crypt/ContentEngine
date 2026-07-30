"""The server-side video poller (services/video_poll.py).

Generation takes minutes and the provider's result URL is temporary — this is
why the poll has to live on the server rather than in a closed browser tab
(see the plan). Each row gets its own try/except so one asset's crash can
never block another tenant's, or another asset of the same tenant's, from
being checked — that guard gets a dedicated test, not just an assertion.

A real (if tiny) MP4 is used for the "succeed" cases rather than fake bytes,
because the poller calls services.video.normalize.probe_video() on the
downloaded file, and that shells out to ffmpeg — the same convention
test_assemble.py/test_kenburns.py already use for exactly this reason.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models.database import Base, MediaAsset, User, UserCredentials
from services import media_store
from services.secrets import encrypt
from services.video.genai.base import GenVideoStatus, VideoGenError
from services.video.kenburns import KenBurnsVideoProvider
from services.video_poll import run_video_poll

pytestmark = pytest.mark.asyncio


async def _tiny_mp4() -> bytes:
    return await KenBurnsVideoProvider().make_reel([_jpeg()], duration_per=0.2)


def _jpeg() -> bytes:
    import io
    from PIL import Image
    img = Image.new("RGB", (1080, 1350), "blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class _FakeProvider:
    def __init__(self, status=None, error=None, download_bytes=None):
        self._status = status or GenVideoStatus(state="processing")
        self._error = error
        self._download_bytes = download_bytes
        self.poll_calls = []
        self.download_calls = []
        self.closed = False

    async def poll(self, task_id):
        self.poll_calls.append(task_id)
        if self._error:
            raise self._error
        return self._status

    async def download(self, url):
        self.download_calls.append(url)
        return self._download_bytes

    async def close(self):
        self.closed = True


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setattr(media_store, "MEDIA_ROOT", tmp_path / "media")
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'poll.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


async def _seed_user_with_key(sm, email="u@ex.com", key="sk-kling-test"):
    async with sm() as db:
        user = User(email=email, password_hash="x")
        db.add(user)
        await db.flush()
        db.add(UserCredentials(user_id=user.id, kling_api_key_enc=encrypt(key)))
        await db.commit()
        return user.id


async def _seed_video_asset(sm, user_id, **over):
    fields = dict(
        user_id=user_id, kind="video", source="ai_gen", status="pending",
        provider="kling", model="kling-v1-6", provider_task_id="text2video:abc",
    )
    fields.update(over)
    async with sm() as db:
        asset = MediaAsset(**fields)
        db.add(asset)
        await db.commit()
        return asset.id


async def _get(sm, asset_id):
    async with sm() as db:
        return await db.get(MediaAsset, asset_id)


def _install_fake(monkeypatch, fake):
    import services.video_poll as video_poll
    monkeypatch.setattr(video_poll, "get_gen_video_provider", lambda *a, **kw: fake)


# ------------------------------------------------------------------ succeed

async def test_succeed_downloads_the_file_and_marks_ready(sm, monkeypatch):
    user_id = await _seed_user_with_key(sm)
    asset_id = await _seed_video_asset(sm, user_id)
    mp4 = await _tiny_mp4()
    fake = _FakeProvider(
        status=GenVideoStatus(state="succeed", video_url="https://cdn/out.mp4"),
        download_bytes=mp4)
    _install_fake(monkeypatch, fake)

    await run_video_poll(sm)

    asset = await _get(sm, asset_id)
    assert asset.status == "ready"
    assert asset.bytes == len(mp4)
    assert asset.width and asset.height
    assert asset.duration_sec and asset.duration_sec > 0
    assert media_store.read(user_id, asset_id) == mp4
    assert fake.closed


# ------------------------------------------------------------------ failed

async def test_failed_status_records_the_error(sm, monkeypatch):
    user_id = await _seed_user_with_key(sm)
    asset_id = await _seed_video_asset(sm, user_id)
    fake = _FakeProvider(status=GenVideoStatus(state="failed", error="content policy"))
    _install_fake(monkeypatch, fake)

    await run_video_poll(sm)

    asset = await _get(sm, asset_id)
    assert asset.status == "failed"
    assert asset.error == "content policy"


async def test_a_download_failure_marks_the_asset_failed_not_ready(sm, monkeypatch):
    user_id = await _seed_user_with_key(sm)
    asset_id = await _seed_video_asset(sm, user_id)
    fake = _FakeProvider(status=GenVideoStatus(state="succeed", video_url="https://cdn/out.mp4"))
    fake.download = _raise(VideoGenError("cdn 500"))
    _install_fake(monkeypatch, fake)

    await run_video_poll(sm)

    asset = await _get(sm, asset_id)
    assert asset.status == "failed"
    assert "cdn 500" in asset.error


def _raise(exc):
    async def _f(url):
        raise exc
    return _f


# ------------------------------------------------------------------ processing / timeout

async def test_still_processing_is_left_alone(sm, monkeypatch):
    user_id = await _seed_user_with_key(sm)
    asset_id = await _seed_video_asset(sm, user_id, status="pending")
    _install_fake(monkeypatch, _FakeProvider(status=GenVideoStatus(state="processing")))

    await run_video_poll(sm)

    asset = await _get(sm, asset_id)
    assert asset.status == "running"
    assert asset.error is None


async def test_a_task_stuck_processing_past_the_timeout_is_failed(sm, monkeypatch):
    user_id = await _seed_user_with_key(sm)
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    asset_id = await _seed_video_asset(sm, user_id, created_at=old)
    _install_fake(monkeypatch, _FakeProvider(status=GenVideoStatus(state="processing")))

    await run_video_poll(sm)

    asset = await _get(sm, asset_id)
    assert asset.status == "failed"
    assert "timed out" in asset.error.lower()


async def test_a_transient_poll_error_before_the_timeout_is_not_a_failure(sm, monkeypatch):
    """A network blip talking to Kling must not fail a generation that is
    probably still fine — only exhausting the timeout should."""
    user_id = await _seed_user_with_key(sm)
    asset_id = await _seed_video_asset(sm, user_id, status="pending")
    _install_fake(monkeypatch, _FakeProvider(error=VideoGenError("network blip")))

    await run_video_poll(sm)

    asset = await _get(sm, asset_id)
    assert asset.status == "pending"
    assert asset.error is None


async def test_a_transient_poll_error_past_the_timeout_is_a_failure(sm, monkeypatch):
    user_id = await _seed_user_with_key(sm)
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    asset_id = await _seed_video_asset(sm, user_id, created_at=old)
    _install_fake(monkeypatch, _FakeProvider(error=VideoGenError("still down")))

    await run_video_poll(sm)

    asset = await _get(sm, asset_id)
    assert asset.status == "failed"


# ------------------------------------------------------------------ key removed

async def test_a_removed_key_fails_the_asset_without_calling_poll(sm, monkeypatch):
    user_id = await _seed_user_with_key(sm)
    async with sm() as db:
        creds = await db.get(UserCredentials, user_id)
        creds.kling_api_key_enc = encrypt("")
        await db.commit()
    asset_id = await _seed_video_asset(sm, user_id)
    fake = _FakeProvider()
    _install_fake(monkeypatch, fake)

    await run_video_poll(sm)

    asset = await _get(sm, asset_id)
    assert asset.status == "failed"
    assert fake.poll_calls == []


# ------------------------------------------------------------------ the WHERE clause / isolation

async def test_untouched_rows_are_left_alone(sm, monkeypatch):
    """A ready video, a pending video with no task id yet, and a pending image
    must never be touched by this sweep — it exists for exactly one shape of
    row."""
    user_id = await _seed_user_with_key(sm)
    ready_id = await _seed_video_asset(sm, user_id, status="ready")
    no_task_id = await _seed_video_asset(sm, user_id, provider_task_id=None)
    async with sm() as db:
        image = MediaAsset(user_id=user_id, kind="image", source="ai_gen", status="pending")
        db.add(image)
        await db.commit()
        image_id = image.id
    fake = _FakeProvider()
    _install_fake(monkeypatch, fake)

    await run_video_poll(sm)

    assert fake.poll_calls == []
    assert (await _get(sm, ready_id)).status == "ready"
    assert (await _get(sm, no_task_id)).status == "pending"
    assert (await _get(sm, image_id)).status == "pending"


async def test_one_crashing_asset_does_not_block_another(sm, monkeypatch):
    user_id = await _seed_user_with_key(sm)
    crashing_id = await _seed_video_asset(sm, user_id)
    fine_id = await _seed_video_asset(sm, user_id, provider_task_id="text2video:def")

    calls = []

    def _factory(name, key, ssl_verify=True):
        class _Crasher:
            async def poll(self, task_id):
                calls.append(task_id)
                if task_id == "text2video:abc":
                    raise RuntimeError("boom — not even a VideoGenError")
                return GenVideoStatus(state="failed", error="fine, just failed normally")

            async def close(self):
                pass
        return _Crasher()

    import services.video_poll as video_poll
    monkeypatch.setattr(video_poll, "get_gen_video_provider", _factory)

    await run_video_poll(sm)

    assert set(calls) == {"text2video:abc", "text2video:def"}
    assert (await _get(sm, fine_id)).status == "failed"
    # the crashing one is untouched (still pending) but did not stop the sweep
    assert (await _get(sm, crashing_id)).status == "pending"
