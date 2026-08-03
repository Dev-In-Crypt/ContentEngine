"""Publishing a video to X (Phase 8).

Pre-validation (checked before any billed API call) plus enqueue + the
resumable poller that drives a job through X's chunked upload: queued ->
uploading -> processing -> tweeting -> published, or -> failed from any stage.
The wire format (query-string OAuth1 signing against the v1.1 media endpoint)
was confirmed live against a real account — see the plan and
backend/scripts/x_video_spike.py, kept only as a recon artifact.

The row IS the job (models.database.VideoPublishJob) — same reasoning as
MediaAsset: a publish that takes minutes and must survive a container restart
needs to be a row, not state held in a process that can die mid-upload.
chunk_index is what makes a restart cheap: the next tick resumes from the
last-sent chunk instead of re-uploading the whole file.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import or_, select

from services.publish_retry import MAX_RETRIES, is_retryable, next_delay_minutes
from services.publishing.base import PublisherError
from services.publishing.factory import make_publisher_for
from services.publishing.x import VIDEO_CHUNK_BYTES
from services.user_settings import build_settings_for_user
from services.video.normalize import probe_av

log = logging.getLogger(__name__)

MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_VIDEO_SEC = 140.0
MIN_VIDEO_SEC = 0.5
_ACCEPTED_VIDEO_CODEC = "h264"


class XVideoRejected(Exception):
    """The file itself means X would refuse it — checked before the first
    billed API call, so it's mapped straight to a 4xx and never reaches
    publish_retry's classifier; the "verb: NNN body" message shape used by
    the real X error paths doesn't apply here."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


async def validate_video_for_x(path: Path) -> Optional[str]:
    """Raise XVideoRejected if X would refuse the file outright; otherwise
    return an advisory warning, or None.

    A silent clip (no audio stream) is a WARNING, not a rejection: it's the
    common case out of the Phase 6 editor when neither voiceover nor music
    was chosen, X's tweet_video category accepts a video-only file, and
    rejecting it would block the main workflow rather than an edge case.
    """
    if not path.exists() or path.stat().st_size == 0:
        raise XVideoRejected(
            "The video file is missing on disk. Render it again.", status_code=404)

    size = path.stat().st_size
    if size > MAX_VIDEO_BYTES:
        raise XVideoRejected(
            f"That video is {size / 1024 / 1024:.0f} MB — X accepts up to "
            f"{MAX_VIDEO_BYTES // 1024 // 1024} MB.")

    _w, _h, duration, vcodec, acodec = await asyncio.to_thread(probe_av, path)

    if duration > MAX_VIDEO_SEC:
        raise XVideoRejected(
            f"That clip is {duration:.0f}s — X accepts up to {MAX_VIDEO_SEC:.0f} seconds.")
    if duration < MIN_VIDEO_SEC:
        raise XVideoRejected(
            "That clip is too short for X (it needs to be at least half a second).")
    if vcodec != _ACCEPTED_VIDEO_CODEC:
        raise XVideoRejected(
            f"X needs H.264 video; this file is {vcodec}. Re-render it in the clip editor.")

    if acodec is None:
        return "This clip has no audio — X will publish it silently."
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Enqueue — video_path/total_bytes are resolved once, here, so the poller below
# never has to know whether a job came from a post's Reel or a standalone
# library clip.
# ─────────────────────────────────────────────────────────────────────────────

_ACTIVE = ("queued", "uploading", "processing", "tweeting")


async def enqueue(db, *, user_id: str, video_path: str, total_bytes: int,
                  post_id: Optional[str] = None, asset_id: Optional[str] = None,
                  caption: str = "", thread_parts: Optional[list[str]] = None,
                  alt_text: Optional[str] = None, long_form: bool = False,
                  platform: str = "x"):
    """Create the publish job for this target, or return the one already in
    flight. Exactly one of post_id/asset_id should be given — that's the
    target a second click while a job is running returns instead of starting
    a duplicate upload. A finished job (published or failed) does not block a
    fresh one: republishing the same clip is allowed and gets its own row, the
    audit trail of every attempt."""
    from models.database import VideoPublishJob

    clause = (VideoPublishJob.post_id == post_id if post_id
             else VideoPublishJob.asset_id == asset_id)
    existing = (await db.execute(
        select(VideoPublishJob)
        .where(VideoPublishJob.status.in_(_ACTIVE), clause)
        .with_for_update()
    )).scalars().first()
    if existing is not None:
        return existing

    job = VideoPublishJob(
        user_id=user_id, platform=platform, post_id=post_id, asset_id=asset_id,
        video_path=video_path, total_bytes=total_bytes, status="queued",
        caption=caption, thread_parts=thread_parts, alt_text=alt_text,
        long_form=long_form,
    )
    db.add(job)
    await db.flush()
    return job


# ─────────────────────────────────────────────────────────────────────────────
# The poller — one tick advances a job at most one stage (uploading sends up
# to _CHUNKS_PER_TICK chunks before returning, so a large file doesn't
# monopolize a tick that every other tenant's job also needs).
# ─────────────────────────────────────────────────────────────────────────────

_TIMEOUT = timedelta(minutes=30)          # nothing legal takes this long
_CHUNKS_PER_TICK = 2


def _is_overdue(job) -> bool:
    created = job.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > _TIMEOUT


def _fail(job, error: str) -> None:
    job.status = "failed"
    job.error = error[:1000]


