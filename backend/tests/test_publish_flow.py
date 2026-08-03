"""Publish flow: no duplicate publishes, no posts stuck 'scheduled' on timeout."""
import asyncio
import io
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import services.publisher_flow as pf
from config import Settings
from models.database import Base, Post as PostModel, Slide as SlideModel
from services.instagram import InstagramError, InstagramPublisher


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (50, 50), "red").save(buf, format="JPEG")
    return buf.getvalue()


# ── IG container timeout raises the flow's own error type ───────────────────

async def test_wait_for_container_timeout_raises_instagram_error():
    """It used to raise builtin TimeoutError, which publisher_flow doesn't catch,
    so a slow-to-process post stayed 'scheduled' forever with images already up."""
    pub = InstagramPublisher(access_token="t", ig_user_id="u")
    try:
        with pytest.raises(InstagramError):
            await pub._wait_for_container("cid", max_retries=0)
    finally:
        await pub.close()


# ── publish_now DB-backed tests ─────────────────────────────────────────────

@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'pub.db'}"


@pytest.fixture
def sessionmaker(db_url):
    eng = create_async_engine(db_url)

    async def _ensure():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_ensure())
    return async_sessionmaker(eng, expire_on_commit=False)


async def _seed(sm, **cols):
    async with sm() as s:
        s.add(PostModel(**cols))
        await s.commit()


def _fake_settings():
    return Settings(
        instagram_access_token="tok", instagram_user_id="uid", imgbb_api_key="key",
    )


class _FakePublisher:
    """Records the images/caption it was handed, returns a canned outcome."""
    def __init__(self, outcome=None, error=None):
        from services.publishing.base import PublishOutcome
        self.outcome = outcome or PublishOutcome(media_id="pub-1", permalink="https://x/1")
        self.error = error
        self.called_with = None

    async def publish(self, images, caption, alt_text=None, long_form=False):
        # long_form is X-only (Premium lifts the char cap); the flow passes it for X.
        self.called_with = (images, caption, alt_text)
        self.long_form = long_form
        if self.error:
            raise self.error
        return self.outcome

    async def close(self):
        ...


async def test_publish_now_idempotent_when_already_published(sessionmaker, monkeypatch):
    """A post already published must not be published a second time — guards the
    double-click and the manual+scheduled-job race."""
    pid = str(uuid.uuid4())
    await _seed(sessionmaker, id=pid, topic="t", format="single", status="published",
                instagram_media_id="existing-123")

    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))

    def boom(*a, **k):
        raise AssertionError("should not build a publisher for an already-published post")

    monkeypatch.setattr(pf, "make_publisher_for", boom)

    media_id = await pf.publish_now(sessionmaker, pid)
    assert media_id == "existing-123"


async def test_publish_now_routes_to_platform_publisher(sessionmaker, monkeypatch, tmp_path):
    """publish_now hands the slide bytes to the publisher built for the post's
    platform, and records the returned id + permalink."""
    from services.publishing.base import PublishOutcome
    pid = str(uuid.uuid4())
    img_path = tmp_path / "slide_1.jpg"
    img_path.write_bytes(_jpeg())
    await _seed(sessionmaker, id=pid, topic="t", format="single", status="preview", platform="x")
    async with sessionmaker() as s:
        s.add(SlideModel(post_id=pid, slide_number=1, image_source="stock",
                         image_path=str(img_path)))
        await s.commit()

    fake = _FakePublisher(PublishOutcome(media_id="tw9", permalink="https://x.com/i/web/status/tw9"))
    seen = {}

    def factory(platform, settings, name_prefix="slide"):
        seen["platform"] = platform
        return fake

    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))
    monkeypatch.setattr(pf, "make_publisher_for", factory)

    media_id = await pf.publish_now(sessionmaker, pid)

    assert seen["platform"] == "x"          # routed by post.platform
    assert media_id == "tw9"
    assert fake.called_with[0] == [_jpeg()]  # slide bytes handed over
    async with sessionmaker() as s:
        post = await s.get(PostModel, pid)
        assert post.status == "published"
        assert post.instagram_media_id == "tw9"
        assert post.published_url == "https://x.com/i/web/status/tw9"


