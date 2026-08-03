"""validate_video_for_x — the pre-flight checks that run before any billed X
API call. Real ffmpeg on tiny clips, same convention as test_normalize.py /
test_clip_edit.py: never mock ffmpeg itself.

The enqueue + poller tests below follow test_video_poll.py's conventions
exactly: real SQLite, a fake publisher installed by monkeypatching the
module-level make_publisher_for, one try/except per row so one job's crash
never blocks another's.
"""
import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models.database import Base, Post, User, UserCredentials, VideoPublishJob
from services.publishing.base import PublishOutcome, PublisherError
from services.secrets import encrypt
from services.tts import ffmpeg_exe
from services.x_video_publish import (
    MAX_VIDEO_BYTES, MAX_VIDEO_SEC, XVideoRejected, enqueue, run_x_video_publish,
    validate_video_for_x,
)


def _clip(path: Path, seconds: float, *, audio: bool = True, vcodec: str = "libx264") -> Path:
    args = [ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={seconds}:size=320x180:rate=30"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-pix_fmt", "yuv420p", "-c:v", vcodec, "-preset", "ultrafast"] \
        if vcodec == "libx264" else ["-pix_fmt", "yuv420p", "-c:v", vcodec]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    args += [str(path)]
    subprocess.run(args, capture_output=True, check=True)
    return path


@pytest.mark.asyncio
async def test_accepts_a_normal_clip_with_no_warning(tmp_path):
    clip = _clip(tmp_path / "ok.mp4", 1.0)
    assert await validate_video_for_x(clip) is None


@pytest.mark.asyncio
async def test_warns_but_does_not_reject_a_silent_clip(tmp_path):
    """Mutation guard: turning this warning into a raise would reject the
    common case the Phase 6 editor produces (neither voiceover nor music)."""
    clip = _clip(tmp_path / "silent.mp4", 1.0, audio=False)
    warning = await validate_video_for_x(clip)
    assert warning is not None
    assert "no audio" in warning.lower()


@pytest.mark.asyncio
async def test_rejects_a_missing_file(tmp_path):
    with pytest.raises(XVideoRejected) as e:
        await validate_video_for_x(tmp_path / "does-not-exist.mp4")
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_rejects_an_empty_file(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(XVideoRejected):
        await validate_video_for_x(empty)


@pytest.mark.asyncio
async def test_rejects_a_clip_over_the_size_cap(tmp_path, monkeypatch):
    """Mutation guard: drop or invert the size comparison and an oversized
    file would sail through to a billed (and doomed) upload."""
    import services.x_video_publish as mod
    monkeypatch.setattr(mod, "MAX_VIDEO_BYTES", 100)   # far below any real clip
    clip = _clip(tmp_path / "big.mp4", 1.0)
    with pytest.raises(XVideoRejected, match="MB"):
        await validate_video_for_x(clip)
    assert MAX_VIDEO_BYTES == 512 * 1024 * 1024        # the real constant is untouched


@pytest.mark.asyncio
async def test_rejects_a_clip_over_the_duration_cap(tmp_path, monkeypatch):
    import services.x_video_publish as mod
    monkeypatch.setattr(mod, "MAX_VIDEO_SEC", 0.3)
    clip = _clip(tmp_path / "long.mp4", 1.0)
    with pytest.raises(XVideoRejected, match="seconds"):
        await validate_video_for_x(clip)
    assert MAX_VIDEO_SEC == 140.0


@pytest.mark.asyncio
async def test_rejects_a_clip_that_is_too_short(tmp_path):
    clip = _clip(tmp_path / "tiny.mp4", 0.1)
    with pytest.raises(XVideoRejected, match="too short"):
        await validate_video_for_x(clip)


@pytest.mark.asyncio
async def test_rejects_a_non_h264_codec(tmp_path):
    clip = _clip(tmp_path / "mpeg4.mp4", 1.0, vcodec="mpeg4")
    with pytest.raises(XVideoRejected, match="H.264"):
        await validate_video_for_x(clip)


# ═════════════════════════════════════════════════════════════════════════
# enqueue + the poller
# ═════════════════════════════════════════════════════════════════════════

class _FakeXPublisher:
    """Every method has a sane default; tests override just the one method
    they need to fail, matching test_video_poll.py's `_raise()` pattern."""

    def __init__(self):
        self.append_calls: list[int] = []
        self.closed = False

    async def video_upload_init(self, total_bytes, **kw):
        return "vid1"

    async def video_upload_append(self, media_id, segment_index, chunk):
        self.append_calls.append(segment_index)

    async def video_upload_finalize(self, media_id):
        return {"state": "succeeded", "check_after_secs": None, "error": None}

    async def video_upload_status(self, media_id):
        return {"state": "succeeded", "check_after_secs": None, "error": None}

    async def publish_video(self, media_id, caption, *, alt_text=None, long_form=False):
        return PublishOutcome(media_id="tw1", permalink="https://x.com/i/web/status/tw1")

    async def publish_video_thread(self, media_id, parts, *, alt_text=None):
        return PublishOutcome(media_id="tw1", permalink="https://x.com/i/web/status/tw1")

    async def close(self):
        self.closed = True


def _install_fake(monkeypatch, fake):
    import services.x_video_publish as mod
    monkeypatch.setattr(mod, "make_publisher_for", lambda *a, **kw: fake)


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'xvp.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


async def _seed_user_with_x_key(sm, email="u@ex.com") -> str:
    async with sm() as db:
        user = User(email=email, password_hash="x")
        db.add(user)
        await db.flush()
        db.add(UserCredentials(
            user_id=user.id, x_api_key_enc=encrypt("ck"), x_api_secret_enc=encrypt("cs"),
            x_access_token_enc=encrypt("at"), x_access_token_secret_enc=encrypt("ats")))
        await db.commit()
        return user.id


def _real_video(tmp_path) -> Path:
    p = tmp_path / "real.mp4"
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi",
                    "-i", "testsrc=duration=0.3:size=320x180:rate=30",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    str(p)], capture_output=True, check=True)
    return p


