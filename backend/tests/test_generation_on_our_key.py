"""POST /api/posts/generate when the account has no key of its own.

The service that decides who pays is tested in test_generation_credits.py. This
file is about the wiring: that the route acts on the decision rather than merely
making it — a different engine, our models, our bill, and the allowance handed
back when nothing came of it.

Everything the real generation would reach over the network is replaced, so what
is asserted here is what the route DID, not what a provider returned.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import api.routes.posts as posts_routes
import services.openrouter as openrouter
from api.deps import get_content_engine, get_db, get_settings
from config import Settings
from main import app
from models.database import Base, LLMUsage, User
from models.schemas import ImageSource, PostFormat
from services.content_engine import GeneratedPost, GeneratedSlide
from services.free_generation import FREE_POST_LIMIT
from services.user_settings import _CRED_FIELDS
from tests.test_posts_crud_api import _cleanup_post_dir, _jpeg, _sse_events

OUR_TEXT_MODEL = "our/cheap-text-model"
OUR_IMAGE_MODEL = "our/cheap-image-model"


def _platform(**overrides) -> Settings:
    """Every credential explicit: Settings() reads the developer's real .env, and
    a key in there would put these tests on the own-key path — passing, and
    proving nothing about the path they are named after."""
    blank = {field: "" for field in _CRED_FIELDS}
    return Settings(app_mode="cloud", api_token="",
                    default_text_provider="openrouter",
                    default_image_provider="openrouter",
                    default_text_model=OUR_TEXT_MODEL,
                    default_image_model=OUR_IMAGE_MODEL,
                    **{**blank, "openrouter_api_key": "app-key", **overrides})


def _generated(post_id: str) -> GeneratedPost:
    return GeneratedPost(
        id=post_id, topic="Sourdough", format=PostFormat.SINGLE,
        caption="A loaf.", hashtags=["#bread"], cta="Follow", hook="Flour.",
        alt_text="A loaf", seo_keywords=["bread"],
        slides=[GeneratedSlide(slide_number=1, image_bytes=_jpeg("blue"),
                               image_source=ImageSource.STOCK, search_query="bread")],
        text_model_used=OUR_TEXT_MODEL, image_model_used=OUR_IMAGE_MODEL,
    )


class _FakeEngine:
    """Records how it was called, and pretends a provider answered.

    `record_usage` is called from inside generate_post on purpose: that is where
    a real provider would file the cost, so whatever `current_user_id` holds at
    that moment is what the ledger will say — which is the thing being tested.
    """

    def __init__(self, cost: float = 0.02, boom: bool = False):
        self.calls: list[dict] = []
        self.cost = cost
        self.boom = boom
        self.brand_engine = None
        self.caption_gen = type("_C", (), {"text_provider": object()})()

    async def generate_post(self, **kwargs):
        self.calls.append(kwargs)
        openrouter.record_usage(kwargs.get("text_model") or "?",
                                {"total_tokens": 10, "cost": self.cost})
        if self.boom:
            raise RuntimeError("the provider fell over")
        return _generated(str(uuid.uuid4()))


@pytest.fixture(autouse=True)
def empty_buffer():
    openrouter.drain_usage()
    yield
    openrouter.drain_usage()


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ours.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


@pytest.fixture
def env(sm, monkeypatch):
    """A cloud client, plus the two engines the route can choose between.

    `built` captures what `build_content_engine` was asked for — the free path
    builds its own rather than taking the injected one, and which settings went
    into it is the whole question.
    """
    settings = _platform()
    built: list[dict] = []
    ours, theirs = _FakeEngine(), _FakeEngine()

    def fake_build(engine_settings, user, *, actor, brand_engine=None):
        built.append({"settings": engine_settings, "actor": actor, "user": user})
        return ours

    monkeypatch.setattr(posts_routes, "build_content_engine", fake_build)

    async def override_db():
        async with sm() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_content_engine] = lambda: theirs
    app.state.sessionmaker = sm

    client = TestClient(app)
    client.ours, client.theirs, client.built = ours, theirs, built
    #: Post ids that reached the database, so their slide JPEGs can be swept.
    #: A generated post writes real files under uploads/, and a suite that
    #: leaves them behind slowly fills the repo with other tests' bread.
    client.created = []
    yield client

    for post_id in client.created:
        _cleanup_post_dir(post_id)
    for dep in (get_db, get_settings, get_content_engine):
        app.dependency_overrides.pop(dep, None)


def _register(client, email="nokey@example.com") -> dict:
    r = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _generate(client, headers) -> list[dict]:
    res = client.post("/api/posts/generate",
                      json={"topic": "Sourdough starters", "format": "single"},
                      headers=headers)
    assert res.status_code == 200, res.text
    events = _sse_events(res)
    if events and events[-1]["type"] == "complete":
        client.created.append(events[-1]["post"]["id"])
    return events


def _used(sm, email: str) -> int:
    async def _go():
        async with sm() as db:
            return (await db.execute(select(User.free_generations_used)
                                     .where(User.email == email))).scalar_one()
    return asyncio.run(_go())


# ── the free path ───────────────────────────────────────────────────────────

def test_a_brand_new_account_can_generate_without_a_key(env, sm):
    """The point of the phase: the question "paste an API key" now arrives after
    somebody has seen the product work, not on the doorstep."""
    headers = _register(env)
    events = _generate(env, headers)

    assert events[-1]["type"] == "complete"
    assert _used(sm, "nokey@example.com") == 1


def test_the_free_path_builds_its_engine_from_our_credentials(env):
    """Not the injected one. The engine a dependency can hand back is always the
    caller's, and the caller is precisely who has no key here."""
    headers = _register(env)
    _generate(env, headers)

    assert len(env.built) == 1
    assert env.built[0]["settings"].openrouter_api_key == "app-key"
    assert env.built[0]["actor"] is None       # our model choice, not theirs
    assert env.ours.calls and not env.theirs.calls


