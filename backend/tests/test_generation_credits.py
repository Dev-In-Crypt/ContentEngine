"""Who pays for a generation, and what happens when the answer is "we do".

This is the only place in the product where somebody who has given us nothing
can spend our money, so the tests are mostly about refusing — and about the
ORDER of the refusals, which carries as much weight as the refusals themselves:

  * anything that will be refused is refused before it costs anything;
  * the allowance is claimed before the model is called, never after;
  * a refusal that is about us (our daily ceiling) must not consume something
    that belongs to them (their remaining free post).

The other half is what the free path is allowed to do with our key: our models,
never theirs, and never a mixture where their cheap text key buys our images.
"""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import services.openrouter as openrouter
from config import Settings
from models.database import Base, User
from services import app_spend, free_generation
from services.user_settings import _CRED_FIELDS
from models.schemas import ImageSource
from services.generation_credits import (
    claim_generation_credentials, free_allowance, image_source_for,
)

def _settings(**overrides) -> Settings:
    """Settings with every credential explicitly blank, then the overrides.

    `Settings()` reads backend/.env, and a developer machine has real keys in
    it — so a constant named NO_KEYS quietly carried the author's own OpenRouter
    key, every free-path test took the own-key branch, and they all passed for
    the wrong reason. They did, until this function existed.
    """
    blank = {field: "" for field in _CRED_FIELDS}
    return Settings(app_mode="cloud", **{**blank, **overrides})


#: The platform, configured: our key, our models. Nothing here reaches a cloud
#: tenant on its own since UX phase 6.0 — it is reachable only by spending an
#: allowance, which is what this module decides.
BASE = _settings(
    openrouter_api_key="app-key",
    default_text_provider="openrouter",
    default_text_model="our/cheap-text-model",
    default_image_provider="openrouter",
    default_image_model="our/cheap-image-model",
    app_daily_spend_usd=10.0,
)

#: A tenant who has pasted nothing: since 6.0 every credential is empty.
NO_KEYS = _settings()

#: A tenant with their own key and their own choice of model.
OWN = _settings(openrouter_api_key="their-key")


@pytest.fixture(autouse=True)
def empty_buffer():
    openrouter.drain_usage()
    yield
    openrouter.drain_usage()


@pytest.fixture
def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'credits.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield async_sessionmaker(eng, expire_on_commit=False)
    asyncio.run(eng.dispose())


def _user(sm, **fields) -> str:
    async def _go():
        async with sm() as db:
            user = User(email="t@example.com", **fields)
            db.add(user)
            await db.commit()
            return user.id
    return asyncio.run(_go())


def _claim(sm, user_id: str, *, effective=NO_KEYS, base=BASE, **kw):
    async def _go():
        async with sm() as db:
            user = await db.get(User, user_id)
            return await claim_generation_credentials(
                db, user, effective=effective, base=base, **kw)
    return asyncio.run(_go())


def _used(sm, user_id: str) -> int:
    async def _go():
        async with sm() as db:
            return (await db.execute(
                select(User.free_generations_used).where(User.id == user_id))).scalar_one()
    return asyncio.run(_go())


# ── their key ───────────────────────────────────────────────────────────────

def test_an_account_with_its_own_key_pays_for_itself(sm):
    uid = _user(sm, text_provider="openrouter", text_model="their/model")
    creds = _claim(sm, uid, effective=OWN)

    assert creds.on_our_key is False
    assert creds.text_model == "their/model"
    assert creds.settings is OWN
    assert _used(sm, uid) == 0        # no allowance touched, nothing to refund


def test_any_of_the_four_text_keys_counts_as_having_one(sm):
    """OpenRouter is the one the product recommends and the one we ourselves
    use, which makes it the easy thing to check for — and checking only it would
    hand free generations to every account paying Anthropic directly."""
    uid = _user(sm, text_provider="anthropic", text_model="claude-sonnet-5")
    creds = _claim(sm, uid, effective=_settings(anthropic_api_key="their-key"))

    assert creds.on_our_key is False
    assert _used(sm, uid) == 0


