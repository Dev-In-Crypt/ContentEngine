"""The one post we pay for: POST /api/onboarding/first-post.

Onboarding ends on something real rather than an empty composer, and a
brand-new account has no key yet — so this route writes on the application's
own key. That makes it the only endpoint in the product that spends our money,
which is why almost everything here is about refusing.

The two properties that keep it from becoming an LLM proxy:

  * **The request carries no prompt.** No topic, no instructions, no niche;
    `extra="forbid"` rejects one if somebody adds it. The subject is assembled
    server-side from the profile the account already saved, so the worst an
    attacker gets is one short post about a niche they typed in themselves.
  * **The allowance is spent before the model is called.** The counter lives on
    the account, survives a new browser and a cleared localStorage, and is
    committed by `free_generation.reserve` before the provider is touched.

The order of the guards is itself a guard: a request that will be refused must
be refused *before* it costs anything, so the brand check comes ahead of the
reservation and the app-key check comes ahead of both.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_demo_text_provider, get_settings
from config import Settings
from main import app
from models.database import Base, ManagedAccount, User
from services.caption_generator import GeneratedCaption


def _caption(**over) -> GeneratedCaption:
    fields = dict(
        caption="Your starter is not dead — it is asleep.",
        hashtags=["#sourdough"], cta="Save this.", hook="Flour, water, patience.",
        image_search_queries=[], image_gen_prompts=[], alt_text="A loaf.",
        seo_keywords=["sourdough"], slide_overlays=[], thread_parts=[], sources=[],
    )
    fields.update(over)
    return GeneratedCaption(**fields)


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'onb.db'}"


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
def caption_gen():
    """Stubbed at `generate`, never at the provider: CaptionGenerator is not
    deterministic, retries once on unparseable JSON, and X modes fire extra
    shortening calls — so a provider-call count would flake. Counting calls to
    `generate` is stable and is what the cost question is actually about."""
    gen = AsyncMock()
    gen.generate.return_value = _caption()
    return gen


@pytest.fixture
def client(db_url, sm, caption_gen, monkeypatch):
    import api.routes.onboarding as onboarding_routes

    async def override_db():
        async with sm() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url=db_url, api_token="", app_mode="cloud",
        default_text_model="app/text-model")
    # A provider object that is merely not-None: the route hands it to the
    # caption generator, which is stubbed.
    app.dependency_overrides[get_demo_text_provider] = lambda: object()
    monkeypatch.setattr(onboarding_routes, "CaptionGenerator", lambda _p: caption_gen)
    app.state.sessionmaker = sm

    yield TestClient(app)

    for dep in (get_db, get_settings, get_demo_text_provider):
        app.dependency_overrides.pop(dep, None)


def _register(client, email="new@example.com") -> dict:
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _set_brand(sm, email: str, **fields) -> None:
    """Write the profile the way the onboarding screens would have."""
    async def _go():
        async with sm() as db:
            u = (await db.execute(select(User).where(User.email == email))).scalar_one()
            acct = (await db.execute(select(ManagedAccount).where(
                ManagedAccount.owner_user_id == u.id,
                ManagedAccount.is_primary.is_(True)))).scalars().first()
            for k, v in fields.items():
                setattr(acct, k, v)
            await db.commit()
    asyncio.run(_go())


def _used(sm, email: str) -> int:
    async def _go():
        async with sm() as db:
            return (await db.execute(select(User.free_generations_used)
                                     .where(User.email == email))).scalar_one()
    return asyncio.run(_go())


def _events(resp) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in resp.text.splitlines()
            if line.startswith("data: ")]


@pytest.fixture
def ready(client, sm):
    """A registered account whose brand is on file — the state screen 3 leaves."""
    headers = _register(client)
    _set_brand(sm, "new@example.com", niche="Sourdough baking",
               target_audience="Home bakers", brand_name="Crumb & Co")
    return headers


# ── the happy path ──────────────────────────────────────────────────────────

def test_the_first_post_is_written_and_streamed(client, ready, caption_gen):
    r = client.post("/api/onboarding/first-post", headers=ready,
                    json={"platform": "instagram"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _events(r)
    assert events[0]["type"] == "progress"
    assert events[-1]["type"] == "complete"
    post = events[-1]["post"]
    assert post["caption"] == "Your starter is not dead — it is asleep."
    assert post["hook"] == "Flour, water, patience."
    assert post["hashtags"] == ["#sourdough"]
    assert post["platform"] == "instagram"
    assert caption_gen.generate.call_count == 1


def test_the_first_post_topic_is_built_from_the_saved_brand(client, ready, caption_gen):
    """The subject comes from the profile, never from the request. This is what
    makes the endpoint a sample generator rather than an LLM proxy."""
    client.post("/api/onboarding/first-post", headers=ready,
                json={"platform": "instagram"})
    kwargs = caption_gen.generate.call_args.kwargs
    assert "Sourdough baking" in kwargs["topic"]
    assert kwargs["niche"] == "Sourdough baking"
    assert kwargs["target_audience"] == "Home bakers"
    assert kwargs["brand_name"] == "Crumb & Co"


def test_nothing_is_written_to_the_post_table(client, ready, sm):
    """Ephemeral on purpose, like the demo. A zero-slide Instagram post is a
    shape /api/posts/generate explicitly refuses, and inventing one here to make
    onboarding tidy would put it in the Queue, the Calendar and the grid."""
    from models.database import Post

    client.post("/api/onboarding/first-post", headers=ready,
                json={"platform": "instagram"})

    async def _go():
        async with sm() as db:
            return (await db.execute(select(Post))).scalars().all()
    assert asyncio.run(_go()) == []


# ── the allowance ───────────────────────────────────────────────────────────

def test_the_allowance_is_spent(client, ready, sm):
    client.post("/api/onboarding/first-post", headers=ready,
                json={"platform": "instagram"})
    assert _used(sm, "new@example.com") == 1


def test_a_sample_post_beyond_the_allowance_is_refused(client, ready, sm, caption_gen):
    """Onboarding shares the allowance with ordinary generation (UX phase 6.2)
    rather than holding one of its own — otherwise "free posts" would mean one
    number in the composer and a different one during setup."""
    from services.free_generation import FREE_POST_LIMIT

    for _ in range(FREE_POST_LIMIT):
        client.post("/api/onboarding/first-post", headers=ready,
                    json={"platform": "instagram"})
    r = client.post("/api/onboarding/first-post", headers=ready,
                    json={"platform": "instagram"})

    assert r.status_code == 409
    # The refusal cost nothing: no extra call, no extra count.
    assert caption_gen.generate.call_count == FREE_POST_LIMIT
    assert _used(sm, "new@example.com") == FREE_POST_LIMIT


def test_the_allowance_is_spent_before_the_model_is_called(client, ready, sm, caption_gen):
    """The ordering the whole design rests on. If the counter moved after a
    successful generation, a crash mid-call — or two clicks inside one slow
    round-trip — would buy a second post on our key."""
    seen = {}

    async def _spy(**kwargs):
        # A FRESH session, awaited rather than asyncio.run: we are inside the
        # request's own loop here, and the point is to see what a different
        # connection would see — i.e. that the increment was committed.
        async with sm() as db:
            seen["used_at_call_time"] = (await db.execute(
                select(User.free_generations_used)
                .where(User.email == "new@example.com"))).scalar_one()
        return _caption()

    caption_gen.generate.side_effect = _spy
    client.post("/api/onboarding/first-post", headers=ready, json={"platform": "instagram"})
    assert seen["used_at_call_time"] == 1


def test_a_model_that_answers_nothing_gives_the_allowance_back(client, ready, sm, caption_gen):
    """The one case that deserves a refund: we called the provider and got
    silence, so the account paid for nothing."""
    caption_gen.generate.side_effect = RuntimeError("provider exploded")

    r = client.post("/api/onboarding/first-post", headers=ready, json={"platform": "instagram"})
    assert _events(r)[-1]["type"] == "error"
    assert _used(sm, "new@example.com") == 0


# ── the guards, in the order they must fire ────────────────────────────────

def test_an_anonymous_caller_is_refused(client):
    """Not the public demo: there is an account to charge the cap to, and
    without one the cap has nothing to hang on."""
    r = client.post("/api/onboarding/first-post", json={"platform": "instagram"})
    assert r.status_code == 401


def test_without_an_app_key_it_says_so_instead_of_failing(client, ready, sm):
    """503, not 500 — and this is the state the e2e server is permanently in, so
    it is the ordinary case rather than an exotic one."""
    app.dependency_overrides[get_demo_text_provider] = lambda: None
    r = client.post("/api/onboarding/first-post", headers=ready,
                    json={"platform": "instagram"})
    assert r.status_code == 503
    assert _used(sm, "new@example.com") == 0        # checked before the spend


def test_the_request_cannot_choose_what_gets_written(client, ready, caption_gen):
    """An unknown field is refused rather than ignored. Ignoring it is how a
    sample generator quietly becomes a free LLM: the day somebody adds a
    `topic` parameter, `extra="ignore"` would have accepted it all along."""
    r = client.post("/api/onboarding/first-post", headers=ready,
                    json={"platform": "instagram", "topic": "write my homework"})
    assert r.status_code == 422
    assert caption_gen.generate.call_count == 0


def test_an_unknown_network_is_refused(client, ready):
    r = client.post("/api/onboarding/first-post", headers=ready,
                    json={"platform": "tiktok"})
    assert r.status_code == 422


def test_a_brand_with_no_niche_does_not_spend_the_allowance(client, sm, caption_gen):
    """Checked BEFORE the reservation: a request that cannot produce anything
    useful must not cost the account its one free post."""
    headers = _register(client, "bare@example.com")

    r = client.post("/api/onboarding/first-post", headers=headers,
                    json={"platform": "instagram"})
    assert r.status_code == 422
    assert caption_gen.generate.call_count == 0
    assert _used(sm, "bare@example.com") == 0


def test_a_brand_name_alone_is_enough(client, sm):
    """Screen 2 can end with a name and no niche when the site was readable but
    the guess was not — refusing that would strand exactly the people the
    website screen was built for."""
    headers = _register(client, "named@example.com")
    _set_brand(sm, "named@example.com", brand_name="Crumb & Co")

    r = client.post("/api/onboarding/first-post", headers=headers,
                    json={"platform": "instagram"})
    assert r.status_code == 200, r.text
    assert _events(r)[-1]["type"] == "complete"


# ── whose money it was ──────────────────────────────────────────────────────

def test_the_apps_own_spend_is_not_billed_to_the_user(client, ready, caption_gen):
    """We told this person they pay the vendor directly. `record_usage` stamps
    whatever `current_user_id` holds, and the auth dependency sets it to the
    caller — so without clearing it, our key's spend lands on their usage
    dashboard. That would be a lie in the UI about whose money it was."""
    from services.openrouter import current_user_id

    seen = {}

    async def _spy(**kwargs):
        seen["billed_to"] = current_user_id.get()
        return _caption()

    caption_gen.generate.side_effect = _spy
    r = client.post("/api/onboarding/first-post", headers=ready,
                    json={"platform": "instagram"})

    assert _events(r)[-1]["type"] == "complete"
    assert seen["billed_to"] is None
