"""The first time somebody rewrites what the AI wrote.

That moment is when brand rules start to mean something: the person has an
opinion about the voice and has just expressed it, in the only vocabulary that
matters — their own edit. UX phase 8 hangs a feature on it, so the moment has to
be detectable, and today it is not.

Two things were missing. A creator post kept no record of what the AI proposed
(`ai_caption` was written only in the Business branch, `business.py:420`), so
there was nothing to compare an edit against. And `PUT /caption` fires on
autosave, so "a request arrived" is not the same event as "somebody changed the
words".

Hence: the snapshot is taken wherever a post is born, and the comparison ignores
whitespace. A milestone that fires when nothing changed is a hint shown to
somebody who did nothing to earn it.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, Post, User
from services import milestones
from services.user_settings import _CRED_FIELDS

AI_WROTE = "A starter is flour, water and patience."


def _platform(db_url: str) -> Settings:
    fields = {f: "" for f in _CRED_FIELDS}
    fields.update(database_url=db_url, api_token="", app_mode="cloud")
    return Settings(**fields)


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'edit.db'}"


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
    yield TestClient(app)
    for dep in (get_db, get_settings):
        app.dependency_overrides.pop(dep, None)


@pytest.fixture
def owner(client, sm):
    """An account with one AI-written post, as the generator would have left it."""
    r = client.post("/api/auth/register",
                    json={"email": "editor@example.com", "password": "password123"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    async def _seed():
        async with sm() as db:
            user = (await db.execute(select(User).where(
                User.email == "editor@example.com"))).scalar_one()
            post = Post(user_id=user.id, managed_account_id=user.active_account_id,
                        topic="Sourdough", format="single", status="preview",
                        platform="instagram", caption=AI_WROTE, ai_caption=AI_WROTE)
            db.add(post)
            await db.commit()
            return post.id
    return {"headers": headers, "post_id": asyncio.run(_seed())}


def _reached(sm) -> dict:
    async def _go():
        async with sm() as db:
            user = (await db.execute(select(User).where(
                User.email == "editor@example.com"))).scalar_one()
            return milestones.all_for(user)
    return asyncio.run(_go())


def _edit(client, owner, **body):
    return client.put(f"/api/posts/{owner['post_id']}/caption", json=body,
                      headers=owner["headers"])


# ── the snapshot ────────────────────────────────────────────────────────────

def test_a_creator_post_remembers_what_the_ai_wrote(client, sm):
    """Without it there is nothing to compare an edit to, and the Business
    branch has had exactly this since phase 4 for exactly this reason."""
    from services.content_engine import GeneratedPost
    from models.schemas import PostFormat

    import api.routes.posts as posts_routes

    async def _persist_one():
        async with sm() as db:
            user = (await db.execute(select(User))).scalars().first()
            generated = GeneratedPost(
                id="snap-1", topic="Sourdough", format=PostFormat.SINGLE,
                caption=AI_WROTE, hashtags=[], cta="", hook="", alt_text="",
                seo_keywords=[], slides=[], text_model_used="m", image_model_used=None)
            await posts_routes._persist(generated, db, "branded_card",
                                        user_id=user.id if user else None)
            return (await db.get(Post, "snap-1")).ai_caption

    client.post("/api/auth/register",
                json={"email": "snap@example.com", "password": "password123"})
    assert asyncio.run(_persist_one()) == AI_WROTE


# ── what counts as an edit ──────────────────────────────────────────────────

def test_rewriting_the_caption_is_the_moment(client, sm, owner):
    _edit(client, owner, caption="Your starter is asleep, not dead. Feed it.")

    assert milestones.EDITED_AI_TEXT in _reached(sm)


def test_saving_without_changing_anything_is_not_an_edit(client, sm, owner):
    """The composer autosaves. A milestone that fires on a request rather than
    on a change would hand somebody a hint about their own voice for having
    typed nothing at all."""
    _edit(client, owner, caption=AI_WROTE)

    assert milestones.EDITED_AI_TEXT not in _reached(sm)


def test_a_whitespace_change_is_not_an_edit(client, sm, owner):
    """A trailing newline from a textarea is not an opinion about voice."""
    _edit(client, owner, caption=f"  {AI_WROTE}\n\n")

    assert milestones.EDITED_AI_TEXT not in _reached(sm)


def test_changing_only_the_hashtags_is_not_a_rewrite(client, sm, owner):
    """The feature this unlocks is about how the product WRITES. Swapping tags
    is housekeeping."""
    _edit(client, owner, hashtags=["#bread", "#baking"])

    assert milestones.EDITED_AI_TEXT not in _reached(sm)


def test_the_moment_is_the_first_one(client, sm, owner):
    _edit(client, owner, caption="First rewrite.")
    first = _reached(sm)[milestones.EDITED_AI_TEXT]

    _edit(client, owner, caption="Second rewrite.")

    assert _reached(sm)[milestones.EDITED_AI_TEXT] == first


def test_a_post_with_no_snapshot_never_fires(client, sm, owner):
    """Every post that existed before this phase. Comparing against NULL would
    make the first edit of an old post look like a rewrite of nothing — and
    crash on the normaliser if it were careless."""
    async def _forget():
        async with sm() as db:
            post = await db.get(Post, owner["post_id"])
            post.ai_caption = None
            await db.commit()
    asyncio.run(_forget())

    r = _edit(client, owner, caption="Anything at all.")

    assert r.status_code == 200
    assert milestones.EDITED_AI_TEXT not in _reached(sm)