async def test_publish_now_marks_failed_on_publisher_error(sessionmaker, monkeypatch, tmp_path):
    """Any publisher failure marks the post failed, not left stuck 'scheduled'."""
    pid = str(uuid.uuid4())
    img_path = tmp_path / "slide_1.jpg"
    img_path.write_bytes(_jpeg())
    await _seed(sessionmaker, id=pid, topic="t", format="single", status="scheduled")
    async with sessionmaker() as s:
        s.add(SlideModel(post_id=pid, slide_number=1, image_source="stock",
                         image_path=str(img_path)))
        await s.commit()

    fake = _FakePublisher(error=RuntimeError("platform exploded"))
    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))
    monkeypatch.setattr(pf, "make_publisher_for", lambda *a, **k: fake)

    with pytest.raises(pf.PublishError):
        await pf.publish_now(sessionmaker, pid)

    async with sessionmaker() as s:
        post = await s.get(PostModel, pid)
        assert post.status == "failed"
        assert post.schedule_error


async def test_publish_uses_post_owner_credentials(sessionmaker, monkeypatch, tmp_path):
    """publish_now builds the publisher from the POST OWNER's stored (encrypted)
    keys, not the global platform settings — the core of per-user publishing."""
    from services.secrets import encrypt
    from models.database import User, UserCredentials

    pid = str(uuid.uuid4())
    img_path = tmp_path / "slide_1.jpg"
    img_path.write_bytes(_jpeg())

    owner_id = str(uuid.uuid4())
    async with sessionmaker() as s:
        s.add(User(id=owner_id, email="owner@ex.com"))
        s.add(UserCredentials(
            user_id=owner_id,
            instagram_access_token_enc=encrypt("owner-ig-token"),
            instagram_user_id_enc=encrypt("owner-ig-uid"),
            imgbb_api_key_enc=encrypt("owner-imgbb"),
        ))
        await s.commit()
    await _seed(sessionmaker, id=pid, user_id=owner_id, topic="t", format="single",
                status="preview", platform="instagram")
    async with sessionmaker() as s:
        s.add(SlideModel(post_id=pid, slide_number=1, image_source="stock",
                         image_path=str(img_path)))
        await s.commit()

    captured = {}

    def factory(platform, settings, name_prefix="slide"):
        captured["ig_token"] = settings.instagram_access_token
        captured["imgbb"] = settings.imgbb_api_key
        return _FakePublisher()

    # Real settings_for_post_owner (exercises cred loading); only the publisher mocked.
    monkeypatch.setattr(pf, "make_publisher_for", factory)

    await pf.publish_now(sessionmaker, pid)
    assert captured["ig_token"] == "owner-ig-token"
    assert captured["imgbb"] == "owner-imgbb"


# ── manual publish cancels the pending scheduled job ────────────────────────

def test_publish_endpoint_cancels_scheduled_job(sessionmaker, monkeypatch):
    """A manual publish must drop the pending APScheduler job, or the job fires
    later and double-publishes (the idempotency guard is the backstop, not the
    only line of defence)."""
    from fastapi.testclient import TestClient

    import services.scheduler as sched
    from api.deps import get_settings
    from main import app

    cancelled = {}
    monkeypatch.setattr(sched, "cancel_publish", lambda pid: cancelled.setdefault("pid", pid))
    monkeypatch.setattr(pf, "publish_now", AsyncMock(return_value="media-9"))

    # Publish now verifies ownership first, so the post must exist.
    pid = str(uuid.uuid4())
    asyncio.run(_seed(sessionmaker, id=pid, topic="t", format="single", status="preview"))

    app.state.sessionmaker = sessionmaker
    app.dependency_overrides[get_settings] = lambda: Settings(api_token="")
    try:
        tc = TestClient(app)
        res = tc.post(f"/api/posts/{pid}/publish")
        assert res.status_code == 200
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert "pid" in cancelled   # cancel_publish was called before publishing


# ── publish_reel_now: symmetry with publish_now (X.5) ───────────────────────

