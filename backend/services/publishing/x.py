"""X (Twitter) publisher — OAuth 1.0a user context.

Unlike Instagram, X takes image bytes directly: upload each image to the v1.1
media endpoint, then post a v2 tweet referencing the returned media ids. A tweet
allows at most 4 images. We cap tweets at 250 characters (below X's 280) and
chain threads with in_reply_to_tweet_id.

Signing: authlib's low-level ClientAuth computes the OAuth 1.0a Authorization
header; we attach it to a plain httpx request and send the body ourselves.
(authlib's httpx auth *flow* empties non-form bodies, which would drop the v2
tweet JSON — so we sign, but don't let it touch the request.) The JSON tweet body
and the multipart media upload are both outside the OAuth1 signature base string,
which is standard.

No live verification exists yet (posting needs a paid API tier); covered by unit
tests against mocked endpoints, same as the Instagram client was.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlencode

import httpx

from models.schemas import TWEET_CHAR_LIMIT
from services.publishing.base import PublishOutcome, PublisherError
from services.x_text import fit_tweet

log = logging.getLogger(__name__)

MAX_CHARS = TWEET_CHAR_LIMIT   # 250; below X's 280 on purpose
MAX_IMAGES = 4
_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
_METADATA_URL = "https://upload.twitter.com/1.1/media/metadata/create.json"
_TWEET_URL = "https://api.twitter.com/2/tweets"
_ME_URL = "https://api.twitter.com/2/users/me"

# Video (Phase 8): the same v1.1 media endpoint, chunked (INIT/APPEND/FINALIZE/
# STATUS). Confirmed live against the account's own OAuth 1.0a credentials —
# query-string params signed, the v2 dedicated endpoints reject this auth type.
VIDEO_CHUNK_BYTES = 4 * 1024 * 1024   # X caps one APPEND segment at 5 MB
_VIDEO_CATEGORY = "tweet_video"


def _normalize_processing(data: dict) -> dict:
    """FINALIZE/STATUS -> {"state", "check_after_secs", "error"}. No
    processing_info at all (can happen for a trivial file) means X considers
    it already done."""
    info = data.get("processing_info")
    if info is None:
        return {"state": "succeeded", "check_after_secs": None, "error": None}
    err = info.get("error")
    return {
        "state": info.get("state", "succeeded"),
        "check_after_secs": info.get("check_after_secs"),
        "error": f"{err.get('name')}: {err.get('message')}" if err else None,
    }


class XPublisher:
    def __init__(self, api_key: str, api_secret: str,
                 access_token: str, access_token_secret: str):
        from authlib.oauth1 import ClientAuth
        self._signer = ClientAuth(api_key, api_secret,
                                  token=access_token, token_secret=access_token_secret)
        self._client = httpx.AsyncClient(timeout=60.0)

    def _auth(self, method: str, url: str) -> str:
        # body=None: JSON/multipart bodies are not part of the OAuth1 signature.
        _, headers, _ = self._signer.sign(method, url, {}, None)
        return headers["Authorization"]

    def _signed(self, method: str, url: str, params: dict) -> tuple[str, dict]:
        """(url_to_send, headers) for a request whose parameters must ride the
        QUERY STRING — OAuth 1.0a folds query params into the signature base
        string but not multipart/JSON bodies, confirmed live: sending `command=`
        only in the multipart body 401s. body stays None for the same reason
        as _auth: a non-empty non-form body makes authlib add oauth_body_hash,
        an extension X doesn't implement."""
        full = f"{url}?{urlencode(sorted(params.items()))}" if params else url
        _, headers, _ = self._signer.sign(method, full, {}, None)
        return full, headers

    async def verify_credentials(self) -> dict:
        """Read-only preflight: confirm the 4 OAuth keys work WITHOUT tweeting.
        Returns {'username','name'} on success; raises PublisherError on failure.
        Uses only the Read scope."""
        try:
            r = await self._client.get(
                _ME_URL, headers={"Authorization": self._auth("GET", _ME_URL)})
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise PublisherError(
                f"{e.response.status_code} {e.response.text[:200]}") from e
        except httpx.RequestError as e:
            raise PublisherError(f"X network error: {e}") from e
        data = r.json().get("data") or {}
        return {"username": data.get("username"), "name": data.get("name")}

    async def publish(self, images: list[bytes], caption: str,
                      alt_text: Optional[str] = None,
                      long_form: bool = False) -> PublishOutcome:
        """One tweet. `long_form` skips the length cap for X Premium accounts,
        where the 280-char limit does not apply."""
        media_ids = [await self._upload_media(img) for img in images[:MAX_IMAGES]]
        for mid in media_ids:
            await self._set_alt_text(mid, alt_text)
        text = (caption or "") if long_form else fit_tweet(caption or "", MAX_CHARS)
        tweet_id = await self._post_tweet(text, media_ids=media_ids)
        return PublishOutcome(
            media_id=tweet_id,
            permalink=f"https://x.com/i/web/status/{tweet_id}",
        )

    async def publish_thread(self, parts: list[str], images: list[bytes],
                             alt_text: Optional[str] = None) -> PublishOutcome:
        """Post a chain: the first tweet carries the image, each following tweet
        replies to the one before it."""
        media_ids = [await self._upload_media(img) for img in images[:1]]
        return await self._post_chain(parts, media_ids, alt_text)

    async def publish_video(self, media_id: str, caption: str, *,
                            alt_text: Optional[str] = None,
                            long_form: bool = False) -> PublishOutcome:
        """One tweet carrying an already-uploaded, already-processed video."""
        await self._set_alt_text(media_id, alt_text)
        text = (caption or "") if long_form else fit_tweet(caption or "", MAX_CHARS)
        tweet_id = await self._post_tweet(text, media_ids=[media_id])
        return PublishOutcome(
            media_id=tweet_id,
            permalink=f"https://x.com/i/web/status/{tweet_id}",
        )

    async def publish_video_thread(self, media_id: str, parts: list[str], *,
                                   alt_text: Optional[str] = None) -> PublishOutcome:
        """A thread with the video on the first tweet."""
        return await self._post_chain(parts, [media_id], alt_text)

    async def _post_chain(self, parts: list[str], media_ids: list[str],
                          alt_text: Optional[str]) -> PublishOutcome:
        """Post a chain: media rides the first tweet, each following tweet
        replies to the one before it. Shared by publish_thread (images) and
        publish_video_thread (one video) so the partial-failure handling below
        exists in exactly one place.

        X has no transaction here. If tweet 4 of 7 fails, tweets 1-3 are already
        live and cannot be rolled back — so the error says how many went out and
        links the first one, otherwise the user is left guessing what to fix.
        """
        parts = [fit_tweet(p, MAX_CHARS) for p in parts if (p or "").strip()]
        if not parts:
            raise PublisherError("Thread is empty — nothing to publish.")

        for mid in media_ids:
            await self._set_alt_text(mid, alt_text)
        first_id: Optional[str] = None
        prev_id: Optional[str] = None
        posted = 0
        try:
            for index, text in enumerate(parts):
                tweet_id = await self._post_tweet(
                    text,
                    media_ids=media_ids if index == 0 else None,
                    reply_to=prev_id,
                )
                posted += 1
                prev_id = tweet_id
                if first_id is None:
                    first_id = tweet_id
        except PublisherError as e:
            if posted:
                raise PublisherError(
                    f"Thread partly published: {posted} of {len(parts)} tweets are live "
                    f"(https://x.com/i/web/status/{first_id}). Finish it by hand or delete "
                    f"it on X, then retry. Original error: {e}"
                ) from e
            raise

        return PublishOutcome(
            media_id=first_id or "",
            permalink=f"https://x.com/i/web/status/{first_id}",
        )

    async def _post_tweet(self, text: str, media_ids: Optional[list[str]] = None,
                          reply_to: Optional[str] = None) -> str:
        payload: dict = {"text": text}
        if media_ids:
            payload["media"] = {"media_ids": media_ids}
        if reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": reply_to}
        try:
            r = await self._client.post(
                _TWEET_URL, json=payload,
                headers={"Authorization": self._auth("POST", _TWEET_URL)},
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise PublisherError(
                f"X tweet failed: {e.response.status_code} {e.response.text[:200]}"
            ) from e
        except httpx.RequestError as e:
            raise PublisherError(f"X network error: {e}") from e

        tweet_id = (r.json().get("data") or {}).get("id")
        if not tweet_id:
            raise PublisherError(f"X response missing tweet id: {r.text[:200]}")
        return tweet_id

    async def _upload_media(self, image_bytes: bytes) -> str:
        try:
            r = await self._client.post(
                _UPLOAD_URL,
                files={"media": image_bytes},   # multipart; not part of the signature
                headers={"Authorization": self._auth("POST", _UPLOAD_URL)},
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise PublisherError(
                f"X media upload failed: {e.response.status_code} {e.response.text[:200]}"
            ) from e
        except httpx.RequestError as e:
            raise PublisherError(f"X media network error: {e}") from e

        mid = r.json().get("media_id_string")
        if not mid:
            raise PublisherError(f"X media response missing id: {r.text[:200]}")
        return mid

    async def video_upload_init(self, total_bytes: int, *, media_type: str = "video/mp4",
                                media_category: str = _VIDEO_CATEGORY) -> str:
        params = {"command": "INIT", "total_bytes": total_bytes,
                  "media_type": media_type, "media_category": media_category}
        url, headers = self._signed("POST", _UPLOAD_URL, params)
        try:
            r = await self._client.post(url, headers=headers)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise PublisherError(
                f"X media INIT failed: {e.response.status_code} {e.response.text[:200]}"
            ) from e
        except httpx.RequestError as e:
            raise PublisherError(f"X media network error: {e}") from e

        mid = r.json().get("media_id_string")
        if not mid:
            raise PublisherError(f"X media INIT response missing id: {r.text[:200]}")
        return mid

    async def video_upload_append(self, media_id: str, segment_index: int,
                                  chunk: bytes) -> None:
        params = {"command": "APPEND", "media_id": media_id, "segment_index": segment_index}
        url, headers = self._signed("POST", _UPLOAD_URL, params)
        try:
            r = await self._client.post(url, files={"media": chunk}, headers=headers)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise PublisherError(
                f"X media APPEND failed: {e.response.status_code} {e.response.text[:200]}"
            ) from e
        except httpx.RequestError as e:
            raise PublisherError(f"X media network error: {e}") from e

    async def video_upload_finalize(self, media_id: str) -> dict:
        params = {"command": "FINALIZE", "media_id": media_id}
        url, headers = self._signed("POST", _UPLOAD_URL, params)
        try:
            r = await self._client.post(url, headers=headers)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise PublisherError(
                f"X media FINALIZE failed: {e.response.status_code} {e.response.text[:200]}"
            ) from e
        except httpx.RequestError as e:
            raise PublisherError(f"X media network error: {e}") from e
        return _normalize_processing(r.json())

    async def video_upload_status(self, media_id: str) -> dict:
        params = {"command": "STATUS", "media_id": media_id}
        url, headers = self._signed("GET", _UPLOAD_URL, params)
        try:
            r = await self._client.get(url, headers=headers)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise PublisherError(
                f"X media STATUS failed: {e.response.status_code} {e.response.text[:200]}"
            ) from e
        except httpx.RequestError as e:
            raise PublisherError(f"X media network error: {e}") from e
        return _normalize_processing(r.json())

    async def _set_alt_text(self, media_id: str, alt_text: Optional[str]) -> None:
        """Attach accessibility text to an uploaded image (v1.1 metadata/create).
        BEST-EFFORT: the media is already uploaded, so a metadata failure must not
        stop the tweet — we log and move on rather than lose the whole post."""
        text = (alt_text or "").strip()
        if not text:
            return
        try:
            r = await self._client.post(
                _METADATA_URL,
                json={"media_id": media_id, "alt_text": {"text": text[:1000]}},
                headers={"Authorization": self._auth("POST", _METADATA_URL)},
            )
            r.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.warning("X alt-text set failed for media %s (continuing): %s",
                        media_id, e)

    async def close(self) -> None:
        await self._client.aclose()
