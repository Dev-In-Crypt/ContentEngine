"""Kling adapter — text-to-video and image-to-video.

Authenticates with a single bearer key (config.Settings.kling_api_key), NOT the
legacy AccessKey+SecretKey/JWT flow some Kling documentation still describes —
that pair won't reach new models going forward, which is why the credential
vault (phase 4) only ever stored one string.

The endpoint shapes below are confirmed against Kling's own api.klingai.com for
text2video: path, request body, and the {data:{task_id}} / {data:{task_status,
task_result}} response envelope. **image2video's exact field name/encoding for
the seed image is not independently confirmed** — Kling's own reference pages
are JS-rendered and don't yield to automated fetching, and third-party guides
disagree with each other on request-body details. If this needs a fix once
tested against a live key, it should cost this one file, which is why every
Kling HTTP call lives here and nowhere else.

Kling's task-status endpoint is namespaced by which creation path made the
task (`.../text2video/{id}` vs `.../image2video/{id}`) — there is no unified
query path confirmed. Since poll() may run in a different process/call than
create_task() (the scheduler poller, not the request that started the job),
that routing can't live in memory; it's encoded directly into the task id this
adapter hands back, as `"<kind>:<real_id>"`. That id is opaque to every caller
outside this file — MediaAsset.provider_task_id just stores whatever this
adapter returns and passes it back unexamined.
"""
from __future__ import annotations

import base64
from typing import Optional

import httpx

from services.http_utils import describe_request_error
from services.url_guard import BlockedURL, guarded_get
from services.video.genai.base import GenVideoStatus, VideoGenError

_KINDS = {"text2video", "image2video"}
#: Same ceiling as a user's own video upload (api/routes/media.py).
_MAX_VIDEO_BYTES = 200 * 1024 * 1024


class KlingVideoProvider:
    BASE_URL = "https://api.klingai.com"

    def __init__(self, api_key: str, ssl_verify: bool = True):
        self._api_key = api_key
        self._ssl_verify = ssl_verify
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                timeout=60.0,
                verify=self._ssl_verify,
            )
        return self._client

    async def create_task(
        self, *, prompt: str, model: str, duration_sec: int, aspect_ratio: str,
        image_bytes: Optional[bytes] = None,
    ) -> str:
        client = self._get_client()
        payload = {"model_name": model, "prompt": prompt, "duration": str(duration_sec)}
        if image_bytes is not None:
            kind = "image2video"
            # image2video derives its frame (and so its aspect) from the photo
            # itself — sending aspect_ratio alongside it has no target to apply to.
            payload["image"] = base64.b64encode(image_bytes).decode("ascii")
        else:
            kind = "text2video"
            payload["aspect_ratio"] = aspect_ratio
        try:
            response = await client.post(f"/v1/videos/{kind}", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise VideoGenError(
                f"Kling rejected the request: {e.response.status_code} {e.response.text}"
            ) from e
        except httpx.RequestError as e:
            raise VideoGenError(describe_request_error(e, "Kling")) from e
        body = response.json()
        task_id = (body.get("data") or {}).get("task_id")
        if not task_id:
            raise VideoGenError(f"Kling: no task_id in response: {body!r}")
        return f"{kind}:{task_id}"

    async def poll(self, task_id: str) -> GenVideoStatus:
        kind, real_id = self._split(task_id)
        client = self._get_client()
        try:
            response = await client.get(f"/v1/videos/{kind}/{real_id}")
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise VideoGenError(
                f"Kling status check failed: {e.response.status_code} {e.response.text}"
            ) from e
        except httpx.RequestError as e:
            raise VideoGenError(describe_request_error(e, "Kling")) from e
        data = response.json().get("data") or {}
        status = data.get("task_status")
        if status == "succeed":
            videos = (data.get("task_result") or {}).get("videos") or []
            url = videos[0].get("url") if videos else None
            if not url:
                # A "succeed" with nothing to download is not actionable — call
                # it a failure with an honest message instead of the poller
                # crashing trying to download None.
                return GenVideoStatus(
                    state="failed", error="Kling reported success with no video URL")
            return GenVideoStatus(state="succeed", video_url=url)
        if status == "failed":
            return GenVideoStatus(
                state="failed", error=data.get("task_status_msg") or "Generation failed")
        return GenVideoStatus(state="processing")

    async def download(self, url: str) -> bytes:
        # The result URL comes back inside Kling's poll response, so it is
        # theirs to choose, not ours — guarded like every other address we
        # didn't pick (services/url_guard.py).
        try:
            r = await guarded_get(url, ssl_verify=self._ssl_verify, timeout=120.0,
                                  max_bytes=_MAX_VIDEO_BYTES)
            r.raise_for_status()
        except BlockedURL as e:
            raise VideoGenError(str(e)) from e
        return r.content

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @staticmethod
    def _split(task_id: str) -> tuple[str, str]:
        kind, _, real_id = task_id.partition(":")
        if kind not in _KINDS or not real_id:
            raise VideoGenError(f"Malformed Kling task id: {task_id!r}")
        return kind, real_id