def test_a_key_with_no_model_chosen_is_still_their_problem(sm):
    """Halfway through setup: a key pasted, no model picked. They get today's
    "choose a provider and model" refusal from the route, because text_model
    comes back empty — not a free generation. Paying for somebody who already
    holds a key would be the wrong way to be generous."""
    uid = _user(sm)
    creds = _claim(sm, uid, effective=OWN)

    assert creds.on_our_key is False
    assert not creds.text_model
    assert _used(sm, uid) == 0


def test_the_desktop_owner_is_not_a_tenant(sm):
    """On the desktop the platform and the user are the same person, and the
    keys come from .env. An allowance there would be an allowance against
    yourself."""
    uid = _user(sm, is_local=True)
    creds = _claim(sm, uid, effective=BASE)

    assert creds.on_our_key is False
    assert _used(sm, uid) == 0


# ── our key ─────────────────────────────────────────────────────────────────

def test_an_account_with_no_key_gets_one_of_ours(sm):
    uid = _user(sm)
    creds = _claim(sm, uid)

    assert creds.on_our_key is True
    assert creds.settings is BASE
    assert creds.actor is None            # the platform's choice, not theirs
    assert _used(sm, uid) == 1            # claimed before anything was called


def test_our_key_runs_our_models(sm):
    """Choosing an expensive model in Settings must not be a decision about our
    spending, made by somebody who does not pay the bill."""
    uid = _user(sm, text_provider="openrouter", text_model="anthropic/expensive",
                image_provider="openrouter", image_model="also/expensive")
    creds = _claim(sm, uid)

    assert creds.text_model == "our/cheap-text-model"
    assert creds.image_model == "our/cheap-image-model"


def test_the_request_cannot_pick_the_model_we_pay_for(sm):
    """Same rule one level lower: the composer sends a per-post override, and on
    our key it is ignored rather than honoured."""
    uid = _user(sm)
    creds = _claim(sm, uid, text_model_override="anthropic/expensive",
                   image_model_override="also/expensive")

    assert creds.text_model == "our/cheap-text-model"
    assert creds.image_model == "our/cheap-image-model"


def test_the_override_is_still_honoured_on_their_own_key(sm):
    """The other half: it is their money, so a per-post model is theirs to pick.
    Without this the guard above could be "ignore overrides", which would break
    a feature instead of protecting a bill."""
    uid = _user(sm, text_provider="openrouter", text_model="their/model")
    creds = _claim(sm, uid, effective=OWN, text_model_override="their/other-model")

    assert creds.text_model == "their/other-model"


# ── refusals, in order ──────────────────────────────────────────────────────

def test_no_key_anywhere_reads_as_it_always_did(sm):
    """Nothing to spend on either side. From where the user sits nothing about
    UX phase 6 happened, so the words do not change."""
    uid = _user(sm)
    with pytest.raises(HTTPException) as refusal:
        _claim(sm, uid, base=NO_KEYS)

    assert refusal.value.status_code == 400
    assert "model" in refusal.value.detail.lower()
    assert _used(sm, uid) == 0


def test_a_spent_allowance_asks_for_a_key(sm):
    uid = _user(sm, free_generations_used=free_generation.FREE_POST_LIMIT)
    with pytest.raises(HTTPException) as refusal:
        _claim(sm, uid)

    assert refusal.value.status_code == 409
    assert "key" in refusal.value.detail.lower()
    assert _used(sm, uid) == free_generation.FREE_POST_LIMIT   # not pushed past it


def test_a_capped_day_refuses_without_spending_their_allowance(sm):
    """The ceiling is about us. Charging them a free post for our bad day would
    take something they can never get back, for a reason that is not theirs."""
    uid = _user(sm)
    asyncio.run(_spend(sm, 10.0))

    with pytest.raises(HTTPException) as refusal:
        _claim(sm, uid)

    assert refusal.value.status_code == 503
    assert _used(sm, uid) == 0


def test_the_ceiling_sees_spend_that_is_still_buffered(sm):
    """The whole reason 6.1 exists, asserted from the caller that depends on it:
    a ceiling reading only committed rows would let a burst through while the
    money sat in a list in memory."""
    uid = _user(sm)
    openrouter.record_usage("m", {"total_tokens": 1, "cost": 11.0})

    with pytest.raises(HTTPException) as refusal:
        _claim(sm, uid)
    assert refusal.value.status_code == 503


