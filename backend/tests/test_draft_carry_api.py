"""POST /api/posts/from-draft — what the landing made, kept after signing up.

The landing writes nothing down (UX phase 7.1), so the post a visitor watched
being written lives in their browser and nowhere else. This is how it survives
registration: the browser hands the words and the picture back, and they become
an ordinary preview post.

The thing to understand about this route is what it is NOT. No model is called
and no allowance is spent — the generation already happened, on our key, on the
landing. Charging a free post for carrying it across would bill the same person
twice for one post, and re-generating it would hand them a DIFFERENT post than
the one they signed up for.

Which makes it the one route in the product that persists text and an image
supplied entirely by the client. Nothing here is trusted: the caption is capped,
the picture is re-encoded through Pillow rather than written as it arrived, and
anything that is not an image is refused.
"""
import asyncio
import base64
import io
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, Post, User
from services.user_settings import _CRED_FIELDS

UPLOADS_DIR = Path(__file__).resolve().parents[1] / "uploads" / "posts"


def _png(size=(120, 150), color="teal") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _data_url(raw: bytes, mime="image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _platform(db_url: str, **over) -> Settings:
    fields = {f: "" for f in _CRED_FIELDS}
    fields.update(database_url=db_url, api_token="", app_mode="cloud",
                  openrouter_api_key="app-key",
                  default_text_provider="openrouter", default_text_model="our/model")
    fields.update(over)
    return Settings(**fields)


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'draft.db'}"


@pytest.fixture
def sm(db_url):
    eng = create_async_engine(db_url)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


@pytest.fixture
def client(db_url, sm):
    async def override_db():
        async with sm() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: _platform(db_url)
    app.state.sessionmaker = sm

    tc = TestClient(app)
    tc.created = []
    yield tc

    for post_id in tc.created:
        d = UPLOADS_DIR / post_id
        if d.exists():
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
    for dep in (get_db, get_settings):
        app.dependency_overrides.pop(dep, None)


def _register(client, email="carried@example.com") -> dict:
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _draft(**over) -> dict:
    body = dict(topic="Sourdough starters",
                caption="A starter is flour, water and patience.",
                hook="Your starter is not dead.", cta="Save this.",
                hashtags=["#sourdough", "#baking"],
                image_data_url=_data_url(_png()))
    body.update(over)
    return body


def _carry(client, headers, **over):
    r = client.post("/api/posts/from-draft", json=_draft(**over), headers=headers)
    if r.status_code == 200:
        client.created.append(r.json()["id"])
    return r


def _used(sm, email: str) -> int:
    async def _go():
        async with sm() as db:
            return (await db.execute(select(User.free_generations_used)
                                     .where(User.email == email))).scalar_one()
    return asyncio.run(_go())


# ── it arrives ──────────────────────────────────────────────────────────────

def test_the_landing_post_becomes_a_real_one(client, sm):
    headers = _register(client)
    r = _carry(client, headers)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["caption"] == "A starter is flour, water and patience."
    assert body["hook"] == "Your starter is not dead."
    assert body["hashtags"] == ["#sourdough", "#baking"]
    assert body["status"] == "preview"


def test_it_belongs_to_the_account_that_carried_it(client, sm):
    headers = _register(client)
    post_id = _carry(client, headers).json()["id"]

    async def _owner():
        async with sm() as db:
            post = await db.get(Post, post_id)
            user = (await db.execute(select(User).where(
                User.email == "carried@example.com"))).scalar_one()
            return post.user_id, user.id, post.managed_account_id, user.active_account_id
    owner, uid, acct, active = asyncio.run(_owner())

    assert owner == uid
    # On the active profile, like every other post since UX phase 2 — otherwise
    # it is invisible in a list that filters on exactly that.
    assert acct == active


def test_the_picture_is_written_where_a_slide_lives(client):
    headers = _register(client)
    post_id = _carry(client, headers).json()["id"]

    assert (UPLOADS_DIR / post_id / "slide_1.jpg").exists()


def test_carrying_a_draft_over_costs_no_free_post(client, sm):
    """The generation already happened, on our key, on the landing. Charging an
    allowance to keep it would bill the same person twice for one post."""
    headers = _register(client)
    _carry(client, headers)

    assert _used(sm, "carried@example.com") == 0


def test_the_carried_draft_is_the_words_from_the_landing(client):
    """Not a fresh generation. Somebody signed up because of a particular post;
    handing them a different one is a worse outcome than handing them none."""
    headers = _register(client)
    r = _carry(client, headers, caption="The exact words they signed up for.")

    assert r.json()["caption"] == "The exact words they signed up for."


# ── nothing here is trusted ─────────────────────────────────────────────────

def test_a_carried_image_is_re_encoded(client):
    """Bytes from a browser, written to our disk. Re-encoding through Pillow is
    what makes "an image" true rather than claimed — the same treatment a logo
    upload already gets."""
    headers = _register(client)
    post_id = _carry(client, headers).json()["id"]

    saved = (UPLOADS_DIR / post_id / "slide_1.jpg").read_bytes()
    assert saved[:2] == b"\xff\xd8"                     # JPEG, whatever arrived
    assert Image.open(io.BytesIO(saved)).format == "JPEG"


def test_something_that_is_not_an_image_is_refused(client):
    headers = _register(client)
    r = client.post("/api/posts/from-draft",
                    json=_draft(image_data_url=_data_url(b"#!/bin/sh\nrm -rf /",
                                                         "image/png")),
                    headers=headers)

    assert r.status_code == 422


def test_a_data_url_that_is_not_an_image_is_refused_on_sight(client):
    """Pillow would refuse this too, so the prefix check looks redundant — until
    you notice it refuses BEFORE base64-decoding several megabytes of whatever
    it actually is. The two refusals say different things, which is how this
    test can tell which one fired."""
    headers = _register(client)
    r = client.post("/api/posts/from-draft",
                    json=_draft(image_data_url=_data_url(b"<h1>not a picture</h1>",
                                                         "text/html")),
                    headers=headers)

    assert r.status_code == 422
    assert "doesn't look like" in r.json()["detail"]


def test_a_draft_with_no_picture_is_still_worth_keeping(client):
    """The words are the part somebody read. A landing run whose picture failed
    should not cost them the post."""
    headers = _register(client)
    r = _carry(client, headers, image_data_url=None)

    assert r.status_code == 200, r.text
    assert r.json()["caption"]


def test_an_enormous_caption_is_refused(client):
    headers = _register(client)
    r = client.post("/api/posts/from-draft",
                    json=_draft(caption="x" * 20_000), headers=headers)

    assert r.status_code == 422


def test_an_enormous_picture_is_refused(client):
    """A data URL is a POST body. Without a ceiling, "carry my draft" is an
    upload endpoint with no limit on it."""
    headers = _register(client)
    r = client.post("/api/posts/from-draft",
                    json=_draft(image_data_url=_data_url(b"\x00" * (9 * 1024 * 1024))),
                    headers=headers)

    assert r.status_code == 422


def test_it_needs_an_account(client):
    assert client.post("/api/posts/from-draft", json=_draft()).status_code == 401


def test_one_account_cannot_carry_into_another(client, sm):
    """There is no id to confuse here — the draft is whatever the client sends —
    so the only thing to get wrong is whose post it becomes."""
    first = _register(client, "first@example.com")
    second = _register(client, "second@example.com")
    post_id = _carry(client, second).json()["id"]

    assert client.get(f"/api/posts/{post_id}", headers=first).status_code == 404