def test_our_key_writes_with_our_models(env):
    headers = _register(env)
    env.post("/api/settings/ai",
             json={"text_provider": "openrouter"}, headers=headers)
    _generate(env, headers)

    assert env.ours.calls[0]["text_model"] == OUR_TEXT_MODEL
    assert env.ours.calls[0]["image_model"] == OUR_IMAGE_MODEL


def test_a_free_generation_does_not_buy_web_search(env):
    """Grounding is a surcharge per call and only OpenRouter has it — which is
    exactly what our key is. A trial post is not worth buying search for."""
    headers = _register(env)
    _generate(env, headers)

    assert env.ours.calls[0]["web_grounded"] is False


def test_our_spend_is_not_billed_to_the_account(env, sm):
    """The one thing we promised: you pay the vendor directly. Filing our own
    call under their name would contradict it on the very screen that shows
    them their spend — and the ceiling in app_spend counts exactly the rows
    this leaves behind."""
    headers = _register(env)
    _generate(env, headers)

    async def _rows():
        async with sm() as db:
            return (await db.execute(select(LLMUsage))).scalars().all()
    rows = asyncio.run(_rows())

    assert rows and all(r.user_id is None for r in rows)


def test_a_failed_generation_gives_the_allowance_back(env, sm, monkeypatch):
    """It bought nothing: no post reached the user. Refunding is the cheap
    direction to be wrong in — the expensive one is charging for silence."""
    headers = _register(env)
    env.ours.boom = True

    events = _generate(env, headers)

    assert events[-1]["type"] == "error"
    assert _used(sm, "nokey@example.com") == 0


def test_the_allowance_runs_out_and_asks_for_a_key(env, sm):
    headers = _register(env)
    for _ in range(FREE_POST_LIMIT):
        _generate(env, headers)

    refused = env.post("/api/posts/generate",
                       json={"topic": "One more please", "format": "single"},
                       headers=headers)

    assert refused.status_code == 409
    assert "key" in refused.json()["detail"].lower()
    assert len(env.ours.calls) == FREE_POST_LIMIT      # the refusal cost nothing


# ── their own key ───────────────────────────────────────────────────────────

def test_an_account_with_a_key_is_untouched_by_any_of_this(env, sm):
    """The ordinary path must not have moved: their engine, their bill, no
    counter, and web search still on because they are paying for it."""
    headers = _register(env)
    env.put("/api/settings/credentials",
            json={"openrouter_api_key": "their-own-key"}, headers=headers)
    env.put("/api/settings/ai",
            json={"text_provider": "openrouter", "text_model": "their/model"},
            headers=headers)

    _generate(env, headers)

    assert env.theirs.calls and not env.ours.calls
    assert env.theirs.calls[0]["text_model"] == "their/model"
    assert env.theirs.calls[0]["web_grounded"] is True
    assert _used(sm, "nokey@example.com") == 0
    assert env.built == []


# ── what /api/usage carries ─────────────────────────────────────────────────

def test_the_usage_endpoint_carries_what_is_left(env, sm):
    """The header already polls this on a timer. A second endpoint for one
    integer would be a second thing to keep in step with the first."""
    headers = _register(env)

    before = env.get("/api/usage", headers=headers).json()
    assert before["free"] == {"remaining": FREE_POST_LIMIT, "limit": FREE_POST_LIMIT}

    _generate(env, headers)

    after = env.get("/api/usage", headers=headers).json()
    assert after["free"]["remaining"] == FREE_POST_LIMIT - 1


def test_an_account_with_a_key_is_told_nothing_about_an_allowance(env):
    headers = _register(env)
    env.put("/api/settings/credentials",
            json={"openrouter_api_key": "their-own-key"}, headers=headers)

    assert env.get("/api/usage", headers=headers).json()["free"] is None


# ── a picture the platform can actually make ────────────────────────────────

def test_a_free_generation_without_a_stock_key_still_makes_a_picture(env):
    """The composer's default source is stock, and the platform here has no
    Unsplash or Pexels key — which is the ordinary state of a deployment that
    has configured only an application AI key. Before this, every free
    generation ended in "Generation failed" and a refund."""
    headers = _register(env)
    _generate(env, headers)

    assert env.ours.calls[0]["default_image_source"] == ImageSource.AI_GEN


def test_a_configured_stock_key_is_left_alone(env):
    """Stock is cheaper than generating, so a platform that has paid for it
    keeps using it. Without this the guard above would read "always generate",
    which spends more of our money than it has to."""
    app.dependency_overrides[get_settings] = lambda: _platform(pexels_api_key="stock-key")
    headers = _register(env, email="stocked@example.com")
    _generate(env, headers)

    assert env.ours.calls[0]["default_image_source"] == ImageSource.STOCK


def test_an_account_on_its_own_key_keeps_the_source_it_chose(env):
    """Their money, their choice, and a stock failure they can see and fix. The
    substitution is ours to make only when the bill is ours."""
    headers = _register(env, email="paying@example.com")
    env.put("/api/settings/credentials",
            json={"openrouter_api_key": "their-own-key"}, headers=headers)
    env.put("/api/settings/ai",
            json={"text_provider": "openrouter", "text_model": "their/model"},
            headers=headers)

    _generate(env, headers)

    assert env.theirs.calls[0]["default_image_source"] == ImageSource.STOCK
