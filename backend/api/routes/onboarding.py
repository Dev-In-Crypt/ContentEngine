"""The one post the application pays for.

Onboarding (UX phase 5) ends on something real rather than an empty composer,
and a brand-new account has no AI key yet — asking for one before showing
anything is exactly what the UX document says not to do. So the last screen's
post is written on the application's own key.

That makes this the only endpoint in the product that spends OUR money, and
almost all of it is therefore about refusing. Two properties keep it from
becoming an LLM proxy:

  * **The request carries no prompt.** No topic, no instructions, no niche, and
    `extra="forbid"` rejects one if somebody adds a field. The subject is built
    server-side from the profile the account already saved, so the worst anyone
    can extract is one short post about a niche they typed in themselves.
  * **The allowance is spent before the model is called**, on the account rather
    than the IP, committed by `free_generation.reserve`. A new browser, a
    cleared localStorage and a VPN all fail to produce a second one.

The order of the guards is itself a guard: anything that will be refused is
refused *before* it costs anything.

**No email-verification guard here, on purpose.** It used to carry one, which
was inert while REQUIRE_VERIFIED_EMAIL was false and turned live the day that
flag went on — and everybody who reaches this screen is unverified, because they
registered ninety seconds ago. So the payoff moment of onboarding started
answering "Please verify your email before publishing", on a screen that
publishes nothing, to every new account. It bought nothing either: the same
unverified account gets a 200 from /api/posts/generate one click later, so the
farm it was supposed to stop simply walks around it. What actually bounds this
route is above — one post per account, per-IP limits, and the daily ceiling.
Verification still gates publishing and scheduling, where it means something.
"""
import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import (
    get_current_user, get_db, get_demo_text_provider, get_settings,
)
from api.ratelimit import limiter
from config import Settings
from models.database import User as UserModel
from models.schemas import Platform, PostFormat
from services import free_generation
from services.caption_generator import CaptionGenerator
from services.managed_account import resolve_active_account
from services.openrouter import current_user_id
from services.user_settings import resolve_user_brand_voice, resolve_user_profile

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


class FirstPostRequest(BaseModel):
    """Deliberately one field.

    `extra="forbid"` rather than the default ignore: ignoring an unknown field
    is how a sample generator quietly becomes a free model. The day somebody
    adds `topic`, "ignore" would have been accepting it all along and nothing
    would have failed to say so.
    """
    model_config = ConfigDict(extra="forbid")

    platform: Literal["instagram", "x"] = "instagram"


def first_post_topic(profile: dict) -> str:
    """What the sample post is about, from the brand the account already saved.

    A pure function of stored data, never of the request — that is the whole
    reason this endpoint cannot be used to write somebody's homework.
    """
    niche = (profile.get("niche") or "").strip()
    audience = (profile.get("target_audience") or "").strip()
    brand = (profile.get("brand_name") or "").strip()
    subject = niche or brand
    topic = f"One useful thing about {subject}"
    if audience:
        topic += f", for {audience}"
    return topic


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


@router.post("/first-post")
@limiter.limit("5/hour;20/day")
async def first_post(
    request: Request,
    body: FirstPostRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    text_provider: Annotated[object, Depends(get_demo_text_provider)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> StreamingResponse:
    """Write one post on the application's key and stream it back.

    Nothing is persisted: no `Post`, no slides, no files. A zero-slide Instagram
    post is a shape `/api/posts/generate` explicitly refuses, and inventing one
    here to make onboarding tidy would put it in the Queue, the Calendar and the
    profile grid — three screens claiming the user has work in progress before
    they have made anything.
    """
    # No app key configured → say so cleanly rather than 500. This is also the
    # permanent state of the e2e server, so it is the ordinary case.
    if text_provider is None:
        raise HTTPException(status_code=503,
                            detail="Sample posts are temporarily unavailable.")

    acct = await resolve_active_account(db, user)
    profile = resolve_user_profile(acct)
    if not (profile.get("niche") or profile.get("brand_name")):
        # Before the reservation: a request that cannot produce anything useful
        # must not cost the account its one free post.
        raise HTTPException(status_code=422, detail="Tell us about your brand first.")

    if not await free_generation.reserve(db, user):
        raise HTTPException(
            status_code=409,
            detail="You've used your free sample post. Add your own AI key to keep going.")

    topic = first_post_topic(profile)
    brand_voice = resolve_user_brand_voice(acct)
    text_model = settings.demo_text_model or settings.default_text_model
    platform = Platform(body.platform)
    caption_gen = CaptionGenerator(text_provider)

    async def event_stream() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = asyncio.Queue()

        async def run() -> None:
            try:
                await queue.put({"type": "progress",
                                 "message": "Writing your first post…"})
                # Our key, our bill. `record_usage` stamps whatever this holds,
                # and the auth dependency set it to the caller — leaving it would
                # put our spend on their usage dashboard, which contradicts the
                # one thing we told them: you pay the vendor directly.
                current_user_id.set(None)
                caption = await caption_gen.generate(
                    topic=topic,
                    format=PostFormat.SINGLE,
                    num_slides=0,
                    text_model=text_model,
                    niche=profile.get("niche"),
                    target_audience=profile.get("target_audience"),
                    brand_name=profile.get("brand_name"),
                    brand_voice=brand_voice,
                    platform=platform,
                    web_grounded=False,
                )
                await queue.put({"type": "complete", "post": {
                    "topic": topic,
                    "platform": platform.value,
                    "hook": caption.hook,
                    "caption": caption.caption,
                    "cta": caption.cta,
                    "hashtags": caption.hashtags,
                }})
            except Exception:
                # The provider was called and gave nothing usable, so the account
                # paid for silence — hand the allowance back. Deliberately broad:
                # every failure here is one where no post reached the user.
                log.exception("First post failed for user=%s", user.id)
                await _refund(db, user)
                await queue.put({"type": "error",
                                 "message": "We couldn't write your sample post. "
                                            "You can start creating anyway."})
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


async def _refund(db: AsyncSession, user: UserModel) -> Optional[None]:
    """Refunding must never be the reason a request 500s — the user has already
    lost their sample post; losing the error frame too would leave a spinner."""
    try:
        await free_generation.refund(db, user)
    except Exception:
        log.exception("Could not refund the free-post allowance for user=%s", user.id)
    return None