def test_their_own_allowance_is_checked_before_our_ceiling(sm):
    """Both are exhausted. They should be told the thing they can act on — add
    a key — rather than "come back tomorrow", which would be true today and
    still true tomorrow."""
    uid = _user(sm, free_generations_used=free_generation.FREE_POST_LIMIT)
    asyncio.run(_spend(sm, 10.0))

    with pytest.raises(HTTPException) as refusal:
        _claim(sm, uid)
    assert refusal.value.status_code == 409


def test_a_capped_day_does_not_stop_somebody_paying_their_own_way(sm):
    """Our ceiling is our problem. An account with a key must not be caught by
    it — nothing they do costs us anything."""
    uid = _user(sm, text_provider="openrouter", text_model="their/model")
    asyncio.run(_spend(sm, 99.0))

    creds = _claim(sm, uid, effective=OWN)
    assert creds.on_our_key is False


async def _spend(sm, usd: float) -> None:
    """Money already on our bill today, written the way the app writes it."""
    openrouter.record_usage("m", {"total_tokens": 1, "cost": usd})
    async with sm() as db:
        await app_spend.flush_usage(db)


# ── what the interface is told ──────────────────────────────────────────────
#
# The counter beside the Generate button and the wall in front of it read the
# same function, on purpose. Two readings of "does this account have a key"
# would eventually disagree, and both ways of disagreeing are bad: a count next
# to a button the server refuses, or a wall in front of somebody it would serve.


def test_an_account_on_the_free_tier_is_told_what_is_left(sm):
    uid = _user(sm)
    assert free_allowance(_get(sm, uid), NO_KEYS, BASE) == {
        "remaining": free_generation.FREE_POST_LIMIT,
        "limit": free_generation.FREE_POST_LIMIT,
    }

    _claim(sm, uid)
    assert free_allowance(_get(sm, uid), NO_KEYS, BASE)["remaining"] == \
        free_generation.FREE_POST_LIMIT - 1


def test_nothing_is_said_to_somebody_paying_their_own_way(sm):
    """None rather than a number: for them it is not a smaller count, it is not
    a subject."""
    uid = _user(sm)
    assert free_allowance(_get(sm, uid), OWN, BASE) is None


def test_nothing_is_said_on_the_desktop(sm):
    uid = _user(sm, is_local=True)
    assert free_allowance(_get(sm, uid), BASE, BASE) is None


def test_nothing_is_said_where_nothing_is_offered(sm):
    """A self-hosted deployment with no application key. "5 free posts left"
    followed by a refusal would be a promise the install cannot keep."""
    uid = _user(sm)
    assert free_allowance(_get(sm, uid), NO_KEYS, NO_KEYS) is None


def test_a_spent_allowance_reads_zero_rather_than_going_negative(sm):
    uid = _user(sm, free_generations_used=free_generation.FREE_POST_LIMIT + 3)
    assert free_allowance(_get(sm, uid), NO_KEYS, BASE)["remaining"] == 0


def _get(sm, user_id: str) -> User:
    async def _go():
        async with sm() as db:
            return await db.get(User, user_id)
    return asyncio.run(_go())


# ── saying what is actually missing ─────────────────────────────────────────

def test_a_chosen_model_with_no_key_is_told_about_the_key(sm):
    """Found on prod right after the phase-6 deploy. The account had named a
    provider and a model, held no key, and the platform holds none either — and
    the refusal said "No text model selected. Choose a provider and model."

    A model IS selected. Sending them to a screen where everything already looks
    right is the least useful thing the sentence could do, and this is the
    permanent state of every self-hosted install, so it is the first refusal a
    new operator reads.
    """
    uid = _user(sm, text_provider="openrouter", text_model="their/model")
    with pytest.raises(HTTPException) as refusal:
        _claim(sm, uid, base=NO_KEYS)

    assert refusal.value.status_code == 400
    assert "key" in refusal.value.detail.lower()
    assert "openrouter" in refusal.value.detail.lower()


def test_an_account_that_has_chosen_nothing_is_told_to_choose(sm):
    """The other half, and the reason the first is not simply "always say key":
    somebody who has picked no provider cannot paste a key for it."""
    uid = _user(sm)
    with pytest.raises(HTTPException) as refusal:
        _claim(sm, uid, base=NO_KEYS)

    assert refusal.value.status_code == 400
    assert "model" in refusal.value.detail.lower()


