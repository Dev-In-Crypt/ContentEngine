"""APScheduler wrapper for scheduled Instagram publishing.

In cloud mode (24/7 backend) this makes scheduled posts publish even when the
user's PC is off. In local mode it only fires while the desktop app is open.

Jobs are persisted in the same database (SQLAlchemyJobStore) so they survive a
process restart — on startup APScheduler re-loads any pending jobs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from services.publish_retry import MAX_RETRIES, is_retryable, next_delay_minutes

log = logging.getLogger(__name__)

# Module-level singleton; set by init_scheduler() during app startup.
_scheduler: Optional[AsyncIOScheduler] = None
_sessionmaker = None


def _sync_jobstore_url(database_url: str) -> str:
    """APScheduler's SQLAlchemyJobStore is synchronous — strip async drivers."""
    return (
        database_url
        .replace("+aiosqlite", "")
        .replace("+asyncpg", "")
        .replace("postgres://", "postgresql://")  # normalize Render/Heroku style
    )


def init_scheduler(database_url: str, sessionmaker, poll_sources: bool = False) -> AsyncIOScheduler:
    global _scheduler, _sessionmaker
    _sessionmaker = sessionmaker
    jobstore = SQLAlchemyJobStore(url=_sync_jobstore_url(database_url))
    _scheduler = AsyncIOScheduler(
        jobstores={"default": jobstore},
        timezone="UTC",
    )
    _scheduler.start()
    # Daily disk housekeeping: drop upload dirs whose post was deleted.
    # replace_existing keeps it idempotent across restarts.
    _scheduler.add_job(
        _run_cleanup_job, trigger="interval", hours=24,
        id="upload_cleanup", replace_existing=True,
        misfire_grace_time=3600,
    )
    # Daily credential health sweep: catches a dead token while there's still time
    # to fix it, instead of at the moment a scheduled post fails to appear.
    _scheduler.add_job(
        _run_connection_check_job, trigger="interval", hours=24,
        id="connection_check", replace_existing=True,
        misfire_grace_time=3600,
    )
    # Business source polling (cloud only): rules-only, no LLM, once an hour.
    if poll_sources:
        _scheduler.add_job(
            _run_source_poll_job, trigger="interval", hours=1,
            id="source_poll", replace_existing=True,
            misfire_grace_time=1800,
        )
    else:
        # Local mode / previous cloud run: make sure a stale persisted job is gone.
        if _scheduler.get_job("source_poll"):
            _scheduler.remove_job("source_poll")
    log.info("APScheduler started with %d pending job(s)", len(_scheduler.get_jobs()))
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def schedule_publish(post_id: str, run_at: datetime) -> None:
    """Schedule (or reschedule) a publish job for a post."""
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialized")
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=timezone.utc)
    _scheduler.add_job(
        _run_publish_job,
        trigger="date",
        run_date=run_at,
        args=[post_id],
        id=f"pub_{post_id}",
        replace_existing=True,
        misfire_grace_time=3600,   # if the app was down at the exact time, still fire within 1h
    )


def cancel_publish(post_id: str) -> bool:
    if _scheduler is None:
        return False
    try:
        _scheduler.remove_job(f"pub_{post_id}")
        return True
    except Exception:
        return False


def get_job(post_id: str):
    if _scheduler is None:
        return None
    return _scheduler.get_job(f"pub_{post_id}")


async def _run_publish_job(post_id: str) -> None:
    """The scheduled publish.

    A coroutine on purpose: AsyncIOScheduler runs coroutine jobs on its own event
    loop, which is the app loop (the scheduler is started in the FastAPI
    lifespan). That's the loop _sessionmaker's async engine belongs to. A sync job
    would instead run in a worker thread and have to spin up a throwaway loop,
    using the engine across loops — the source of "Event loop is closed" errors.
    """
    from services.publisher_flow import publish_now

    failure: Optional[Exception] = None
    try:
        media_id = await publish_now(_sessionmaker, post_id)
        log.info("Scheduled publish OK: post=%s media=%s", post_id, media_id)
        await _clear_attempts(post_id)
        return
    except Exception as e:
        # publish_now already marked the post failed with the error; don't let the
        # exception escape into APScheduler, where the outcome would be invisible.
        # Held in `failure` because Python unbinds `e` at the end of the block.
        failure = e
        log.error("Scheduled publish FAILED: post=%s error=%s", post_id, e)

    try:
        await _handle_failure(post_id, failure)
    except Exception:
        # Retry bookkeeping must never take the job down with it.
        log.exception("Retry handling failed for post=%s", post_id)


