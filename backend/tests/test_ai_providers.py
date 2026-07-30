"""Multi-provider AI layer: catalogue, factory and each vendor adapter.

Each adapter speaks a different protocol, so the request shape is pinned per
provider (URL, auth header, payload layout) as well as the response parsing.
"""
import base64
import json

import pytest
from pytest_httpx import HTTPXMock

from services.ai.anthropic_provider import AnthropicProvider
from services.ai.base import AIError, require_model
from services.ai.catalog import (
    IMAGE, PROVIDERS, TEXT, VIDEO_PROVIDERS, estimate_cost, estimate_video_cost,
    is_valid_provider, key_field_for, list_providers, list_video_providers,
    supports_grounding,
)
from services.ai.factory import make_image_provider, make_text_provider
from services.ai.google_provider import GoogleProvider
from services.ai.openai_provider import OpenAIProvider
from services.ai.openrouter_provider import OpenRouterProvider

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


# ── catalogue ────────────────────────────────────────────────────────────────

def test_catalog_entries_are_well_formed():
    for key, meta in PROVIDERS.items():
        assert meta["label"] and meta["key_field"] and meta["key_url"]
        for bucket in ("text_models", "image_models"):
            for m in meta[bucket]:
                assert m["id"] and m["label"]
                assert m["price_in"] >= 0 and m["price_out"] >= 0, f"{key}/{m['id']}"


def test_video_catalog_is_separate_from_the_text_image_providers():
    """Kling has no text_models/image_models and its key is a single bearer
    token priced per second — it must not live inside PROVIDERS, which every
    other test here assumes is the text/image, per-token world."""
    assert "kling" not in PROVIDERS
    assert "kling" in VIDEO_PROVIDERS


def test_video_catalog_entries_are_well_formed():
    for key, meta in VIDEO_PROVIDERS.items():
        assert meta["label"] and meta["key_field"] and meta["key_url"]
        for m in meta["video_models"]:
            assert m["id"] and m["label"]
            assert m["price_per_sec"] >= 0, f"{key}/{m['id']}"


def test_list_video_providers_never_leaks_secrets():
    for p in list_video_providers():
        assert all(not k.endswith("_enc") for k in p)


def test_estimate_video_cost_scales_with_seconds():
    model = VIDEO_PROVIDERS["kling"]["video_models"][0]
    assert estimate_video_cost("kling", model["id"], 10) == round(10 * model["price_per_sec"], 4)
    assert estimate_video_cost("kling", model["id"], 0) == 0.0


def test_estimate_video_cost_unknown_model_or_provider_is_zero():
    assert estimate_video_cost("kling", "not-a-real-model", 10) == 0.0
    assert estimate_video_cost("not-a-real-provider", "kling-v3-0", 10) == 0.0


def test_anthropic_has_no_image_models():
    """Anthropic has no image generation API — the UI must not offer it."""
    assert PROVIDERS["anthropic"]["image_models"] == []
    image_keys = {p["key"] for p in list_providers(IMAGE)}
    assert "anthropic" not in image_keys
    assert "openrouter" in image_keys


def test_list_providers_never_leaks_secrets():
    for p in list_providers(TEXT):
        assert "api_key" not in p and "key" in p and p["models"]


def test_is_valid_provider_and_key_field():
    assert is_valid_provider("openrouter") and is_valid_provider("google", IMAGE)
    assert not is_valid_provider("anthropic", IMAGE)      # text-only
    assert not is_valid_provider("nope") and not is_valid_provider(None)
    assert key_field_for("openai") == "openai_api_key"
    assert key_field_for("nope") is None


def test_only_openrouter_supports_grounding():
    assert supports_grounding("openrouter")
    for other in ("openai", "anthropic", "google", None, "nope"):
        assert not supports_grounding(other)