# ── a picture when there is no stock key ────────────────────────────────────
#
# The composer's default image source is "stock". On the free path the engine is
# built from the PLATFORM's settings, and a platform with no Unsplash or Pexels
# key has a StockClient with neither client in it — so search_and_download finds
# no source, content_engine retries with stock, and the generation fails.
#
# Which means the day an application key is configured, every free generation
# ends in "Generation failed" and a refund. Phase 6 turns itself on and breaks in
# the same motion. The rule below is the smallest honest answer: on OUR
# credentials, a stock request we cannot serve becomes a picture we can make.


def test_a_stock_request_we_cannot_serve_becomes_a_generated_picture(sm):
    uid = _user(sm)
    creds = _claim(sm, uid)

    assert image_source_for(ImageSource.STOCK, BASE, on_our_key=creds.on_our_key) == ImageSource.AI_GEN


def test_a_configured_stock_key_is_still_preferred(sm):
    """Not "always generate": stock is cheaper, and when the platform has paid
    for it the free path should use it."""
    uid = _user(sm)
    with_stock = _settings(openrouter_api_key="app-key", pexels_api_key="stock-key",
                           default_text_provider="openrouter",
                           default_text_model="our/cheap-text-model",
                           default_image_provider="openrouter",
                           default_image_model="our/cheap-image-model")
    creds = _claim(sm, uid, base=with_stock)

    assert image_source_for(ImageSource.STOCK, with_stock, on_our_key=creds.on_our_key) == ImageSource.STOCK


def test_their_own_stock_choice_is_never_overridden(sm):
    """The only place in the product where we substitute what somebody asked
    for, so it is confined to the case where their choice cannot be served AND
    the bill is ours. On their own key a stock failure is theirs to see and fix."""
    uid = _user(sm, text_provider="openrouter", text_model="their/model")
    creds = _claim(sm, uid, effective=OWN)

    assert image_source_for(ImageSource.STOCK, BASE, on_our_key=creds.on_our_key) == ImageSource.STOCK


def test_only_stock_is_ever_substituted(sm):
    """Uploads are somebody's own photograph and AI is already a choice. Quietly
    swapping either would publish something they did not pick."""
    uid = _user(sm)
    creds = _claim(sm, uid)

    for asked in (ImageSource.UPLOAD, ImageSource.AI_GEN, ImageSource.CANVA):
        assert image_source_for(asked, BASE, on_our_key=creds.on_our_key) == asked


def test_nothing_is_substituted_when_we_cannot_generate_either(sm):
    """No stock key and no image model is a deployment that can produce no
    pictures at all. Switching then would trade one failure for another and lose
    the honest error message on the way."""
    uid = _user(sm)
    no_images = _settings(openrouter_api_key="app-key",
                          default_text_provider="openrouter",
                          default_text_model="our/cheap-text-model",
                          default_image_provider="", default_image_model="")
    creds = _claim(sm, uid, base=no_images)

    assert image_source_for(ImageSource.STOCK, no_images, on_our_key=creds.on_our_key) == ImageSource.STOCK


def test_a_burst_hour_pauses_the_free_tier_without_spending_the_allowance(sm):
    """The same rule as the landing, on the signed-in path.

    An hour's share of the budget is gone, the day's is not. The account is
    told to come back shortly rather than tomorrow, and — the part that matters
    — its free posts are still all there: a ceiling of ours must never quietly
    consume something of theirs.
    """
    uid = _user(sm)
    asyncio.run(_spend(sm, 10.0 / app_spend.BURST_HOURS))

    with pytest.raises(HTTPException) as refusal:
        _claim(sm, uid)

    assert refusal.value.status_code == 503
    detail = refusal.value.detail.lower()
    assert "shortly" in detail and "tomorrow" not in detail
    assert _used(sm, uid) == 0


def test_half_an_hour_of_budget_still_lets_them_generate(sm):
    """The other half of the pair, so the ceiling cannot be tightened into a
    permanent closure by accident."""
    uid = _user(sm)
    asyncio.run(_spend(sm, 10.0 / app_spend.BURST_HOURS / 2))

    creds = _claim(sm, uid)
    assert creds.on_our_key
    assert _used(sm, uid) == 1