async def _seed_job(sm, user_id, tmp_path, **over) -> str:
    video = _real_video(tmp_path)
    fields = dict(user_id=user_id, platform="x", status="queued",
                 video_path=str(video), total_bytes=video.stat().st_size,
                 caption="Check this out.")
    fields.update(over)
    async with sm() as db:
        job = VideoPublishJob(**fields)
        db.add(job)
        await db.commit()
        return job.id


async def _get(sm, job_id):
    async with sm() as db:
        return await db.get(VideoPublishJob, job_id)


# ------------------------------------------------------------------ enqueue

async def test_enqueue_creates_a_queued_job(sm, tmp_path):
    user_id = await _seed_user_with_x_key(sm)
    async with sm() as db:
        job = await enqueue(db, user_id=user_id, video_path="/tmp/a.mp4",
                            total_bytes=100, asset_id="a1", caption="hi")
        await db.commit()
        job_id = job.id
    job = await _get(sm, job_id)
    assert job.status == "queued"
    assert job.asset_id == "a1"
    assert job.caption == "hi"


async def test_enqueue_returns_the_existing_job_for_a_second_click(sm):
    """Mutation guard: drop the status.in_(_ACTIVE) pre-select and this
    creates two rows — two uploads, two tweets, from one click each."""
    user_id = await _seed_user_with_x_key(sm)
    async with sm() as db:
        first = await enqueue(db, user_id=user_id, video_path="/tmp/a.mp4",
                              total_bytes=100, asset_id="a1")
        await db.commit()
        first_id = first.id
    async with sm() as db:
        second = await enqueue(db, user_id=user_id, video_path="/tmp/a.mp4",
                               total_bytes=100, asset_id="a1")
        await db.commit()
        assert second.id == first_id


async def test_enqueue_after_a_finished_job_creates_a_new_row(sm):
    """Republishing the same clip is allowed — each attempt is its own
    audit-trail row, not blocked by a prior terminal job."""
    user_id = await _seed_user_with_x_key(sm)
    async with sm() as db:
        first = await enqueue(db, user_id=user_id, video_path="/tmp/a.mp4",
                              total_bytes=100, asset_id="a1")
        first.status = "published"
        await db.commit()
    async with sm() as db:
        second = await enqueue(db, user_id=user_id, video_path="/tmp/a.mp4",
                               total_bytes=100, asset_id="a1")
        await db.commit()
        assert second.id != first.id


# ------------------------------------------------------------------ queued -> uploading

async def test_a_queued_job_inits_and_moves_to_uploading(sm, tmp_path, monkeypatch):
    user_id = await _seed_user_with_x_key(sm)
    job_id = await _seed_job(sm, user_id, tmp_path, status="queued")
    _install_fake(monkeypatch, _FakeXPublisher())

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "uploading"
    assert job.media_id == "vid1"
    assert job.chunk_index == 0