def test_estimate_cost():
    # anthropic/claude-haiku-4.5 on OpenRouter is $1/M in, $5/M out
    cost = estimate_cost("openrouter", "anthropic/claude-haiku-4.5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(6.0)
    assert estimate_cost("openrouter", "unknown/model", 1000, 1000) == 0.0
    assert estimate_cost("nope", "x", 1000, 1000) == 0.0


def test_require_model_guards_empty():
    assert require_model(" gpt-5 ", "text") == "gpt-5"
    with pytest.raises(AIError, match="No text model selected"):
        require_model("", "text")


# ── factory ──────────────────────────────────────────────────────────────────

def test_factory_builds_each_provider():
    assert isinstance(make_text_provider("openrouter", "k"), OpenRouterProvider)
    assert isinstance(make_text_provider("openai", "k"), OpenAIProvider)
    assert isinstance(make_text_provider("anthropic", "k"), AnthropicProvider)
    assert isinstance(make_text_provider("google", "k"), GoogleProvider)


def test_factory_rejects_unknown_and_imageless():
    with pytest.raises(AIError, match="Unknown AI provider"):
        make_text_provider("nope", "k")
    with pytest.raises(AIError, match="does not generate images"):
        make_image_provider("anthropic", "k")


# ── OpenAI adapter ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openai_text_request_and_parse(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })
    p = OpenAIProvider(api_key="sk-test")
    content, citations = await p.generate_text(
        model="gpt-5-mini", system_prompt="SYS", user_prompt="USR", max_tokens=99)
    assert content == "hello"
    assert citations == []                      # no grounding outside OpenRouter

    req = httpx_mock.get_requests()[0]
    assert str(req.url).endswith("/chat/completions")
    assert req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["model"] == "gpt-5-mini"
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["messages"][1] == {"role": "user", "content": "USR"}
    await p.close()


@pytest.mark.asyncio
async def test_openai_image_decodes_b64(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    p = OpenAIProvider(api_key="sk-test")
    out = await p.generate_image(model="gpt-image-1", prompt="a cat")
    assert out == PNG
    assert str(httpx_mock.get_requests()[0].url).endswith("/images/generations")
    await p.close()


@pytest.mark.asyncio
async def test_openai_http_error_becomes_aierror(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=401, text="bad key")
    p = OpenAIProvider(api_key="nope")
    with pytest.raises(AIError, match="OpenAI failed: 401"):
        await p.generate_text(model="gpt-5", system_prompt="s", user_prompt="u")
    await p.close()


# ── Anthropic adapter ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anthropic_request_shape_and_parse(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={
        "content": [{"type": "text", "text": "hi there"}],
        "usage": {"input_tokens": 7, "output_tokens": 3},
    })
    p = AnthropicProvider(api_key="sk-ant")
    content, citations = await p.generate_text(
        model="claude-sonnet-5", system_prompt="SYS", user_prompt="USR", max_tokens=123)
    assert content == "hi there"
    assert citations == []

    req = httpx_mock.get_requests()[0]
    assert str(req.url).endswith("/messages")
    assert req.headers["x-api-key"] == "sk-ant"          # not a Bearer token
    assert req.headers["anthropic-version"]
    body = json.loads(req.content)
    assert body["system"] == "SYS"                        # top-level, not a message
    assert body["messages"] == [{"role": "user", "content": "USR"}]
    assert body["max_tokens"] == 123                      # required by Anthropic
    await p.close()


@pytest.mark.asyncio
async def test_anthropic_cannot_generate_images():
    p = AnthropicProvider(api_key="sk-ant")
    assert not hasattr(p, "generate_image")
    await p.close()


@pytest.mark.asyncio
async def test_anthropic_empty_content_raises(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"content": []})
    p = AnthropicProvider(api_key="sk-ant")
    with pytest.raises(AIError, match="no text in response"):
        await p.generate_text(model="claude-sonnet-5", system_prompt="s", user_prompt="u")
    await p.close()


# ── Google adapter ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_google_text_request_and_parse(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={
        "candidates": [{"content": {"parts": [{"text": "gemini says hi"}]}}],
        "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2},
    })
    p = GoogleProvider(api_key="AIza-test")
    content, _ = await p.generate_text(
        model="gemini-2.5-flash", system_prompt="SYS", user_prompt="USR")
    assert content == "gemini says hi"

    req = httpx_mock.get_requests()[0]
    assert "models/gemini-2.5-flash:generateContent" in str(req.url)
    assert "key=AIza-test" in str(req.url)                # key goes in the query
    body = json.loads(req.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "SYS"
    await p.close()


@pytest.mark.asyncio
async def test_google_image_decodes_inline_data(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}},
    ]}}]})
    p = GoogleProvider(api_key="AIza-test")
    assert await p.generate_image(model="gemini-2.5-flash-image", prompt="a cat") == PNG
    await p.close()


@pytest.mark.asyncio
async def test_google_missing_image_raises(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"candidates": [{"content": {"parts": [{"text": "nope"}]}}]})
    p = GoogleProvider(api_key="AIza-test")
    with pytest.raises(AIError, match="no image in response"):
        await p.generate_image(model="gemini-2.5-flash-image", prompt="a cat")
    await p.close()


# ── OpenRouter adapter (grounding lives here) ────────────────────────────────

@pytest.mark.asyncio
async def test_openrouter_adapter_appends_online_when_grounded(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"choices": [{"message": {"content": "x"}}]})
    p = OpenRouterProvider(api_key="k")
    await p.generate_text(model="anthropic/claude-sonnet-5", system_prompt="s",
                          user_prompt="u", web_grounded=True)
    assert json.loads(httpx_mock.get_requests()[0].content)["model"].endswith(":online")
    await p.close()


@pytest.mark.asyncio
async def test_openrouter_adapter_plain_model_when_not_grounded(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"choices": [{"message": {"content": "x"}}]})
    p = OpenRouterProvider(api_key="k")
    await p.generate_text(model="anthropic/claude-sonnet-5", system_prompt="s",
                          user_prompt="u", web_grounded=False)
    assert json.loads(httpx_mock.get_requests()[0].content)["model"] == "anthropic/claude-sonnet-5"
    await p.close()
