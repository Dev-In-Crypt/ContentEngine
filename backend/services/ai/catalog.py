"""Curated model catalogue.

Why curated: OpenRouter alone exposes ~340 models — an unusable dropdown. We ship a
short, opinionated list per provider (prices verified against each provider's public
list), and every provider also accepts a free-text "custom model id" so a user is
never blocked when a model is added or retired between releases.

The catalogue is served WITHOUT the user's API key, because the model dropdown has
to populate *before* the key is entered.

Prices are USD per 1M tokens and are used for two things: showing cost in the UI and
estimating spend for providers that (unlike OpenRouter) do not return a cost field.
They are indicative, not billing-grade — see estimate_cost().
"""
from __future__ import annotations

from typing import Optional

TEXT = "text"
IMAGE = "image"


def _m(model_id: str, label: str, price_in: float, price_out: float) -> dict:
    return {"id": model_id, "label": label, "price_in": price_in, "price_out": price_out}


# provider key → metadata. `key_field` is the Settings/credentials field holding the
# API key, so one key per provider serves both text and images.
PROVIDERS: dict[str, dict] = {
    "openrouter": {
        "label": "OpenRouter",
        "key_field": "openrouter_api_key",
        "key_url": "https://openrouter.ai/keys",
        "hint": "One key, every vendor's models. The only provider with live web search.",
        "supports_grounding": True,
        "text_models": [
            _m("anthropic/claude-sonnet-5", "Claude Sonnet 5", 2.00, 10.00),
            _m("anthropic/claude-opus-4.8", "Claude Opus 4.8", 5.00, 25.00),
            _m("anthropic/claude-haiku-4.5", "Claude Haiku 4.5", 1.00, 5.00),
            _m("openai/gpt-5.4", "GPT-5.4", 2.50, 15.00),
            _m("openai/gpt-5", "GPT-5", 1.25, 10.00),
            _m("openai/gpt-5-mini", "GPT-5 mini", 0.25, 2.00),
            _m("google/gemini-3.5-flash", "Gemini 3.5 Flash", 1.50, 9.00),
            _m("google/gemini-2.5-flash", "Gemini 2.5 Flash", 0.30, 2.50),
            _m("x-ai/grok-4.5", "Grok 4.5", 2.00, 6.00),
            _m("deepseek/deepseek-chat", "DeepSeek Chat", 0.20, 0.80),
        ],
        "image_models": [
            _m("google/gemini-3.1-flash-image", "Gemini 3.1 Flash Image", 0.50, 3.00),
            _m("google/gemini-3-pro-image", "Gemini 3 Pro Image", 2.00, 12.00),
            _m("google/gemini-2.5-flash-image", "Gemini 2.5 Flash Image", 0.30, 2.50),
            _m("google/gemini-3.1-flash-lite-image", "Gemini 3.1 Flash Lite Image", 0.25, 1.50),
            _m("openai/gpt-5-image-mini", "GPT-5 Image mini", 2.50, 2.00),
        ],
    },
    "openai": {
        "label": "OpenAI",
        "key_field": "openai_api_key",
        "key_url": "https://platform.openai.com/api-keys",
        "hint": "Direct OpenAI account. Text and images.",
        "supports_grounding": False,
        "text_models": [
            # GPT-5.6, newest first. Prices are the published per-1M-token rates
            # for short context on the standard tier — the same basis as every
            # other row here, so estimate_cost stays comparable across vendors.
            # Long context and Fast mode cost about double; the app does not
            # request either, so quoting the short-context number is honest
            # rather than optimistic.
            _m("gpt-5.6-sol", "GPT-5.6 Sol", 5.00, 30.00),
            _m("gpt-5.6-terra", "GPT-5.6 Terra", 2.00, 12.00),
            _m("gpt-5.6-luna", "GPT-5.6 Luna", 0.20, 1.20),
            _m("gpt-5.4", "GPT-5.4", 2.50, 15.00),
            _m("gpt-5", "GPT-5", 1.25, 10.00),
            _m("gpt-5-mini", "GPT-5 mini", 0.25, 2.00),
            _m("gpt-4.1", "GPT-4.1", 2.00, 8.00),
            _m("gpt-4o", "GPT-4o", 2.50, 10.00),
            _m("gpt-4o-mini", "GPT-4o mini", 0.15, 0.60),
        ],
        "image_models": [
            # Image tokens, not text tokens: $8.00 in / $30.00 out per 1M.
            _m("gpt-image-2", "GPT Image 2", 8.00, 30.00),
            _m("gpt-image-1", "GPT Image 1", 5.00, 40.00),
            _m("gpt-image-1-mini", "GPT Image 1 mini", 2.50, 20.00),
        ],
    },
    "anthropic": {
        "label": "Anthropic",
        "key_field": "anthropic_api_key",
        "key_url": "https://console.anthropic.com/settings/keys",
        "hint": "Direct Anthropic account. Text only — Anthropic does not generate images.",
        "supports_grounding": False,
        "text_models": [
            _m("claude-sonnet-5", "Claude Sonnet 5", 2.00, 10.00),
            _m("claude-opus-4-8", "Claude Opus 4.8", 5.00, 25.00),
            _m("claude-haiku-4-5", "Claude Haiku 4.5", 1.00, 5.00),
        ],
        "image_models": [],          # no image generation — hidden in the image picker
    },
    "google": {
        "label": "Google Gemini",
        "key_field": "google_api_key",
        "key_url": "https://aistudio.google.com/apikey",
        "hint": "Google AI Studio key. Strong and cheap for images.",
        "supports_grounding": False,
        "text_models": [
            _m("gemini-2.5-pro", "Gemini 2.5 Pro", 1.25, 10.00),
            _m("gemini-2.5-flash", "Gemini 2.5 Flash", 0.30, 2.50),
            _m("gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite", 0.10, 0.40),
        ],
        "image_models": [
            _m("gemini-2.5-flash-image", "Gemini 2.5 Flash Image", 0.30, 2.50),
        ],
    },
}

