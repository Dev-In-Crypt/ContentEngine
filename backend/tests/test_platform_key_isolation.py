"""A cloud account never generates on the platform's key by accident.

The product's one promise about money is that people pay the model vendor
directly. `build_settings_for_user` used to keep that promise only for accounts
that had already stored a key: it overlaid the user's credentials on top of the
platform `.env` and left the platform value wherever the user had none. Nothing
downstream could tell the two apart — `resolve_ai_choice` reads the merged
object and sees one string.

That made the platform key reachable in three moves, none of which need a key:

    POST /api/auth/register
    PUT  /api/settings/ai   {"text_provider": "openrouter", "text_model": "…"}
    POST /api/posts/generate

and `record_usage` would file our spend under the caller's name. The only thing
standing in the way was `guardGenerateKeys()` in the browser, which reads the
account's OWN credentials and is therefore correct — and irrelevant to curl.

So the fifteen credential fields are now the user's own value or empty, and the
existing "choose a model in Account" refusals do the rest. The tests below fix
the posture rather than any single route: the leak was one function, and so is
the guard.

The desktop owner is deliberately untouched — `is_local` keeps the whole `.env`,
because there the platform and the user are the same person.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import services.user_settings as user_settings
from api.deps import get_db, get_settings
from config import Settings
from main import app
from models.database import Base, Post, User
from services.user_settings import (
    _CRED_FIELDS, build_settings_for_user, settings_for_post_owner,
)

#: A platform that has paid for everything — the state prod enters the moment an
#: app key is configured for onboarding (UX phase 5). Every value here is one a
#: cloud tenant must never be handed silently.
PLATFORM = dict(
    app_mode="cloud",
    openrouter_api_key="platform-openrouter",
    openai_api_key="platform-openai",
    anthropic_api_key="platform-anthropic",
    google_api_key="platform-google",
    instagram_access_token="platform-ig-token",
    instagram_user_id="platform-ig-user",
    imgbb_api_key="platform-imgbb",
    x_api_key="platform-x-key",
    x_api_secret="platform-x-secret",
    x_access_token="platform-x-token",
    x_access_token_secret="platform-x-token-secret",
    unsplash_access_key="platform-unsplash",
    pexels_api_key="platform-pexels",
    elevenlabs_api_key="platform-elevenlabs",
    kling_api_key="platform-kling",
)


@pytest.fixture
def platform_settings(monkeypatch) -> Settings:
    """Patch the module-level getter, not the FastAPI dependency.

    `build_settings_for_user` calls `config.get_settings()` itself — it is used
    by the scheduler and publisher_flow, which have no request to hang a
    dependency override on. Overriding only the dependency would leave this test
    reading the real .env and passing for the wrong reason.
    """
    settings = Settings(**PLATFORM)
    monkeypatch.setattr(user_settings, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'iso.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


@pytest.fixture
def client(sm, platform_settings):
    async def override_db():
        async with sm() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: platform_settings
    app.state.sessionmaker = sm
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_settings, None)


def _register(client, email="tenant@example.com") -> dict:
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _effective(sm, email: str) -> Settings:
    async def _go():
        async with sm() as db:
            user = (await db.execute(
                select(User).where(User.email == email))).scalar_one()
            return await build_settings_for_user(db, user)
    return asyncio.run(_go())


# ── the posture ─────────────────────────────────────────────────────────────

def test_a_cloud_account_sees_none_of_the_platform_keys(client, sm):
    """All fifteen at once, deliberately.

    Naming one field would let the next credential added to _CRED_FIELDS arrive
    with the old leak intact — which is how instagram_access_token would come
    back, and publishing on the platform's token means posting into OUR account
    on somebody else's behalf.
    """
    _register(client)
    effective = _effective(sm, "tenant@example.com")

    leaked = {field: getattr(effective, field) for field in _CRED_FIELDS
              if getattr(effective, field)}
    assert leaked == {}


def test_storing_one_key_does_not_unlock_the_others(client, sm):
    """The ordinary state of a real account: one key set, fourteen not. The one
    they paid for arrives; the rest stay empty rather than falling back."""
    hdr = _register(client)
    client.put("/api/settings/credentials",
               json={"openrouter_api_key": "tenant-own-key"}, headers=hdr)

    effective = _effective(sm, "tenant@example.com")
    assert effective.openrouter_api_key == "tenant-own-key"
    assert effective.kling_api_key == ""
    assert effective.pexels_api_key == ""
    assert effective.instagram_access_token == ""


def test_the_desktop_owner_still_gets_the_whole_env(sm, platform_settings):
    """Not a leak: on the desktop the platform and the user are one person, and
    the offline app is configured entirely through .env."""
    async def _go():
        async with sm() as db:
            local = User(email="local@localhost", is_local=True)
            db.add(local)
            await db.commit()
            return await build_settings_for_user(db, local)
    effective = asyncio.run(_go())

    assert effective.openrouter_api_key == "platform-openrouter"
    assert effective.kling_api_key == "platform-kling"


# ── the route that made it reachable ────────────────────────────────────────

# The exploit this file was written for — register, name a provider and a model
# (neither needs a key), generate on ours — is no longer refused outright, and
# that is deliberate: UX phase 6.2 gives an account with NO key of its own a few
# generations on the application's key, so that "paste an API key" arrives after
# somebody has seen the product work. What used to be an open tap is now metered.
#
# The bounds live where the harness for them is, in test_generation_on_our_key.py:
# `test_our_key_runs_our_models` (their model choice never becomes our bill) and
# `test_the_allowance_runs_out_and_asks_for_a_key` (the door closes again). The
# posture asserted HERE is the one that has no allowance and never will: their
# own credentials are theirs, and the platform's are the platform's.


def test_every_route_that_writes_copy_refuses_the_same_way(client, sm):
    """Three routes call a text model, and all three used to reach it through a
    provider object that may be None. `generate` crashed inside the generator;
    `regenerate-field` wrapped the same AttributeError in a 502 reading
    "Regeneration failed: 'NoneType' object has no attribute 'generate_text'".

    One test for all of them: it fails if the guard is dropped from any single
    call site, which a per-route test would not.
    """
    hdr = _register(client)
    # A key for one vendor, a model chosen from another: a realistic half-configured
    # state, and the one that still reaches this guard now that an account with
    # no key at all has an allowance instead. They hold a key, so they are on
    # their own path — and the provider they named has no client to build.
    client.put("/api/settings/credentials",
               json={"anthropic_api_key": "their-anthropic-key"}, headers=hdr)
    client.put("/api/settings/ai",
               json={"text_provider": "openrouter",
                     "text_model": "anthropic/claude-sonnet-4"}, headers=hdr)

    async def _post() -> str:
        async with sm() as db:
            user = (await db.execute(
                select(User).where(User.email == "tenant@example.com"))).scalar_one()
            post = Post(user_id=user.id, topic="Sourdough starters", caption="A loaf.",
                        format="single", platform="instagram", status="preview")
            db.add(post)
            await db.commit()
            return post.id
    post_id = asyncio.run(_post())

    for label, response in (
        ("generate", client.post("/api/posts/generate",
                                 json={"topic": "AI trends in 2026", "format": "single"},
                                 headers=hdr)),
        ("regenerate-field", client.post(f"/api/posts/{post_id}/regenerate-field",
                                         json={"field": "hook"}, headers=hdr)),
        ("adapt", client.post(f"/api/posts/{post_id}/adapt/x", headers=hdr)),
    ):
        assert response.status_code == 400, f"{label}: {response.status_code} {response.text}"
        assert "key" in response.json()["detail"].lower(), label


def test_a_video_cannot_be_generated_on_the_platforms_kling_key(client):
    """The most expensive call in the product — about a dollar a clip — and the
    one where a silent fallback would be felt first."""
    hdr = _register(client)
    r = client.post("/api/media/videos",
                    json={"prompt": "A loaf of sourdough cooling on a rack"},
                    headers=hdr)
    assert r.status_code == 400
    assert "kling" in r.json()["detail"].lower()


# ── the request-less path: publishing and the scheduler ─────────────────────

def test_publishing_a_post_never_falls_back_to_our_instagram_token(client, sm):
    """`settings_for_post_owner` is what the scheduler publishes with, hours
    after the request that created the post is gone. Falling back here would
    publish a stranger's post into the platform's own Instagram account."""
    _register(client)

    async def _go():
        async with sm() as db:
            user = (await db.execute(
                select(User).where(User.email == "tenant@example.com"))).scalar_one()
            post = Post(user_id=user.id, topic="Sourdough", caption="…",
                        format="single", platform="instagram", status="scheduled")
            db.add(post)
            await db.commit()
            return await settings_for_post_owner(db, post)
    effective = asyncio.run(_go())

    assert effective.instagram_access_token == ""
    assert effective.imgbb_api_key == ""
