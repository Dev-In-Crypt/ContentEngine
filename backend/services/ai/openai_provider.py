"""OpenAI adapter (direct account, not via OpenRouter).

Text uses /v1/chat/completions — the same shape OpenRouter mimics. Images use
/v1/images/generations, which is a different endpoint and returns base64, not a URL.
No web grounding, and no cost field in the response: spend is estimated from the
catalogue price table (see catalog.estimate_cost).
"""
from __future__ import annotations

import asyncio
import base64

import httpx

from services.ai.base import AIError, require_model
from services.ai.catalog import estimate_cost
from services.http_utils import describe_request_error
from services.openrouter import record_usage


class OpenAIProvider:
    BASE_URL = "https://api.openai.com/v1"
    supports_grounding = False

    def __init__(self, api_key: str, ssl_verify: bool = True):
        self._api_key = api_key
        self._ssl_verify = ssl_verify
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                timeout=120.0,
                verify=self._ssl_verify,
            )
        return self._client

    async def _post(self, path: str, payload: dict, retries: int = 2,
                    backoff: float = 3.0) -> dict:
        client = self._get_client()
        for attempt in range(retries + 1):
            try:
                response = await client.post(path, json=payload)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 503) and attempt < retries:
                    await asyncio.sleep(backoff * (attempt + 1))
                    continue
                raise AIError(
                    f"OpenAI failed: {e.response.status_code} {e.response.text}") from e
            except httpx.RequestError as e:
                raise AIError(describe_request_error(e, "OpenAI")) from e
        raise AIError("OpenAI: retries exhausted")

    async def generate_text(self, model: str, system_prompt: str, user_prompt: str,
                            max_tokens: int = 2000,
                            web_grounded: bool = False) -> tuple[str, list[dict]]:
        model_id = require_model(model, "text")
        # No reasoning effort is sent, on purpose. The GPT-5.6 family takes
        # `reasoning.effort` — but on the Responses API, which is a different
        # request shape, a different reply shape and a different usage block,
        # and that usage block feeds estimate_cost and the daily spend cap. So
        # adopting it is a change to the money path, not a new field.
        #
        # And the reason to want it is weak here: reasoning tokens bill as
        # OUTPUT, which on Luna is $1.20 against $0.20 in, while writing a
        # caption is not a multi-step reasoning task. Running the models at
        # their own defaults was measured on prod first — the captions came back
        # coherent and on-topic at $0.0012 a post.
        #
        # Revisit when there is a reason from the output, not from the menu.
        payload = await self._post("/chat/completions", {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_completion_tokens": max_tokens,
        })
        usage = payload.get("usage") or {}
        record_usage(model_id, {
            **usage,
            "cost": estimate_cost("openai", model_id, usage.get("prompt_tokens"),
                                  usage.get("completion_tokens")),
        })
        try:
            content = payload["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError) as e:
            raise AIError(f"OpenAI: unexpected response shape: {payload!r}") from e
        return content, []          # no grounding → no citations

    async def generate_image(self, model: str, prompt: str) -> bytes:
        model_id = require_model(model, "image")
        payload = await self._post("/images/generations", {
            "model": model_id, "prompt": prompt, "n": 1, "size": "1024x1024",
        })
        data = (payload.get("data") or [{}])[0]
        b64 = data.get("b64_json")
        if b64:
            return base64.b64decode(b64)
        url = data.get("url")
        if url:
            return await self._download(url)
        raise AIError(f"OpenAI: no image in response: {payload!r}")

    async def _download(self, url: str) -> bytes:
        async with httpx.AsyncClient(timeout=60.0, verify=self._ssl_verify) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.content

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