async def test_publish_reel_idempotent_when_already_published(sessionmaker, monkeypatch):
    pid = str(uuid.uuid4())
    await _seed(sessionmaker, id=pid, topic="t", format="reel", status="published",
                instagram_media_id="reel-existing")
    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))

    def boom(*a, **k):
        raise AssertionError("should not re-publish a published reel")

    monkeypatch.setattr(pf, "InstagramPublisher", boom)

    media_id = await pf.publish_reel_now(sessionmaker, pid, "https://x/v.mp4")
    assert media_id == "reel-existing"


async def test_publish_reel_marks_failed_on_non_ig_error(sessionmaker, monkeypatch):
    """A non-InstagramError (network/timeout) during the reel publish must mark
    the post failed, not leave it hanging in its current status."""
    pid = str(uuid.uuid4())
    await _seed(sessionmaker, id=pid, topic="t", format="reel", status="scheduled")
    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))

    class FakePublisher:
        def __init__(self, *a, **k): ...
        async def publish_reel(self, *a, **k):
            raise RuntimeError("network reset")   # NOT an InstagramError
        async def close(self): ...

    monkeypatch.setattr(pf, "InstagramPublisher", FakePublisher)

    with pytest.raises(pf.PublishError):
        await pf.publish_reel_now(sessionmaker, pid, "https://x/v.mp4")

    async with sessionmaker() as s:
        post = await s.get(PostModel, pid)
        assert post.status == "failed"
        assert post.schedule_error


async def test_publish_reel_rejects_non_instagram(sessionmaker, monkeypatch):
    """publish_reel_now stays the Instagram-only half; an X post must go
    through Publish to X (services/x_video_publish.py) instead."""
    pid = str(uuid.uuid4())
    await _seed(sessionmaker, id=pid, topic="t", format="reel", status="preview", platform="x")
    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))
    with pytest.raises(pf.PublishError, match="Publish to X"):
        await pf.publish_reel_now(sessionmaker, pid, "https://x/v.mp4")


# ── thread routing (PART XXIV) ──────────────────────────────────────────────

