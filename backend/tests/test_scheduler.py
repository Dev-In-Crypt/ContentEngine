"""The scheduled-publish job must be a coroutine run on the app's event loop.

It used to be a sync function that called asyncio.run() from an APScheduler
worker thread, creating a fresh event loop per job while reusing the app's async
DB engine — which is bound to the app loop. That produced intermittent
"attached to a different loop" / "Event loop is closed" failures on SQLite and
broke outright on asyncpg. As a coroutine, AsyncIOScheduler runs it on its own
(the app's) loop.
"""
import inspect
from unittest.mock import AsyncMock

import services.scheduler as scheduler


def test_run_publish_job_is_a_coroutine():
    # If this reverts to a sync def, AsyncIOScheduler runs it in a worker thread
    # with no loop and the asyncio.run() bridge comes back.
    assert inspect.iscoroutinefunction(scheduler._run_publish_job)


async def test_run_publish_job_awaits_publish_now(monkeypatch):
    called = {}

    async def fake_publish_now(sessionmaker, post_id):
        called["post_id"] = post_id
        return "media-123"

    monkeypatch.setattr("services.publisher_flow.publish_now", fake_publish_now)
    monkeypatch.setattr(scheduler, "_sessionmaker", object())

    await scheduler._run_publish_job("post-abc")

    assert called["post_id"] == "post-abc"


async def test_run_publish_job_swallows_failure(monkeypatch):
    """publish_now already marks the post failed; the job must not raise (that
    would escape into APScheduler and leave the outcome invisible)."""
    failing = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr("services.publisher_flow.publish_now", failing)
    monkeypatch.setattr(scheduler, "_sessionmaker", object())

    await scheduler._run_publish_job("post-abc")   # must not raise


def test_sync_jobstore_driver_is_installed():
    """APScheduler's SQLAlchemyJobStore uses a SYNC engine. On Postgres the URL
    resolves to postgresql:// which needs psycopg2 — without it the scheduler
    silently fails to start (caught in lifespan) and no scheduled post fires."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError
    url = scheduler._sync_jobstore_url("postgresql+asyncpg://u:p@localhost:1/db")
    assert url == "postgresql://u:p@localhost:1/db"
    try:
        create_engine(url).connect()
    except ModuleNotFoundError as e:   # the failure we're guarding against
        raise AssertionError(f"sync Postgres driver missing: {e}") from e
    except OperationalError:
        pass   # driver present, just can't reach a fake host — that's fine


# ── retry + notify ───────────────────────────────────────────────────────────

class _Post:
    """Minimal stand-in for the ORM row the retry path touches."""
    def __init__(self):
        self.id, self.topic, self.user_id = "p1", "A topic", "u1"
        self.publish_attempts, self.schedule_error, self.status = 0, None, "failed"


def _stub_post_access(monkeypatch, post):
    """Route the job's DB helpers at an in-memory post + capture emails."""
    sent = []
    async def _load(_sm, _pid): return post, "owner@example.com"
    async def _save(_sm, _post): return None
    async def _mail(to, topic, reason): sent.append((to, topic, reason))
    monkeypatch.setattr(scheduler, "_load_post_for_retry", _load)
    monkeypatch.setattr(scheduler, "_save_retry_state", _save)
    monkeypatch.setattr(scheduler, "_notify_publish_failed", _mail)
    monkeypatch.setattr(scheduler, "_sessionmaker", object())
    return sent


async def test_a_transient_failure_is_rescheduled_not_reported(monkeypatch):
    """A dropped connection is worth another go, and the user shouldn't be
    emailed about a blip we're already handling."""
    post = _Post()
    sent = _stub_post_access(monkeypatch, post)
    armed = {}
    monkeypatch.setattr(scheduler, "schedule_publish",
                        lambda pid, when: armed.update(pid=pid, when=when))
    monkeypatch.setattr("services.publisher_flow.publish_now",
                        AsyncMock(side_effect=RuntimeError("X network error: connection reset")))

    await scheduler._run_publish_job("p1")

    assert armed["pid"] == "p1"                 # another attempt is on the books
    assert post.publish_attempts == 1
    assert sent == []                           # no email while retries remain


async def test_a_permanent_failure_is_reported_immediately(monkeypatch):
    """A bad token fails the same way in an hour — retrying only delays the news."""
    post = _Post()
    sent = _stub_post_access(monkeypatch, post)
    # Recorded, not raised: _run_publish_job swallows exceptions from the retry
    # path, so a pytest.fail() in here would vanish and the guard would be silent.
    retries = []
    monkeypatch.setattr(scheduler, "schedule_publish", lambda pid, when: retries.append(pid))
    monkeypatch.setattr("services.publisher_flow.publish_now",
                        AsyncMock(side_effect=RuntimeError("X tweet failed: 401 Unauthorized")))

    await scheduler._run_publish_job("p1")

    assert retries == []                        # a 401 must never be retried
    assert post.publish_attempts == 0           # no attempt was spent
    assert len(sent) == 1 and "401" in sent[0][2]


async def test_the_last_retry_reports_instead_of_looping(monkeypatch):
    """Mutation guard: without the budget check this reschedules forever."""
    post = _Post()
    post.publish_attempts = scheduler.MAX_RETRIES
    sent = _stub_post_access(monkeypatch, post)
    retries = []
    monkeypatch.setattr(scheduler, "schedule_publish", lambda pid, when: retries.append(pid))
    monkeypatch.setattr("services.publisher_flow.publish_now",
                        AsyncMock(side_effect=RuntimeError("X network error: reset")))

    await scheduler._run_publish_job("p1")

    assert retries == []                        # budget was exhausted
    assert len(sent) == 1


async def test_success_clears_the_attempt_counter(monkeypatch):
    """Otherwise a post that once blipped starts its next slot pre-spent."""
    post = _Post()
    post.publish_attempts = 2
    _stub_post_access(monkeypatch, post)
    monkeypatch.setattr("services.publisher_flow.publish_now", AsyncMock(return_value="mid"))

    await scheduler._run_publish_job("p1")

    assert post.publish_attempts == 0