# ------------------------------------------------------------------ uploading

async def test_a_tick_sends_at_most_two_chunks(sm, tmp_path, monkeypatch):
    """Mutation guard: drop the _CHUNKS_PER_TICK bound and a large upload
    monopolizes one tick for minutes, starving every other tenant's job."""
    user_id = await _seed_user_with_x_key(sm)
    # a bigger clip so more than 2 chunks are needed at a tiny chunk size
    video = tmp_path / "bigger.mp4"
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi",
                    "-i", "testsrc=duration=1:size=640x360:rate=30",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    str(video)], capture_output=True, check=True)
    total = video.stat().st_size
    job_id = await _seed_job(sm, user_id, tmp_path, status="uploading",
                             video_path=str(video), total_bytes=total, media_id="vid1")
    import services.x_video_publish as mod
    monkeypatch.setattr(mod, "VIDEO_CHUNK_BYTES", max(1, total // 10))  # force >2 chunks
    fake = _FakeXPublisher()
    _install_fake(monkeypatch, fake)

    await run_x_video_publish(sm)

    assert len(fake.append_calls) <= 2
    job = await _get(sm, job_id)
    assert job.status == "uploading"          # not finalized yet — more chunks remain


async def test_uploading_resumes_from_chunk_index_after_a_restart(sm, tmp_path, monkeypatch):
    """Mutation guard: reset chunk_index to 0 at the top of the stage and this
    fails — in production every restart would re-upload from scratch."""
    user_id = await _seed_user_with_x_key(sm)
    video = tmp_path / "bigger.mp4"
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi",
                    "-i", "testsrc=duration=1:size=640x360:rate=30",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    str(video)], capture_output=True, check=True)
    total = video.stat().st_size
    await _seed_job(sm, user_id, tmp_path, status="uploading",
                    video_path=str(video), total_bytes=total,
                    media_id="vid1", chunk_index=2)
    import services.x_video_publish as mod
    monkeypatch.setattr(mod, "VIDEO_CHUNK_BYTES", max(1, total // 10))
    fake = _FakeXPublisher()
    _install_fake(monkeypatch, fake)

    await run_x_video_publish(sm)

    assert fake.append_calls[0] == 2


async def test_a_changed_file_fails_the_job_instead_of_splicing_chunks(sm, tmp_path, monkeypatch):
    """Mutation guard: remove the size check and chunks from two different
    files would be spliced under one media_id, publishing a corrupt video."""
    user_id = await _seed_user_with_x_key(sm)
    job_id = await _seed_job(sm, user_id, tmp_path, status="uploading",
                             media_id="vid1", total_bytes=999999)  # never matches the real file
    _install_fake(monkeypatch, _FakeXPublisher())

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "failed"
    assert "changed" in job.error.lower()


async def test_finishing_the_last_chunk_finalizes_and_moves_to_processing(sm, tmp_path, monkeypatch):
    user_id = await _seed_user_with_x_key(sm)
    job_id = await _seed_job(sm, user_id, tmp_path, status="uploading", media_id="vid1")
    fake = _FakeXPublisher()

    async def _pending_finalize(media_id):
        return {"state": "pending", "check_after_secs": 5, "error": None}
    fake.video_upload_finalize = _pending_finalize
    _install_fake(monkeypatch, fake)

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "processing"
    assert job.next_attempt_at is not None


# ------------------------------------------------------------------ processing

async def test_processing_succeeded_moves_to_tweeting_and_publishes(sm, tmp_path, monkeypatch):
    """A quick clip can go processing -> tweeting -> published inside one
    sweep only if run_x_video_publish re-selects; a single tick only advances
    one stage, so this seeds directly at 'processing' to check that hop."""
    user_id = await _seed_user_with_x_key(sm)
    job_id = await _seed_job(sm, user_id, tmp_path, status="processing", media_id="vid1")
    _install_fake(monkeypatch, _FakeXPublisher())

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "tweeting"


async def test_processing_failed_records_x_s_reason_and_does_not_retry(sm, tmp_path, monkeypatch):
    user_id = await _seed_user_with_x_key(sm)
    job_id = await _seed_job(sm, user_id, tmp_path, status="processing", media_id="vid1")
    fake = _FakeXPublisher()

    async def _failed_status(media_id):
        return {"state": "failed", "check_after_secs": None,
                "error": "InvalidMedia: Unsupported codec"}
    fake.video_upload_status = _failed_status
    _install_fake(monkeypatch, fake)

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "failed"
    assert job.attempts == 0                  # never retried
    assert "Unsupported codec" in job.error


# ------------------------------------------------------------------ tweeting

async def test_a_published_job_writes_the_tweet_id_and_permalink_onto_the_post(sm, tmp_path, monkeypatch):
    """Mutation guard: skip the Post projection and the feed/analytics never
    show the post as published even though the tweet is live."""
    user_id = await _seed_user_with_x_key(sm)
    async with sm() as db:
        post = Post(user_id=user_id, topic="t", format="single", status="scheduled",
                    platform="x")
        db.add(post)
        await db.commit()
        post_id = post.id
    job_id = await _seed_job(sm, user_id, tmp_path, status="tweeting", media_id="vid1",
                             post_id=post_id)
    _install_fake(monkeypatch, _FakeXPublisher())

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "published"
    assert job.tweet_id == "tw1"
    assert job.permalink.endswith("tw1")
    async with sm() as db:
        post = await db.get(Post, post_id)
        assert post.status == "published"
        assert post.instagram_media_id == "tw1"
        assert post.published_url.endswith("tw1")
        assert post.published_at is not None


async def test_a_standalone_asset_job_never_touches_a_post(sm, tmp_path, monkeypatch):
    user_id = await _seed_user_with_x_key(sm)
    job_id = await _seed_job(sm, user_id, tmp_path, status="tweeting", media_id="vid1",
                             asset_id="a1", post_id=None)
    _install_fake(monkeypatch, _FakeXPublisher())

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "published"       # no exception from a missing Post


async def test_a_thread_job_uses_thread_parts(sm, tmp_path, monkeypatch):
    user_id = await _seed_user_with_x_key(sm)
    await _seed_job(sm, user_id, tmp_path, status="tweeting", media_id="vid1",
                    thread_parts=["Hook.", "Follow-up."])
    fake = _FakeXPublisher()
    calls = []

    async def _thread(media_id, parts, *, alt_text=None):
        calls.append(parts)
        return PublishOutcome(media_id="tw1", permalink="https://x.com/i/web/status/tw1")
    fake.publish_video_thread = _thread
    _install_fake(monkeypatch, fake)

    await run_x_video_publish(sm)

    assert calls == [["Hook.", "Follow-up."]]


async def test_a_tweeting_failure_is_never_retried(sm, tmp_path, monkeypatch):
    """The single most important guard in this phase: a POST /2/tweets that
    times out may already be live, and X cannot undo one — retrying risks a
    duplicate. Mutation guard: let 'tweeting' go through is_retryable/backoff
    like every other stage → this fails, and production could double-post."""
    user_id = await _seed_user_with_x_key(sm)
    job_id = await _seed_job(sm, user_id, tmp_path, status="tweeting", media_id="vid1")
    fake = _FakeXPublisher()

    async def _boom(media_id, caption, *, alt_text=None, long_form=False):
        raise PublisherError("X tweet failed: 503 Service Unavailable")  # retryable-shaped
    fake.publish_video = _boom
    _install_fake(monkeypatch, fake)

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "failed"
    assert job.attempts == 0
    assert job.next_attempt_at is None


# ------------------------------------------------------------------ retry / timeout

async def test_a_retryable_upload_error_keeps_the_stage_and_schedules_a_backoff(sm, tmp_path, monkeypatch):
    """Mutation guard: reset the stage (or chunk_index) on a retryable error
    and the next tick re-INITs, paying for the same upload twice."""
    user_id = await _seed_user_with_x_key(sm)
    video = _real_video(tmp_path)
    total = video.stat().st_size
    job_id = await _seed_job(sm, user_id, tmp_path, status="uploading", media_id="vid1",
                             video_path=str(video), total_bytes=total, chunk_index=1)
    import services.x_video_publish as mod
    monkeypatch.setattr(mod, "VIDEO_CHUNK_BYTES", max(1, total // 5))  # needs several chunks
    fake = _FakeXPublisher()

    async def _flaky(media_id, segment_index, chunk):
        raise PublisherError("X media APPEND failed: 503 Service Unavailable")
    fake.video_upload_append = _flaky
    _install_fake(monkeypatch, fake)

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "uploading"           # stage unchanged
    assert job.chunk_index == 1                # not reset
    assert job.attempts == 1
    assert job.next_attempt_at is not None


async def test_a_permanent_upload_error_fails_without_retry(sm, tmp_path, monkeypatch):
    user_id = await _seed_user_with_x_key(sm)
    job_id = await _seed_job(sm, user_id, tmp_path, status="uploading", media_id="vid1")
    fake = _FakeXPublisher()

    async def _rejected(media_id, segment_index, chunk):
        raise PublisherError("X media APPEND failed: 400 Bad segment")
    fake.video_upload_append = _rejected
    _install_fake(monkeypatch, fake)

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "failed"
    assert job.attempts == 0


async def test_a_stale_job_past_the_timeout_is_failed(sm, tmp_path, monkeypatch):
    user_id = await _seed_user_with_x_key(sm)
    old = datetime.now(timezone.utc) - timedelta(minutes=45)
    job_id = await _seed_job(sm, user_id, tmp_path, status="processing", media_id="vid1",
                             created_at=old)
    _install_fake(monkeypatch, _FakeXPublisher())

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "failed"
    assert "timed out" in job.error.lower()


async def test_tweeting_is_exempt_from_the_stale_timeout(sm, tmp_path, monkeypatch):
    """A tick either posts or fails 'tweeting' outright — it can never be the
    stage that ages out, so the timeout check must not fire for it."""
    user_id = await _seed_user_with_x_key(sm)
    old = datetime.now(timezone.utc) - timedelta(minutes=45)
    job_id = await _seed_job(sm, user_id, tmp_path, status="tweeting", media_id="vid1",
                             created_at=old)
    _install_fake(monkeypatch, _FakeXPublisher())

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "published"           # not failed by the timeout check


# ------------------------------------------------------------------ credentials removed

async def test_a_job_whose_owner_removed_their_x_keys_fails_cleanly(sm, tmp_path):
    user_id = await _seed_user_with_x_key(sm)
    async with sm() as db:
        creds = await db.get(UserCredentials, user_id)
        creds.x_api_key_enc = encrypt("")
        await db.commit()
    job_id = await _seed_job(sm, user_id, tmp_path, status="queued")

    await run_x_video_publish(sm)

    job = await _get(sm, job_id)
    assert job.status == "failed"
    assert "credentials" in job.error.lower()


# ------------------------------------------------------------------ isolation

async def test_untouched_jobs_are_left_alone(sm, tmp_path, monkeypatch):
    user_id = await _seed_user_with_x_key(sm)
    published_id = await _seed_job(sm, user_id, tmp_path, status="published")
    failed_id = await _seed_job(sm, user_id, tmp_path, status="failed")
    future_id = await _seed_job(sm, user_id, tmp_path, status="uploading",
                                next_attempt_at=datetime.now(timezone.utc) + timedelta(hours=1))
    _install_fake(monkeypatch, _FakeXPublisher())

    await run_x_video_publish(sm)

    assert (await _get(sm, published_id)).status == "published"
    assert (await _get(sm, failed_id)).status == "failed"
    assert (await _get(sm, future_id)).status == "uploading"   # not eligible yet


async def test_one_crashing_job_does_not_block_another(sm, tmp_path, monkeypatch):
    user_id = await _seed_user_with_x_key(sm)
    crashing_id = await _seed_job(sm, user_id, tmp_path, status="queued")
    fine_id = await _seed_job(sm, user_id, tmp_path, status="tweeting", media_id="vid1")

    calls = []

    def _factory(platform, settings, name_prefix="slide"):
        class _Crasher:
            async def video_upload_init(self, total_bytes, **kw):
                calls.append("init")
                raise RuntimeError("boom — not even a PublisherError")

            async def publish_video(self, media_id, caption, *, alt_text=None, long_form=False):
                calls.append("tweet")
                return PublishOutcome(media_id="tw1", permalink="https://x.com/i/web/status/tw1")

            async def close(self):
                pass
        return _Crasher()

    import services.x_video_publish as mod
    monkeypatch.setattr(mod, "make_publisher_for", _factory)

    await run_x_video_publish(sm)

    assert set(calls) == {"init", "tweet"}
    assert (await _get(sm, fine_id)).status == "published"
    # the crashing one is untouched (still queued) but did not stop the sweep
    assert (await _get(sm, crashing_id)).status == "queued"
