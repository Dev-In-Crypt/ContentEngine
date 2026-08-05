import asyncio
import base64
import httpx
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

from services.http_utils import describe_request_error
from services.url_guard import BlockedURL, guarded_get

#: Same ceiling as a user's own photo upload (api/routes/media.py).
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


# ── LLM usage tracking ───────────────────────────────────────────────────────
# OpenRouter returns a `usage` object (and `usage.cost` in USD when we ask for
# it). We buffer each call in-memory; the admin router flushes the buffer into
# the LLMUsage table on demand.
_USAGE_BUFFER: list[dict] = []

# The acting user's id for the current async task, set by api.deps.get_current_user.
# Recorded on each usage row so the cost dashboard can be scoped per user. Lives
# here (not in api.deps) to avoid a circular import — deps imports this module.
current_user_id: ContextVar[Optional[str]] = ContextVar("current_user_id", default=None)


def record_usage(model: str, usage: Optional[dict]) -> None:
    if not usage:
        return
    _USAGE_BUFFER.append({
        "model": model,
        "user_id": current_user_id.get(),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cost": float(usage.get("cost") or 0.0),
        "at": datetime.now(timezone.utc),
    })


def drain_usage() -> list[dict]:
    """Return and clear the buffered usage records."""
    global _USAGE_BUFFER
    out, _USAGE_BUFFER = _USAGE_BUFFER, []
    return out


TEXT_MODELS: dict[str, str] = {
    "claude-sonnet": "anthropic/claude-sonnet-4",
    "claude-haiku": "anthropic/claude-haiku-4",
    "gpt-4o": "openai/gpt-4o",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gemini-pro": "google/gemini-2.5-pro",
    "gemini-flash": "google/gemini-2.5-flash",
    "llama-70b": "meta-llama/llama-3.3-70b-instruct",
}

IMAGE_MODELS: dict[str, str] = {
    "dall-e-3": "openai/dall-e-3",
    "flux-pro": "black-forest-labs/flux-1.1-pro",
    "sdxl": "stabilityai/stable-diffusion-xl",
}


class OpenRouterError(Exception):
    pass


class OpenRouterClient:
    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self, api_key: str, referer: str = "https://localhost",
        app_title: str = "InstaContentEngine", ssl_verify: bool = True,
    ):
        self.api_key = api_key
        self._referer = referer
        self._app_title = app_title
        self._ssl_verify = ssl_verify
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": self._referer,
                    "X-Title": self._app_title,
                },
                timeout=120.0,
                verify=self._ssl_verify,
            )
        return self._client

    async def _post_with_retry(self, payload: dict, retries: int = 2, backoff: float = 3.0) -> httpx.Response:
        client = self._get_client()
        last_exc: Exception = RuntimeError("no attempts")
        for attempt in range(retries + 1):
            try:
                response = await client.post("/chat/completions", json=payload)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 503) and attempt < retries:
                    await asyncio.sleep(backoff * (attempt + 1))
                    last_exc = e
                    continue
                raise OpenRouterError(
                    f"OpenRouter failed: {e.response.status_code} {e.response.text}"
                ) from e
            except httpx.RequestError as e:
                raise OpenRouterError(describe_request_error(e, "OpenRouter")) from e
        raise OpenRouterError(str(last_exc)) from last_exc

    async def generate_text(
        self, model: str, system_prompt: str, user_prompt: str, max_tokens: int = 2000,
        web_grounded: bool = False,
    ) -> tuple[str, list[dict]]:
        """Returns (content, citations).

        `web_grounded` appends OpenRouter's ':online' suffix, which runs a live web
        search (Exa.ai) and returns url citations. Grounding is an OpenRouter
        feature, so the suffix is applied here rather than by the caller.
        """
        if web_grounded and model and not model.endswith(":online"):
            model = f"{model}:online"
        response = await self._post_with_retry({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "usage": {"include": True},
        })
        payload = response.json()
        record_usage(model, payload.get("usage"))
        message = payload["choices"][0]["message"]
        content = message.get("content", "")
        # Flatten OpenRouter annotations → [{title, url}]
        citations: list[dict] = []
        for ann in (message.get("annotations") or []):
            if ann.get("type") == "url_citation":
                uc = ann.get("url_citation") or {}
                url = uc.get("url")
                if url:
                    citations.append({"title": uc.get("title") or url, "url": url})
        return content, citations

    async def generate_vision_json(
        self, model: str, prompt: str, image_urls: list[str], max_tokens: int = 600,
    ) -> dict:
        """Ask a vision model to look at images and answer in JSON — used by the
        b-roll frame judge (Reels R2). One user message with content parts
        (text + image_url×N), temperature 0 for determinism. Returns {} when the
        reply isn't usable JSON — callers are fail-open by design."""
        content: list[dict] = [{"type": "text", "text": prompt}]
        for url in image_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})
        response = await self._post_with_retry({
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "usage": {"include": True},
        })
        payload = response.json()
        record_usage(model, payload.get("usage"))
        raw = payload["choices"][0]["message"].get("content", "")
        from services.lead_builder import _loads   # json-repair tolerant parse
        data = _loads(raw)
        return data if isinstance(data, dict) else {}

    async def generate_image(self, model: str, prompt: str, size: str = "1024x1024") -> bytes:
        """Generate image via chat/completions (Gemini-style). Returns raw image bytes."""
        response = await self._post_with_retry({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "usage": {"include": True},
        })

        payload = response.json()
        record_usage(model, payload.get("usage"))
        message = payload["choices"][0]["message"]

        # Check message.images list first (OpenRouter Gemini format)
        for source in [
            message.get("images") or [],
            message.get("content") if isinstance(message.get("content"), list) else [],
        ]:
            for part in source:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    if url.startswith("data:"):
                        return self._decode_base64(url)
                    return await self._download_url(url)

        # Fallback: content is a plain data URL string
        content = message.get("content", "")
        if isinstance(content, str) and content.startswith("data:"):
            return self._decode_base64(content)

        raise OpenRouterError(f"No image found in OpenRouter response. Message: {message!r}")

    @staticmethod
    def _decode_base64(data_url: str) -> bytes:
        _, b64 = data_url.split(",", 1)
        return base64.b64decode(b64)

    async def _download_url(self, url: str) -> bytes:
        # Guarded: this URL comes out of a model response, not from us. A
        # provider having a bad day — or a prompt that talked one into naming
        # an address of its choosing — must not turn into a fetch of our own
        # network. See services/url_guard.py.
        try:
            r = await guarded_get(url, ssl_verify=self._ssl_verify, timeout=60.0,
                                  max_bytes=_MAX_IMAGE_BYTES)
            r.raise_for_status()
        except BlockedURL as e:
            raise OpenRouterError(str(e)) from e
        except httpx.RequestError as e:
            raise OpenRouterError(describe_request_error(e, "OpenRouter image download")) from e
        return r.content

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
