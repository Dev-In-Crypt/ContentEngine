"""Public, no-auth demo for the Business product (doc §11 + §14).

Anyone pastes a public link → we detect the source type, pull the last ~90 days,
keep the newsworthy events (explainable rules), and stream a ready-to-edit draft
per event. This is marketing AND the first real-data run of the fetchers + selector,
so the hypothesis (good selection, grounded drafts) is tested before building the
full Business app.

Guardrails: no auth, but a HARD per-IP rate limit; runs on the app's OWN OpenRouter
key (anonymous visitors have none) so spend is bounded by the limit; text-only
drafts (no image cost); nothing is written to the database — the response is
ephemeral. Framed as "draft starters from your public data", never "a post from you".
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from typing import Optional

from pydantic import ConfigDict, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import (
    build_content_engine, get_db, get_demo_text_provider, get_settings,
)
from api.ratelimit import limiter
from config import Settings
from models.schemas import ImageSource, Platform, PostFormat
from services import app_spend
from services.brand_extract import extract_brand
from services.generation_credits import image_source_for
from services.url_guard import BlockedURL
from services.user_settings import resolve_ai_choice
from services.event_selector import score_item
from services.lead_builder import build_lead
from services.sources import SourceFetchError, detect_source_type, get_source_fetcher

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/demo", tags=["demo"])

_LOOKBACK_DAYS = 90
_MAX_LEADS = 5           # cap LLM calls per run — anonymous traffic on the app's key


class DemoRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        s = (v or "").strip()
        if not (s.startswith("http://") or s.startswith("https://")):
            raise ValueError("Enter a public http(s) URL")
        if len(s) > 500:
            raise ValueError("URL is too long")
        return s


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


@router.post("/from-url")
@limiter.limit("3/hour;10/day")
async def demo_from_url(
    request: Request,
    body: DemoRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    text_provider: Annotated[object, Depends(get_demo_text_provider)],
) -> StreamingResponse:
    # No app key configured → the demo can't generate. Say so cleanly, don't 500.
    if text_provider is None:
        raise HTTPException(status_code=503, detail="Demo is temporarily unavailable.")

    url = body.url
    text_model = settings.demo_text_model or settings.default_text_model
    ssl_verify = settings.ssl_verify

    async def event_stream() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = asyncio.Queue()

        async def run() -> None:
            try:
                await queue.put({"type": "progress", "message": "Reading the source…"})
                kind = detect_source_type(url)
                fetcher = get_source_fetcher(kind, ssl_verify=ssl_verify)
                since = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)
                items = await fetcher.fetch(url, since=since)

                # Keep the newsworthy events, de-duplicating against what we've seen.
                seen_titles: list[str] = []
                worthy: list[tuple] = []
                for it in items:
                    strength, reason = score_item(it, seen_titles)
                    seen_titles.append(it.title)
                    if strength == "worthy":
                        worthy.append((it, reason))
                    if len(worthy) >= _MAX_LEADS:
                        break

                if not worthy:
                    await queue.put({"type": "empty",
                                     "message": "No newsworthy updates found in the last 90 days."})
                    return

                await queue.put({"type": "progress",
                                 "message": f"Drafting {len(worthy)} post(s)…"})
                for it, reason in worthy:
                    lead = await build_lead(text_provider, it,
                                            text_model=text_model, platform=Platform.INSTAGRAM)
                    lead["strength"] = "worthy"
                    lead["reason"] = reason
                    await queue.put({"type": "lead", "lead": lead})
                await queue.put({"type": "complete"})
            except SourceFetchError as e:
                log.warning("Demo fetch failed for %s: %s", url, e)
                await queue.put({"type": "error",
                                 "message": "Couldn't read that source. Try a public page, "
                                            "RSS feed, or GitHub repository link."})
            except Exception:
                log.exception("Demo generation failed for %s", url)
                await queue.put({"type": "error",
                                 "message": "Something went wrong. Please try again."})
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        while True:
            event = await queue.get()
            if event is None:
                break
            yield _sse(event)
        await task

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────────────────────────
# A finished post for a visitor with no account (UX phase 7.1)
# ─────────────────────────────────────────────────────────────────────────────

#: One picture, and the request has no say in it. A carousel is ten images on a
#: stranger, and the landing exists to show what a post looks like rather than
#: how much we will spend proving it.
_LANDING_FORMAT = PostFormat.SINGLE


class LandingPostRequest(BaseModel):
    """A topic or a link. Never both, never anything else.

    `extra="forbid"` rather than the default ignore: ignoring an unknown field
    is how a sample generator quietly becomes a free model. The day somebody
    adds `instructions`, "ignore" would have been accepting it all along and
    nothing would have failed to say so.
    """
    model_config = ConfigDict(extra="forbid")

    topic: Optional[str] = None
    url: Optional[str] = None

    @model_validator(mode="after")
    def _exactly_one(self):
        topic = (self.topic or "").strip()
        url = (self.url or "").strip()
        if bool(topic) == bool(url):
            raise ValueError("Send either a topic or a link, not both")
        if topic and not (3 <= len(topic) <= 300):
            raise ValueError("A topic is between 3 and 300 characters")
        if url:
            if not (url.startswith("http://") or url.startswith("https://")):
                raise ValueError("Enter a public http(s) URL")
            if len(url) > 500:
                raise ValueError("URL is too long")
        self.topic, self.url = topic or None, url or None
        return self


async def _topic_from_url(url: str, text_provider, settings: Settings) -> str:
    """What a website is about, in a sentence a generator can use.

    Both halves are phase 1's: `extract_brand` fetches through the SSRF guard,
    and `guess_niche` never raises. So the worst a thin page produces is a thin
    topic, and the worst a hostile link produces is the same refusal every other
    user-supplied URL in the product already gets.
    """
    from services.brand_extract import guess_niche

    info = await extract_brand(url, ssl_verify=settings.ssl_verify)
    name = getattr(info, "name", None)
    niche = getattr(info, "niche", None)
    if not niche:
        guess = await guess_niche(
            text_provider, name=name, description=getattr(info, "description", None),
            text_model=settings.demo_text_model or settings.default_text_model)
        niche = getattr(guess, "niche", None)
    return f"One useful thing about {niche or name or url}"


def _data_url(image_bytes: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")


@router.post("/post")
@limiter.limit("4/hour;12/day")
async def landing_post(
    request: Request,
    body: LandingPostRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    text_provider: Annotated[object, Depends(get_demo_text_provider)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """Write one post and stream it back. Nothing is written down.

    No Post row, no slide file, no record that the visit happened — the picture
    goes back as a data URL and lives in the visitor's browser. That is not
    tidiness: a post with no owner would appear in the desktop app's own lists,
    outlive the visitor who caused it, and need a sweeper nobody has written.
    It is also what lets "Download" work with no second request and no account.
    """
    if text_provider is None:
        raise HTTPException(status_code=503, detail="Demo is temporarily unavailable.")

    # Our ceiling, before the model rather than after it. A visitor pays with
    # nothing — not even an email address — so this is the only thing standing
    # between a public text field and our invoice.
    if await app_spend.flush_and_total(db) >= settings.app_daily_spend_usd:
        raise HTTPException(
            status_code=503,
            detail="The free demo is resting until tomorrow. Sign up to keep going.")

    engine = build_content_engine(settings, None, actor=None)
    image_source = image_source_for(ImageSource.STOCK, settings, on_our_key=True)
    text_model = settings.demo_text_model or settings.default_text_model
    _provider, image_model, _key = resolve_ai_choice(None, settings, "image")

    async def event_stream() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = asyncio.Queue()

        async def progress(message: str, *, step: int | None = None,
                           total: int | None = None) -> None:
            event = {"type": "progress", "message": message}
            if step is not None:
                event["step"], event["total"] = step, total
            await queue.put(event)

        async def run() -> None:
            try:
                # Something on screen before the model answers. The engine
                # narrates its own stages, but the first of them arrives only
                # after the caption call returns — ten seconds of a field that
                # looks broken, on the one screen where nobody has any reason
                # to wait for us yet.
                topic = body.topic
                if topic is None:
                    await progress("Reading your site…")
                    topic = await _topic_from_url(body.url, text_provider, settings)
                else:
                    await progress("Writing your post…")
                generated = await engine.generate_post(
                    topic=topic,
                    format=_LANDING_FORMAT,
                    text_model=text_model,
                    image_model=image_model,
                    default_image_source=image_source,
                    platform=Platform.INSTAGRAM,
                    # A surcharge per call, and a stranger's first look is not
                    # worth buying live web search for.
                    web_grounded=False,
                    progress=progress,
                )
                slide = generated.slides[0] if generated.slides else None
                await queue.put({"type": "complete", "post": {
                    "topic": topic,
                    "caption": generated.caption,
                    "hook": generated.hook,
                    "cta": generated.cta,
                    "hashtags": generated.hashtags,
                    "image_data_url": _data_url(slide.image_bytes) if slide else None,
                }})
            except BlockedURL:
                await queue.put({"type": "error",
                                 "message": "That link can't be read. Try a public page."})
            except SourceFetchError:
                await queue.put({"type": "error",
                                 "message": "Couldn't read that link. Try a public page."})
            except Exception:
                log.exception("Landing post failed")
                await queue.put({"type": "error",
                                 "message": "Something went wrong. Please try again."})
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        while True:
            event = await queue.get()
            if event is None:
                break
            yield _sse(event)
        await task

    return StreamingResponse(event_stream(), media_type="text/event-stream")
