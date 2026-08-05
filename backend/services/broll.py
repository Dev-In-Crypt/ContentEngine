"""B-roll sourcing for voiceover Reels (R2) — Pexels VIDEO search + frame judge.

Ported from the user's proven shorts-pipeline step4, minus the heavy parts:
progressive query fallback, portrait-first sorting and ≥1080p file selection are
kept; CLIP reranking is replaced by an optional vision judge running on the
user's own OpenRouter key (fail-open — b-roll must degrade, never crash a reel).

Uses the SAME Pexels key users already store for stock photos.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from services.url_guard import BlockedURL, guarded_stream

log = logging.getLogger(__name__)

_SEARCH_URL = "https://api.pexels.com/videos/search"
_JUDGE_MAX_CANDIDATES = 3
_JUDGE_MIN_MEANING = 6
#: A stock b-roll clip is seconds long. Anything this size is not one.
_MAX_CLIP_BYTES = 200 * 1024 * 1024


class PexelsAuthError(Exception):
    """The Pexels key was rejected (401/403). Unlike a transient error this will
    never succeed on retry, so the search stops and the caller surfaces it as a
    clear "fix your key" message instead of silently falling back to slides."""


@dataclass
class Candidate:
    """One stock video candidate for a narration segment."""
    video_id: int
    url: str                     # download link of the chosen quality variant
    duration: float
    thumbnail_url: str
    picture_urls: list[str] = field(default_factory=list)   # ~3 sampled frames

    @property
    def frames(self) -> list[str]:
        urls = list(self.picture_urls)
        if self.thumbnail_url and self.thumbnail_url not in urls:
            urls.append(self.thumbnail_url)
        return urls[:3]

    @property
    def page_url(self) -> str:
        return f"https://www.pexels.com/video/{self.video_id}/"


class PexelsVideoSearch:
    def __init__(self, api_key: str, *, ssl_verify: bool = True) -> None:
        self._api_key = api_key
        self._ssl_verify = ssl_verify

    async def _call(self, client: httpx.AsyncClient, query: str,
                    min_dur: Optional[int], size: str) -> dict:
        params: dict = {"query": query, "orientation": "portrait", "size": size,
                        "per_page": 15}
        if min_dur:
            params["min_duration"] = min_dur
        resp = await client.get(_SEARCH_URL, params=params)
        resp.raise_for_status()
        return resp.json()

    async def candidates(self, query: str, target_duration: float,
                         max_results: int = 8) -> list[Candidate]:
        """Progressive fallback: full query at large/medium quality with a
        duration floor, then a simplified query, then no duration filter.
        First attempt that yields videos wins."""
        target_int = max(1, math.ceil(target_duration))
        simple = " ".join(query.split()[:2]) or query
        attempts = [
            (query, target_int, "large"),
            (query, target_int, "medium"),
            (simple if simple != query else None, target_int, "medium"),
            (query, None, "medium"),
            (simple if simple != query else None, None, "medium"),
        ]

        videos: list[dict] = []
        async with httpx.AsyncClient(
            headers={"Authorization": self._api_key}, timeout=20.0,
            verify=self._ssl_verify,
        ) as client:
            for q, mdur, size in attempts:
                if not q:
                    continue
                try:
                    data = await self._call(client, q, mdur, size)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in (401, 403):
                        # A bad key fails every attempt identically — stop and let
                        # the caller tell the user, don't burn 5 retries or hide it.
                        raise PexelsAuthError(
                            "Pexels rejected the API key") from e
                    log.warning("Pexels video search failed (q=%r): %s", q, e)
                    await asyncio.sleep(2)
                    try:
                        data = await self._call(client, q, mdur, size)
                    except Exception:  # noqa: BLE001
                        continue
                except Exception as e:  # noqa: BLE001 — one retry then next attempt
                    log.warning("Pexels video search failed (q=%r): %s", q, e)
                    await asyncio.sleep(2)
                    try:
                        data = await self._call(client, q, mdur, size)
                    except Exception:  # noqa: BLE001
                        continue
                videos = data.get("videos", []) or []
                if videos:
                    break

        if not videos:
            return []

        # Portrait first, longer first — vertical sources need no crop gymnastics.
        videos.sort(key=lambda v: (
            0 if v.get("height", 0) >= v.get("width", 0) else 1,
            -float(v.get("duration") or 0),
        ))

        out: list[Candidate] = []
        for v in videos[:max_results]:
            files = v.get("video_files") or []
            if not files:
                continue
            # Prefer ≥1080-high variants (sharp after the 1080x1920 normalize),
            # closest to 1920; else the largest available.
            hi = [f for f in files if (f.get("height") or 0) >= 1080]
            if hi:
                best = min(hi, key=lambda f: abs((f.get("height") or 0) - 1920))
            else:
                best = max(files, key=lambda f: (f.get("height") or 0))
            link = best.get("link")
            if not link:
                continue
            pics = [p.get("picture") for p in (v.get("video_pictures") or [])
                    if isinstance(p, dict) and p.get("picture")]
            if len(pics) > 3:   # start / middle / end — evens out fades and slates
                pics = [pics[0], pics[len(pics) // 2], pics[-1]]
            out.append(Candidate(
                video_id=int(v.get("id") or 0), url=link,
                duration=float(v.get("duration") or 0),
                thumbnail_url=str(v.get("image") or ""), picture_urls=pics,
            ))
        return out

    async def download(self, url: str, dest: Path) -> None:
        # guarded_stream, not guarded_get: the CDN link comes from a Pexels
        # search response (theirs to choose, so it is guarded) but a stock clip
        # is tens of megabytes and belongs on disk, not buffered in the RAM of
        # a box that is also running ffmpeg.
        written = 0
        try:
            async with guarded_stream("GET", url, ssl_verify=self._ssl_verify,
                                      timeout=120.0) as resp:
                resp.raise_for_status()
                with dest.open("wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        written += len(chunk)
                        if written > _MAX_CLIP_BYTES:
                            raise BlockedURL(
                                f"clip over {_MAX_CLIP_BYTES} bytes from {url!r}")
                        f.write(chunk)
        except BlockedURL:
            dest.unlink(missing_ok=True)     # never leave a half clip to be muxed
            raise


def _judge_prompt(segment_text: str, query: str, n_frames: int) -> str:
    return (
        "You are a strict editor reviewing whether a stock clip fits a specific "
        "scene in a vertical social video.\n\n"
        f'Voiceover line: "{segment_text}"\n'
        f'Intended shot: "{query}"\n\n'
        f"You are shown {n_frames} keyframes from the same clip.\n\n"
        "Score the clip on two axes (1-10 each):\n"
        "- meaning_match: how well it depicts the intended shot / line meaning\n"
        "- mood_match: how well its atmosphere fits\n\n"
        'Return STRICT JSON: {"meaning_match": <1-10>, "mood_match": <1-10>, '
        '"use": true|false, "reason": "<short>"}'
    )


async def pick_with_judge(provider, candidates: list[Candidate], *,
                          segment_text: str, query: str,
                          judge_model: str) -> Optional[Candidate]:
    """Return the first candidate the vision judge accepts (use && meaning>=6),
    judging at most 3. FAIL-OPEN by design: a provider without vision support,
    a judge error or unusable JSON all return the top search hit — a worse clip
    beats a crashed reel."""
    if not candidates:
        return None
    if not hasattr(provider, "vision_json") or not judge_model:
        return candidates[0]
    for cand in candidates[:_JUDGE_MAX_CANDIDATES]:
        frames = cand.frames
        if not frames:
            continue
        try:
            data = await provider.vision_json(
                model=judge_model,
                prompt=_judge_prompt(segment_text, query, len(frames)),
                image_urls=frames, max_tokens=300)
        except Exception as e:  # noqa: BLE001 — judge is optional
            log.warning("B-roll judge unavailable: %s", e)
            return candidates[0]
        try:
            meaning = float(data.get("meaning_match") or 0)
            use = bool(data.get("use"))
        except (TypeError, ValueError):
            return candidates[0]
        if use and meaning >= _JUDGE_MIN_MEANING:
            return cand
    return candidates[0]   # nobody passed — still prefer showing something