class _FakeThreadPublisher(_FakePublisher):
    """Adds the thread entry point so we can see which one the flow picked."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.thread_called_with = None

    async def publish_thread(self, parts, images, alt_text=None):
        self.thread_called_with = (list(parts), images, alt_text)
        return self.outcome


async def _seed_post_with_slide(sm, tmp_path, pid, **cols):
    img_path = tmp_path / f"{pid}.jpg"
    img_path.write_bytes(_jpeg())
    await _seed(sm, id=pid, topic="t", format="single", status="preview", **cols)
    async with sm() as s:
        s.add(SlideModel(post_id=pid, slide_number=1, image_source="stock",
                         image_path=str(img_path)))
        await s.commit()


async def test_x_post_with_thread_parts_goes_to_publish_thread(
        sessionmaker, monkeypatch, tmp_path):
    """A generated thread must be posted as a chain, not flattened into one tweet."""
    pid = str(uuid.uuid4())
    await _seed_post_with_slide(sessionmaker, tmp_path, pid, platform="x",
                                caption="One.\n\nTwo.", hashtags=["#run"],
                                thread_parts=["One.", "Two."])

    fake = _FakeThreadPublisher()
    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))
    monkeypatch.setattr(pf, "make_publisher_for", lambda *a, **k: fake)

    await pf.publish_now(sessionmaker, pid)

    assert fake.called_with is None                       # single-tweet path untouched
    parts, images, _ = fake.thread_called_with
    assert parts[0] == "One."                             # hook stays clean
    assert parts[-1].endswith("#run")                     # hashtags ride the LAST tweet
    assert images == [_jpeg()]


async def test_x_post_without_thread_parts_still_uses_publish(
        sessionmaker, monkeypatch, tmp_path):
    pid = str(uuid.uuid4())
    await _seed_post_with_slide(sessionmaker, tmp_path, pid, platform="x",
                                caption="Just one.", hashtags=["#run"])

    fake = _FakeThreadPublisher()
    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))
    monkeypatch.setattr(pf, "make_publisher_for", lambda *a, **k: fake)

    await pf.publish_now(sessionmaker, pid)

    assert fake.thread_called_with is None
    assert fake.called_with[1] == "Just one.\n\n#run"


async def _seed_user(sm, uid, x_premium):
    from models.database import User as UserModel
    async with sm() as s:
        s.add(UserModel(id=uid, email=f"{uid}@e.com", x_premium=x_premium))
        await s.commit()


async def test_text_only_x_post_publishes_with_no_images(sessionmaker, monkeypatch):
    """A zero-slide X post is a text-only tweet — it must publish with images==[].
    Mutation guard: drop the platform=='x' exemption → it fails 'No slides'."""
    pid = str(uuid.uuid4())
    await _seed(sessionmaker, id=pid, topic="t", format="single", status="preview",
                platform="x", caption="Just a thought.")   # NO slides seeded
    fake = _FakeThreadPublisher()
    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))
    monkeypatch.setattr(pf, "make_publisher_for", lambda *a, **k: fake)

    await pf.publish_now(sessionmaker, pid)
    assert fake.called_with[0] == []                       # images empty
    assert fake.called_with[1] == "Just a thought."


async def test_text_only_rejected_on_instagram(sessionmaker, monkeypatch):
    """Instagram needs media — a zero-slide IG post must still fail fast."""
    pid = str(uuid.uuid4())
    await _seed(sessionmaker, id=pid, topic="t", format="single", status="preview",
                platform="instagram", caption="No image here.")
    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))
    monkeypatch.setattr(pf, "make_publisher_for", lambda *a, **k: _FakePublisher())
    with pytest.raises(pf.PublishError, match="No slides"):
        await pf.publish_now(sessionmaker, pid)


async def test_insights_use_owner_instagram_token(sessionmaker):
    """refresh_insights (and publishing) must use the OWNER's own IG token, not the
    platform .env — build_settings_for_user overlays the tenant's encrypted key.
    Mutation guard: read the global settings instead → the env token wins, fails."""
    from models.database import User as UserModel, UserCredentials as CredsModel
    from services.secrets import encrypt
    from services.user_settings import build_settings_for_user

    uid = str(uuid.uuid4())
    async with sessionmaker() as s:
        s.add(UserModel(id=uid, email="ig@e.com", is_local=False))
        s.add(CredsModel(user_id=uid,
                         instagram_access_token_enc=encrypt("tenant-tok"),
                         instagram_user_id_enc=encrypt("tenant-uid")))
        await s.commit()
        user = await s.get(UserModel, uid)
        settings = await build_settings_for_user(s, user)
    assert settings.instagram_access_token == "tenant-tok"
    assert settings.instagram_user_id == "tenant-uid"


async def test_long_form_only_for_premium_owner(sessionmaker, monkeypatch, tmp_path):
    """A >250-char single caption is sent uncut ONLY when the owner is X Premium.
    Non-Premium must be fit to the cap (else X rejects >280). Mutation guard: drop
    the x_premium check → long_form is True for the non-Premium owner too."""
    long_cap = "word " * 100                              # ~500 chars, no thread
    # Premium owner → long_form True (uncut)
    puid, ppid = str(uuid.uuid4()), str(uuid.uuid4())
    await _seed_user(sessionmaker, puid, True)
    await _seed_post_with_slide(sessionmaker, tmp_path, ppid, platform="x",
                                caption=long_cap, user_id=puid)
    fake_p = _FakeThreadPublisher()
    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))
    monkeypatch.setattr(pf, "make_publisher_for", lambda *a, **k: fake_p)
    await pf.publish_now(sessionmaker, ppid)
    assert fake_p.long_form is True

    # Non-Premium owner → long_form False (fit to the cap)
    nuid, npid = str(uuid.uuid4()), str(uuid.uuid4())
    await _seed_user(sessionmaker, nuid, False)
    await _seed_post_with_slide(sessionmaker, tmp_path, npid, platform="x",
                                caption=long_cap, user_id=nuid)
    fake_n = _FakeThreadPublisher()
    monkeypatch.setattr(pf, "make_publisher_for", lambda *a, **k: fake_n)
    await pf.publish_now(sessionmaker, npid)
    assert fake_n.long_form is False


async def test_instagram_ignores_thread_parts(sessionmaker, monkeypatch, tmp_path):
    """thread_parts on a non-X post must never reroute Instagram — X-only feature."""
    pid = str(uuid.uuid4())
    await _seed_post_with_slide(sessionmaker, tmp_path, pid, platform="instagram",
                                caption="Cap", thread_parts=["a", "b"])

    fake = _FakeThreadPublisher()
    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))
    monkeypatch.setattr(pf, "make_publisher_for", lambda *a, **k: fake)

    await pf.publish_now(sessionmaker, pid)

    assert fake.thread_called_with is None
    assert fake.called_with[1] == "Cap"


async def test_thread_hashtags_are_appended_once_and_never_clipped(
        sessionmaker, monkeypatch, tmp_path):
    """The tags ride the last tweet; if it's full, the TEXT gives way, not the tag."""
    from models.schemas import TWEET_CHAR_LIMIT
    pid = str(uuid.uuid4())
    long_tail = "word " * 60          # ~300 chars, well over the limit with tags
    await _seed_post_with_slide(sessionmaker, tmp_path, pid, platform="x",
                                caption="Hook.", hashtags=["#Walking", "#Habits"],
                                thread_parts=["Hook.", long_tail])

    fake = _FakeThreadPublisher()
    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))
    monkeypatch.setattr(pf, "make_publisher_for", lambda *a, **k: fake)

    await pf.publish_now(sessionmaker, pid)

    last = fake.thread_called_with[0][-1]
    assert last.endswith("#Walking #Habits")          # both tags whole
    assert last.count("#Walking") == 1                # not doubled
    assert len(last) <= TWEET_CHAR_LIMIT