async def run_x_video_publish(sessionmaker) -> dict:
    """Advance every in-flight X video publish by one stage.

    Returns {"published": n, "failed": n} for the caller to log — never
    raised on error, since a crash here must not take the scheduler down with
    it (the wrapper in services/scheduler.py holds that guarantee at the
    outer level; this holds it per-row, the finer-grained half of the same
    promise, same as services/video_poll.py).
    """
    from models.database import VideoPublishJob

    counts = {"published": 0, "failed": 0}
    async with sessionmaker() as db:
        now = datetime.now(timezone.utc)
        rows = (await db.execute(
            select(VideoPublishJob)
            .where(VideoPublishJob.status.in_(_ACTIVE),
                   or_(VideoPublishJob.next_attempt_at.is_(None),
                       VideoPublishJob.next_attempt_at <= now))
            .with_for_update()
        )).scalars().all()

        for job in rows:
            try:
                await _advance_one(db, job)
            except Exception as e:
                log.error("X video publish crashed for job=%s: %s", job.id, e)
                continue
            if job.status == "published":
                counts["published"] += 1
            elif job.status == "failed":
                counts["failed"] += 1

        await db.commit()
    return counts


async def _advance_one(db, job) -> None:
    from models.database import Post as PostModel, User as UserModel

    # tweeting is exempt: it either posts or fails on its single attempt
    # within one tick (see below), so it can never be the thing that ages out.
    if job.status != "tweeting" and _is_overdue(job):
        _fail(job, f"Timed out publishing to X after {_TIMEOUT}. Nothing was posted.")
        return

    user = await db.get(UserModel, job.user_id)
    settings = await build_settings_for_user(db, user)
    try:
        publisher = make_publisher_for(job.platform, settings)
    except Exception as e:
        # The owner removed their X keys mid-flight — same posture as
        # video_poll's removed-Kling-key case: fail cleanly, no retry.
        _fail(job, str(e))
        return

    try:
        if job.status == "queued":
            await _do_init(job, publisher)
        elif job.status == "uploading":
            await _do_upload_chunks(job, publisher)
        elif job.status == "processing":
            await _do_check_status(job, publisher)
        elif job.status == "tweeting":
            await _do_tweet(db, job, publisher, PostModel)
    except PublisherError as e:
        if job.status == "tweeting":
            # A POST /2/tweets that timed out may already be live — retrying
            # would risk a second tweet, and X has no way to undo one (the
            # same reason publish_thread's partial-failure message tells the
            # user to finish or delete it by hand rather than auto-retrying).
            _fail(job, f"{e} Check your X timeline before publishing again.")
        else:
            delay = next_delay_minutes(job.attempts) if is_retryable(e) else None
            if delay is not None:
                job.attempts += 1
                job.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=delay)
                job.error = (f"{e} — retrying in {delay} min "
                            f"(attempt {job.attempts} of {MAX_RETRIES})")
                # status and chunk_index UNCHANGED: the retry resumes mid-upload.
            else:
                _fail(job, str(e))
    finally:
        await publisher.close()


async def _do_init(job, publisher) -> None:
    job.media_id = await publisher.video_upload_init(job.total_bytes)
    job.status = "uploading"
    job.chunk_index = 0


async def _do_upload_chunks(job, publisher) -> None:
    path = Path(job.video_path)
    if not path.exists() or path.stat().st_size != job.total_bytes:
        # The source file was re-rendered or replaced under an in-flight
        # upload — splicing chunks from two different files under one
        # media_id would publish a corrupt video, so this is not retryable.
        _fail(job, "The video changed while it was uploading — publish it again.")
        return

    with open(path, "rb") as f:
        sent = 0
        while sent < _CHUNKS_PER_TICK:
            f.seek(job.chunk_index * VIDEO_CHUNK_BYTES)
            chunk = f.read(VIDEO_CHUNK_BYTES)
            if not chunk:
                break
            await publisher.video_upload_append(job.media_id, job.chunk_index, chunk)
            job.chunk_index += 1
            sent += 1
        f.seek(job.chunk_index * VIDEO_CHUNK_BYTES)
        all_sent = not f.read(1)

    if all_sent:
        status = await publisher.video_upload_finalize(job.media_id)
        _apply_processing(job, status)


async def _do_check_status(job, publisher) -> None:
    status = await publisher.video_upload_status(job.media_id)
    _apply_processing(job, status)


def _apply_processing(job, status: dict) -> None:
    if status["state"] == "succeeded":
        job.status = "tweeting"
    elif status["state"] == "failed":
        # X's own processing rejection has no HTTP status to classify —
        # falls straight to permanent, which is correct: a rejected codec
        # fails identically on every retry.
        _fail(job, status["error"] or "X rejected the video during processing")
    else:
        job.status = "processing"
        check_after = status.get("check_after_secs")
        if check_after:
            job.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=check_after)


async def _do_tweet(db, job, publisher, post_model) -> None:
    if job.thread_parts:
        outcome = await publisher.publish_video_thread(
            job.media_id, job.thread_parts, alt_text=job.alt_text)
    else:
        outcome = await publisher.publish_video(
            job.media_id, job.caption or "", alt_text=job.alt_text,
            long_form=job.long_form)

    job.status = "published"
    job.tweet_id = outcome.media_id
    job.permalink = outcome.permalink

    if job.post_id:
        post = await db.get(post_model, job.post_id)
        if post is not None:
            post.status = "published"
            post.instagram_media_id = outcome.media_id   # platform post id, name kept for back-compat
            post.published_url = outcome.permalink
            post.published_at = datetime.now(timezone.utc)
            post.schedule_error = None