#: Providers that can generate images at all (used by the UI and by validation).
IMAGE_CAPABLE = [k for k, v in PROVIDERS.items() if v["image_models"]]


def is_valid_provider(provider: Optional[str], kind: str = TEXT) -> bool:
    if not provider or provider not in PROVIDERS:
        return False
    if kind == IMAGE:
        return bool(PROVIDERS[provider]["image_models"])
    return True


def key_field_for(provider: str) -> Optional[str]:
    """Which credential field holds this provider's key."""
    meta = PROVIDERS.get(provider)
    return meta["key_field"] if meta else None


def supports_grounding(provider: Optional[str]) -> bool:
    meta = PROVIDERS.get(provider or "")
    return bool(meta and meta["supports_grounding"])


def list_providers(kind: str = TEXT) -> list[dict]:
    """Catalogue for the settings dropdowns. Never includes keys or secrets.
    For kind=image, providers without image models are omitted."""
    out = []
    for key, meta in PROVIDERS.items():
        models = meta["image_models"] if kind == IMAGE else meta["text_models"]
        if not models:
            continue
        out.append({
            "key": key,
            "label": meta["label"],
            "hint": meta["hint"],
            "key_field": meta["key_field"],
            "key_url": meta["key_url"],
            "supports_grounding": meta["supports_grounding"],
            "models": models,
        })
    return out


def estimate_cost(provider: str, model: str,
                  prompt_tokens: Optional[int], completion_tokens: Optional[int]) -> float:
    """Approximate USD spend from token counts, for providers that do not report a
    cost (everyone except OpenRouter). Unknown model → 0.0 rather than a wrong guess."""
    meta = PROVIDERS.get(provider)
    if not meta:
        return 0.0
    for bucket in ("text_models", "image_models"):
        for m in meta[bucket]:
            if m["id"] == model:
                return round(
                    (prompt_tokens or 0) / 1e6 * m["price_in"]
                    + (completion_tokens or 0) / 1e6 * m["price_out"], 6)
    return 0.0


# ── Video generation ──────────────────────────────────────────────────────────
#
# Deliberately a separate catalogue, not another PROVIDERS entry. Kling's
# current API key is a single bearer token (the older AccessKey+SecretKey pair
# that needed a JWT signed on our side is now legacy and won't reach new
# models), which fits the plain per-user Settings field every non-text/image
# key already uses (elevenlabs_api_key, pexels_api_key) — not the
# provider+model+key trio resolve_ai_choice() resolves. And it is billed per
# second, not per token, so it can't share estimate_cost() either. Folding it
# into PROVIDERS would mean bending every existing entry's shape (key_field,
# a token-priced cost function) to accommodate one entry that would not even
# use most of it.
def _mv(model_id: str, label: str, price_per_sec: float) -> dict:
    return {"id": model_id, "label": label, "price_per_sec": price_per_sec}


# Model ids confirmed against Kling's own API reference. Per-second prices are
# NOT from Kling directly — they are reseller/aggregator-quoted figures at the
# time this was written and drift, sometimes by a lot, between vendors and
# over time. Treat these as a rough order of magnitude for a pre-flight UI
# estimate, the same "indicative, not billing-grade" caveat as estimate_cost(),
# and re-verify against Kling's current price sheet before this ships anywhere
# a user makes a spending decision from it.
VIDEO_PROVIDERS: dict[str, dict] = {
    "kling": {
        "label": "Kling",
        "key_field": "kling_api_key",
        "key_url": "https://kling.ai/dev/api-key",
        "hint": "Text-to-video and image-to-video. Billed per second, not per token — a "
                "10s clip runs roughly $0.75, about a hundred times an AI image.",
        "video_models": [
            _mv("kling-v3-0", "Kling 3.0", 0.075),
            _mv("kling-v3-0-turbo", "Kling 3.0 Turbo", 0.106),
            _mv("kling-v2-6", "Kling 2.6", 0.075),
            _mv("kling-v2-1-master", "Kling 2.1 Master", 0.075),
            _mv("kling-v1-6", "Kling 1.6 (default)", 0.075),
        ],
    },
}


def list_video_providers() -> list[dict]:
    """Catalogue for the video model dropdown. Never includes keys or secrets."""
    out = []
    for key, meta in VIDEO_PROVIDERS.items():
        out.append({
            "key": key,
            "label": meta["label"],
            "hint": meta["hint"],
            "key_field": meta["key_field"],
            "key_url": meta["key_url"],
            "models": meta["video_models"],
        })
    return out


def estimate_video_cost(provider: str, model: str, seconds: float) -> float:
    """Approximate USD spend for a video call, priced per second. See the
    VIDEO_PROVIDERS comment above for how rough these prices are — this exists
    to show an order of magnitude before a $0.75 click, not to bill anyone."""
    meta = VIDEO_PROVIDERS.get(provider)
    if not meta:
        return 0.0
    for m in meta["video_models"]:
        if m["id"] == model:
            return round(max(seconds, 0) * m["price_per_sec"], 4)
    return 0.0