async def test_long_x_post_is_not_squeezed_to_the_tweet_limit(
        sessionmaker, monkeypatch, tmp_path):
    """An X Premium long post has no cap — appending tags must not trigger a trim.
    Long-form is keyed off the OWNER's x_premium flag, so seed a Premium owner."""
    from models.schemas import TWEET_CHAR_LIMIT
    pid, uid = str(uuid.uuid4()), str(uuid.uuid4())
    await _seed_user(sessionmaker, uid, True)
    body = "sentence. " * 90                          # ~900 chars, no thread
    await _seed_post_with_slide(sessionmaker, tmp_path, pid, platform="x",
                                caption=body, hashtags=["#Walking"], user_id=uid)

    fake = _FakeThreadPublisher()
    monkeypatch.setattr(pf, "settings_for_post_owner", AsyncMock(return_value=_fake_settings()))
    monkeypatch.setattr(pf, "make_publisher_for", lambda *a, **k: fake)

    await pf.publish_now(sessionmaker, pid)

    _, caption, _ = fake.called_with
    assert len(caption) > TWEET_CHAR_LIMIT            # untouched
    assert caption.endswith("#Walking")


# ── build_x_text (8.2 prep: shared by publish_now and the Phase 8 video path) ──

from models.database import User as UserModel  # noqa: E402


def test_build_x_text_appends_tags_and_fits_the_char_limit():
    from models.schemas import TWEET_CHAR_LIMIT
    post = PostModel(caption="sentence. " * 90, hashtags=["#Walking"])
    caption, thread_parts, long_form = pf.build_x_text(post, owner=None)
    assert thread_parts == []
    assert long_form is False
    assert len(caption) <= TWEET_CHAR_LIMIT
    assert caption.endswith("#Walking")


def test_build_x_text_long_form_needs_a_premium_owner():
    post = PostModel(caption="sentence. " * 90, hashtags=["#Walking"])
    owner = UserModel(email="a@ex.com", x_premium=False)
    _caption, _thread_parts, long_form = pf.build_x_text(post, owner)
    assert long_form is False

    owner.x_premium = True
    _caption2, _thread_parts2, long_form2 = pf.build_x_text(post, owner)
    assert long_form2 is True


def test_build_x_text_puts_tags_only_on_the_last_thread_tweet():
    """Mutation guard: tag every part instead of just the last → the hook tweet
    carries hashtags it was never supposed to."""
    post = PostModel(caption="unused", hashtags=["#Walking"],
                     thread_parts=["Hook.", "Middle.", "End."])
    _caption, thread_parts, _long_form = pf.build_x_text(post, owner=None)
    assert thread_parts[0] == "Hook."
    assert thread_parts[1] == "Middle."
    assert thread_parts[2].endswith("#Walking")
