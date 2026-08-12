"""Whose credentials the platform spends, and on which vendor.

Three things run on the PLATFORM's own key rather than a tenant's: the landing
demo an anonymous visitor sees, the post written at the end of onboarding, and
the five free generations a new account gets. Everything else spends the
tenant's key.

Two of those three already resolve the vendor from configuration —
`generation_credits` asks `resolve_ai_choice(None, base, …)`, which reads
`default_text_provider` and finds the key through `key_field_for()`. The landing
demo did not: it named OpenRouter in the code and read `openrouter_api_key`
directly. So the owner could point the platform at any vendor and the landing
would keep calling one particular one, with a key field nothing else was
filling — a 503 on the first screen of the product, for a deployment that looked
correctly configured everywhere else.
"""
import pytest

from api.deps import get_demo_text_provider
from config import Settings
from services.ai.catalog import PROVIDERS


def _settings(**kw) -> Settings:
    return Settings(app_mode="cloud", **kw)


def test_the_landing_demo_follows_the_configured_platform_vendor():
    """Point the platform at OpenAI and the landing demo calls OpenAI."""
    provider = get_demo_text_provider(_settings(
        default_text_provider="openai",
        default_text_model="gpt-5.6-luna",
        openai_api_key="sk-test",
        openrouter_api_key="",
    ))
    assert provider is not None
    assert "openai" in type(provider).__name__.lower()


def test_no_platform_key_means_no_provider_and_a_clean_503():
    """The route turns None into "temporarily unavailable" rather than failing
    somewhere inside the generation."""
    assert get_demo_text_provider(_settings(
        default_text_provider="openai",
        default_text_model="gpt-5.6-luna",
        openai_api_key="",
    )) is None


def test_openrouter_still_works_when_that_is_what_is_configured():
    """The default deployment is unchanged — this widens the choice, it does not
    move it."""
    provider = get_demo_text_provider(_settings(
        default_text_provider="openrouter",
        openrouter_api_key="sk-or-test",
        openai_api_key="",
    ))
    assert provider is not None
    assert "openrouter" in type(provider).__name__.lower()


# ── the catalog has to know the models before anyone can be billed for them ──

#: Published prices, USD per 1M tokens, short context, standard tier.
#: Read off platform.openai.com/docs/pricing on 2026-08-12 rather than recalled:
#: these numbers drive both the cost shown in the app and the daily spend cap,
#: and a guessed price makes the cap protect the wrong amount of money.
GPT_5_6_PRICES = {
    "gpt-5.6-sol": (5.00, 30.00),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-luna": (0.20, 1.20),
}


@pytest.mark.parametrize("model_id,prices", GPT_5_6_PRICES.items())
def test_the_gpt_5_6_family_is_priced(model_id, prices):
    by_id = {m["id"]: m for m in PROVIDERS["openai"]["text_models"]}
    assert model_id in by_id, f"{model_id} is missing from the OpenAI catalog"
    assert (by_id[model_id]["price_in"], by_id[model_id]["price_out"]) == prices


def test_the_cheap_one_is_actually_the_cheap_one():
    """Luna is what a giveaway should run on — 25x cheaper on output than Sol.
    Asserted rather than assumed, because the whole reason to add the family is
    to be able to point the free tier at the cheap member of it."""
    by_id = {m["id"]: m for m in PROVIDERS["openai"]["text_models"]}
    assert by_id["gpt-5.6-luna"]["price_out"] < by_id["gpt-5.6-terra"]["price_out"]
    assert by_id["gpt-5.6-terra"]["price_out"] < by_id["gpt-5.6-sol"]["price_out"]