async def _clear_attempts(post_id: str) -> None:
    """A published post starts its next slot with a full retry budget."""
    loaded = await _load_post_for_retry(_sessionmaker, post_id)
    if not loaded:
        return
    post, _email = loaded
    if post.publish_attempts:
        post.publish_attempts = 0
        await _save_retry_state(_sessionmaker, post)


async def _handle_failure(post_id: str, exc: Exception) -> None:
    """Re-arm a transient failure, or tell the owner it's over.

    Nobody is watching a scheduled publish, so the two outcomes that matter are
    "we'll try again" and "it did not go out". Permanent failures skip straight to
    the second: a bad token fails identically in an hour, and on X every attempt
    costs money.
    """
    loaded = await _load_post_for_retry(_sessionmaker, post_id)
    if not loaded:
        return
    post, owner_email = loaded

    delay = next_delay_minutes(post.publish_attempts or 0) if is_retryable(exc) else None
    if delay is None:
        if owner_email:
            await _notify_publish_failed(owner_email, post.topic or "your post", str(exc))
        return

    attempt = (post.publish_attempts or 0) + 1
    post.publish_attempts = attempt
    when = datetime.now(timezone.utc) + timedelta(minutes=delay)
    # Say so on the post itself: "failed" with a retry pending is a different
    # state from "failed, that's the end of it".
    post.schedule_error = (f"{exc} — retrying in {delay} min "
                           f"(attempt {attempt} of {MAX_RETRIES})")
    await _save_retry_state(_sessionmaker, post)
    schedule_publish(post_id, when)
    log.info("Scheduled publish retry %d/%d for post=%s in %d min",
             attempt, MAX_RETRIES, post_id, delay)


async def _load_post_for_retry(sessionmaker, post_id: str):
    """(post, owner_email) or None. Separate so tests can stub the DB away."""
    from models.database import Post as PostModel, User as UserModel

    async with sessionmaker() as db:
        post = await db.get(PostModel, post_id)
        if post is None:
            return None
        email = None
        if post.user_id:
            owner = await db.get(UserModel, post.user_id)
            email = getattr(owner, "email", None)
        db.expunge(post)
        return post, email


async def _save_retry_state(sessionmaker, post) -> None:
    from models.database import Post as PostModel

    async with sessionmaker() as db:
        row = await db.get(PostModel, post.id)
        if row is None:
            return
        row.publish_attempts = post.publish_attempts
        row.schedule_error = post.schedule_error
        await db.commit()


async def _notify_publish_failed(to: str, topic: str, reason: str) -> None:
    from services.email import send_publish_failed_email

    await send_publish_failed_email(to, topic, reason)


async def _run_connection_check_job() -> None:
    """Daily publishing-credential sweep. Never lets an error escape into APScheduler."""
    from services.connection_check import run_connection_check

    try:
        await run_connection_check(_sessionmaker)
    except Exception as e:
        log.error("Connection check FAILED: %s", e)


async def _run_cleanup_job() -> None:
    """Daily orphaned-uploads sweep. Never lets an error escape into APScheduler."""
    from services.cleanup import run_upload_cleanup

    try:
        await run_upload_cleanup(_sessionmaker)
    except Exception as e:
        log.error("Upload cleanup FAILED: %s", e)


async def _run_source_poll_job() -> None:
    """Hourly Business source poll (rules only, no LLM). Swallows all errors."""
    from config import get_settings
    from services.source_poller import poll_all

    try:
        await poll_all(_sessionmaker, ssl_verify=get_settings().ssl_verify)
    except Exception as e:
        log.error("Source poll FAILED: %s", e)


async def reconcile_scheduled(sessionmaker) -> None:
    """Fix posts stuck in 'scheduled' with no live job — e.g. the process was down
    at fire time (misfire dropped the job) or the jobstore lost it. Run once on
    startup: past-due posts are marked failed; still-future posts are re-armed."""
    from sqlalchemy import select

    from models.database import Post as PostModel

    now = datetime.now(timezone.utc)
    fixed = 0
    async with sessionmaker() as db:
        rows = (await db.execute(
            select(PostModel).where(PostModel.status == "scheduled")
        )).scalars().all()
        for post in rows:
            if get_job(post.id) is not None:
                continue   # still armed — leave it
            when = post.scheduled_at
            if when is not None and when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when is None or when <= now:
                post.status = "failed"
                post.schedule_error = "Missed its scheduled time while the server was offline."
            else:
                schedule_publish(post.id, when)   # re-arm the future job
            fixed += 1
        await db.commit()
    if fixed:
        log.info("Reconciled %d stale scheduled post(s)", fixed)
