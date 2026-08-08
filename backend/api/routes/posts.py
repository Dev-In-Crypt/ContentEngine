import asyncio
import io
import json
import logging
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Optional
from collections.abc import AsyncGenerator, Sequence

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import (
    build_content_engine, get_content_engine, get_current_user, get_db,
    get_effective_settings, get_settings, get_text_provider, load_brand_config,
    owned_post, require_local, require_token, require_verified,
)
from api.ratelimit import limiter
from api.routes.media import _owned_asset as _owned_media_asset
from services.brand_engine import PillowBrandEngine
from services.brand_voice import resolve_brand_voice
from services.managed_account import brand_for_post, resolve_active_account
from services.user_settings import (
    apply_brand_slide_style, build_settings_for_user, resolve_ai_choice,
    resolve_user_brand_voice, resolve_user_profile,
)
from config import Settings
from models.database import (
    Post as PostModel, Slide as SlideModel,
    PostInsight as PostInsightModel, User as UserModel,
)
from models.schemas import (
    CaptionUpdate, GenerateRequest, ImageSource, OverlayUpdateRequest, Platform,
    PostInsightSchema, PostPreview, PostStatus, PostSummary, PostVariant,
    RegenFieldRequest,
    RegenFieldResponse, ReelRequest, ReplaceSlideRequest, ScheduleRequest, SlidePreview,
    PlanItem, PlanRequest, PlanResponse, PublishResult, StagedUpload, UseAssetRequest,
    PostFormat, VideoPublishJobStatus, XPostMode, XStyle,
)
from services import free_generation, media_store, staging
from services.app_spend import flush_usage
from services.generation_credits import claim_generation_credentials
from services.openrouter import current_user_id
from services.publishing.factory import PUBLISHABLE_PLATFORMS
from services.claims import find_claims
from services.content_engine import ContentEngine, GeneratedPost, _num_slides
from services.content_plan import plan_topics
from services.pillars import (
    _PILLAR_BY_KEY, classify_pillar, pillar_mix, suggest_today,
)
from services.image_router import SlideImageConfig
from services.instagram import InstagramPublisher
from services.openrouter import OpenRouterError
from services.stock import StockError

router = APIRouter(prefix="/api/posts", tags=["posts"])
log = logging.getLogger(__name__)

UPLOADS_DIR = Path(__file__).parent.parent.parent / "uploads" / "posts"


def _preview_opts():
    """Eager-load options for the full PostPreview shape (slides)."""
    return (
        selectinload(PostModel.slides),
    )


async def _group_variants(db: AsyncSession, post: PostModel) -> list[PostVariant]:
    """Every post of this idea, for the result screen's tab bar.

    Scoped by user_id as well as by group: `variant_group_id` is a plain uuid
    with no owner of its own, so a row carrying someone else's key would
    otherwise be listed as a tab — putting a stranger's post id in the response
    and one click away from the editor.
    """
    group_id = post.variant_group_id or post.id
    rows = (await db.execute(
        select(PostModel)
        .where(PostModel.variant_group_id == group_id,
               PostModel.user_id == post.user_id)
        .order_by(PostModel.created_at.asc())
    )).scalars().all()
    if not rows:
        rows = [post]           # a hand-seeded row with no group of its own
    return [PostVariant(id=r.id, platform=r.platform or "instagram",
                        status=PostStatus(r.status)) for r in rows]


def _slide_path(post_id: str, slide_num: int) -> Path:
    return UPLOADS_DIR / post_id / f"slide_{slide_num}.jpg"


def _slide_raw_path(post_id: str, slide_num: int) -> Path:
    """Unbranded background, kept around so PUT /overlay can re-render without re-fetching."""
    return UPLOADS_DIR / post_id / f"slide_{slide_num}_raw.jpg"


def _build_slide_preview(post: PostModel, slide: SlideModel, cache_bust: bool = False) -> SlidePreview:
    """Single source of truth for SlidePreview shape, used by /generate, /regenerate,
    /upload, /overlay and the GET endpoints."""
    height = 1350 if (post.template_style or "branded_card") == "branded_card" else 1080
    rp = slide.render_params or {}
    url = f"/api/posts/{post.id}/slides/{slide.slide_number}/image"
    if cache_bust:
        url += f"?t={int(datetime.now(timezone.utc).timestamp())}"
    return SlidePreview(
        slide_number=slide.slide_number,
        image_url=url,
        image_source=ImageSource(slide.image_source),
        width=1080,
        height=height,
        attribution=slide.attribution,
        overlay_text=rp.get("overlay_text"),
        niche_text=rp.get("niche_text"),
        original_overlay_text=slide.original_overlay_text,
        original_niche_text=slide.original_niche_text,
        has_raw_image=bool(slide.raw_image_path and Path(slide.raw_image_path).exists()),
    )


def _to_preview(post: PostModel,
                variants: Sequence[PostVariant] = ()) -> PostPreview:
    slides = [
        _build_slide_preview(post, s)
        for s in sorted(post.slides, key=lambda s: s.slide_number)
    ]
    # Sentences the author should verify before posting, computed from the text as
    # it stands now — so a claim removed by an edit disappears on the next preview.
    # A thread carries its lines separately, so scan both.
    claim_source = "\n".join([post.caption or "", *(post.thread_parts or [])])
    # Business (Phase 4): LLM-verified claims + brand-rule flags, computed once at draft
    # time and stored on the post. Creator posts have no claim_check → these stay empty
    # (no LLM ever runs on the creator preview path).
    cc = post.claim_check if isinstance(post.claim_check, dict) else {}
    checked_claims = cc.get("claims") or []
    brand_flags = cc.get("brand") or {}
    fact_check = cc.get("check") or {}
    # The column can outlive the file — an uploads volume that wasn't persisted,
    # a restored database — and a preview pointing at a 404 is worse than one
    # that says "not rendered yet", so the file has to be there. Same check
    # _build_slide_preview makes for has_raw_image, for the same reason.
    video_url = None
    if post.video_path:
        vp = Path(post.video_path)
        if vp.exists():
            video_url = f"/api/posts/{post.id}/reel/video?t={int(vp.stat().st_mtime)}"
    return PostPreview(
        id=post.id,
        topic=post.topic,
        format=post.format,
        status=PostStatus(post.status),
        caption=post.caption or "",
        thread_parts=post.thread_parts or [],
        hashtags=post.hashtags or [],
        seo_keywords=post.seo_keywords or [],
        cta=post.cta or "",
        hook=post.hook or "",
        platform=Platform(post.platform or "instagram"),
        # Read straight through: the 4.1 backfill filled every existing row and
        # _persist fills every new one, so a COALESCE here would only hide the
        # day that stops being true.
        variant_group_id=post.variant_group_id,
        variants=list(variants),
        slides=slides,
        video_url=video_url,
        text_model_used=post.text_model or "",
        image_model_used=post.image_model,
        created_at=post.created_at or datetime.now(timezone.utc),
        sources=post.sources or [],
        claims=find_claims(claim_source),
        checked_claims=checked_claims,
        brand_flags=brand_flags,
        fact_check=fact_check,
        scheduled_at=post.scheduled_at,
        published_at=post.published_at,
        schedule_error=post.schedule_error,
        instagram_media_id=post.instagram_media_id,
    )


async def _persist(
    generated: GeneratedPost, db: AsyncSession, template_style: str = "branded_card",
    user_id: Optional[str] = None, managed_account_id: Optional[str] = None,
    tone: Optional[str] = None,
) -> PostModel:
    """The one place a Post row is built — the only one in api/, services/ or bot/.

    That is why phase 4's two columns are filled here and nowhere else: every
    creation path already runs through this function, so `variant_group_id`
    cannot be NULL on a new row however the post was made.
    """
    post_dir = UPLOADS_DIR / generated.id
    post_dir.mkdir(parents=True, exist_ok=True)

    db_post = PostModel(
        id=generated.id,
        user_id=user_id,
        managed_account_id=managed_account_id,
        topic=generated.topic,
        format=generated.format.value,
        status="preview",
        caption=generated.caption,
        thread_parts=generated.thread_parts or None,
        hashtags=generated.hashtags,
        seo_keywords=generated.seo_keywords,
        sources=generated.sources,
        cta=generated.cta,
        hook=generated.hook,
        alt_text=generated.alt_text,
        platform=generated.platform.value,
        template_style=template_style,
        text_model=generated.text_model_used,
        image_model=generated.image_model_used,
        pillar=classify_pillar(generated.topic, generated.caption),
        # A fresh idea is a group of one, keyed by its own id. Adapting to a
        # second network copies this key rather than minting a new one.
        variant_group_id=generated.id,
        # NULL when the caller never offered a choice (the Business draft path),
        # which is honest: nobody picked a tone, so there is none to preserve.
        tone=tone,
    )
    db.add(db_post)

    for slide in generated.slides:
        path = _slide_path(generated.id, slide.slide_number)
        path.write_bytes(slide.image_bytes)
        raw_path_str: Optional[str] = None
        if slide.raw_bytes:
            raw_path = _slide_raw_path(generated.id, slide.slide_number)
            raw_path.write_bytes(slide.raw_bytes)
            raw_path_str = str(raw_path)
        rp = slide.render_params or {}
        db.add(SlideModel(
            post_id=generated.id,
            slide_number=slide.slide_number,
            image_source=slide.image_source.value,
            image_path=str(path),
            search_query=slide.search_query,
            gen_prompt=slide.gen_prompt,
            attribution=slide.attribution,
            render_params=slide.render_params,
            raw_image_path=raw_path_str,
            original_overlay_text=rp.get("overlay_text"),
            original_niche_text=rp.get("niche_text"),
        ))

    await db.commit()

    result = await db.execute(
        select(PostModel)
        .where(PostModel.id == generated.id)
        .options(
            selectinload(PostModel.slides),
        )
    )
    return result.scalar_one()


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


async def _refund_free_generation(db: AsyncSession, user: UserModel) -> None:
    """Refunding must never be the reason a request dies: the user has already
    lost their post, and losing the error frame too would leave them looking at
    a spinner. Same shape as the onboarding route, for the same reason."""
    try:
        await free_generation.refund(db, user)
    except Exception:
        log.exception("Could not refund a free generation for user=%s", user.id)


def _require_text_provider(engine: ContentEngine, provider: Optional[str]) -> None:
    """Refuse before the model call when no client could be built for it.

    Naming a provider and a model costs nothing and stores nothing secret, so an
    account can sit in that state legitimately — picked GPT-4o, has not pasted
    the key yet. The model checks above pass (a model IS named) and the crash
    lands inside the generator as `'NoneType' object has no attribute
    'generate_text'`, which reaches the user as a failed generation rather than
    as the one sentence that fixes it.

    This became the ordinary way to arrive here when the platform key stopped
    filling the gap silently (UX phase 6.0) — before that, a configured .env
    quietly supplied a working client and nobody saw this path.
    """
    if engine.caption_gen.text_provider is not None:
        return
    named = f" for {provider}" if provider else ""
    raise HTTPException(
        status_code=400,
        detail=f"No API key{named}. Add it in Account → AI models.",
    )


@router.post("/generate")
@limiter.limit("15/minute;150/hour")
async def generate_post(
    request: Request,
    body: GenerateRequest,
    engine: Annotated[ContentEngine, Depends(get_content_engine)],
    settings: Annotated[Settings, Depends(get_settings)],
    effective: Annotated[Settings, Depends(get_effective_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> StreamingResponse:
    slide_configs: Optional[list[SlideImageConfig]] = None
    if body.slides:
        slide_configs = [
            SlideImageConfig(
                slide_number=s.slide_number,
                image_source=s.image_source,
                search_query=s.search_query,
                gen_prompt=s.gen_prompt,
                gen_model=s.gen_model,
                canva_template_id=s.canva_template_id,
                upload_id=s.upload_id,
                page_number=s.page_number,
            )
            for s in body.slides
        ]

    # Text-only is a pure-text post (no image). Instagram's API requires media, so
    # it only makes sense on X — refuse before spending a generation on it.
    if body.text_only and body.platform != Platform.X:
        raise HTTPException(
            status_code=422,
            detail="Text-only posts are supported on X only.",
        )

    # Own photos: one per slide, in the order they were picked. Refuse up front
    # rather than generating a post with holes in it. (Text-only has no slides.)
    if body.default_image_source == ImageSource.UPLOAD and not body.text_only:
        needed = _num_slides(body.format)
        if len(body.upload_ids) < needed:
            raise HTTPException(
                status_code=422,
                detail=(f"This format needs {needed} photo(s), "
                        f"but {len(body.upload_ids)} were uploaded."),
            )

    # Whose key writes this post. Their own if they have one — their provider,
    # their models, their bill, exactly as before. Otherwise this claims one of
    # the free generations (UX phase 6.2) and refuses in every case where it
    # cannot: no key anywhere, allowance spent, or our own daily ceiling reached.
    # It commits the claim, so from here on a failure owes them a refund.
    creds = await claim_generation_credentials(
        db, user, effective=effective, base=settings,
        text_model_override=body.text_model,
        image_model_override=body.image_model)
    if creds.on_our_key:
        # Built from OUR settings, choosing models as the platform would. The
        # caller is still `user`: their uploads and their brand, our bill.
        engine = build_content_engine(creds.settings, user, actor=None)
    text_model, image_model = creds.text_model, creds.image_model
    if not text_model:
        raise HTTPException(
            status_code=400,
            detail="No text model selected. Choose a provider and model in Account → AI models.",
        )
    _require_text_provider(engine, creds.text_provider)
    # Long-form X posts only exist for Premium accounts; X itself would reject the
    # tweet, so refuse before spending a generation on it.
    if (body.platform == Platform.X and body.x_mode == XPostMode.LONG
            and not getattr(user, "x_premium", False)):
        raise HTTPException(
            status_code=422,
            detail="Long X posts need X Premium. Enable it in Account, or pick Short or Thread.",
        )

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
                if creds.on_our_key:
                    # Our key, our bill. `record_usage` stamps whatever this
                    # holds, and the auth dependency set it to the caller —
                    # leaving it would put our spend on their usage dashboard,
                    # which contradicts the one thing we told them: you pay the
                    # vendor directly. Set inside the task, so only this
                    # generation is affected.
                    current_user_id.set(None)
                # Brand identity comes from the active profile — always a row
                # since UX phase 2, never the User's own columns. Keys and
                # x_premium stay on `user` (the owner's).
                acct = await resolve_active_account(db, user)
                brand_cfg = apply_brand_slide_style(
                    await load_brand_config(db, body.brand_config_id), acct,
                    is_local=bool(user.is_local))
                engine.brand_engine = PillowBrandEngine(brand_cfg)
                # Brand voice: the active brand's saved preset, optionally overridden for
                # this one post by body.brand_voice_preset (custom uses its saved text).
                if body.brand_voice_preset:
                    _custom = acct.brand_voice_custom if body.brand_voice_preset == "custom" else None
                    brand_voice = resolve_brand_voice(body.brand_voice_preset, _custom)
                else:
                    brand_voice = resolve_user_brand_voice(acct)
                # Fall back to the active brand's saved profile when the composer leaves
                # niche/audience blank; an explicit value in the request still wins.
                profile = resolve_user_profile(acct)
                niche = body.niche or profile["niche"]
                target_audience = body.target_audience or profile["target_audience"]
                generated = await engine.generate_post(
                    topic=body.topic,
                    format=body.format,
                    text_model=text_model,
                    image_model=image_model,
                    default_image_source=body.default_image_source,
                    text_only=body.text_only,
                    upload_ids=body.upload_ids,
                    slide_configs=slide_configs,
                    tone=body.tone,
                    niche=niche,
                    target_audience=target_audience,
                    additional_instructions=body.additional_instructions,
                    apply_branding=body.apply_branding,
                    platform=body.platform,
                    length_tier=body.length_tier,
                    template_style=body.template_style,
                    niche_box_color=body.niche_box_color,
                    show_logo=body.show_logo,
                    brand_voice=brand_voice,
                    brand_name=profile["brand_name"],
                    x_mode=body.x_mode,
                    x_style=body.x_style,
                    thread_min=body.thread_min,
                    thread_max=body.thread_max,
                    # Live web search is a surcharge per call, and a free trial
                    # post is not worth buying it. On their own key it stays on.
                    web_grounded=not creds.on_our_key,
                    progress=progress,
                )
                await progress("Saving to database...")
                db_post = await _persist(
                    generated, db, body.template_style.value,
                    user_id=user.id,
                    managed_account_id=acct.id,
                    tone=body.tone,
                )
                if body.plan_date is not None:
                    # A batch draft: pin it to its calendar date but leave it a
                    # preview — no publish job. The user reviews, then schedules.
                    db_post.scheduled_at = body.plan_date
                    await db.commit()
                preview = _to_preview(db_post, await _group_variants(db, db_post))
                # Persist any buffered LLM usage from this generation. On our key
                # the row lands un-attributed, which is what the daily ceiling
                # counts — so this is also how the next request learns the price
                # of this one.
                try:
                    await flush_usage(db)
                except Exception:
                    pass
                await queue.put({"type": "complete", "post": preview.model_dump(mode="json")})
            except Exception:
                # Log the detail server-side; don't leak internals (incl. upstream
                # API text) to the client.
                log.exception("Post generation failed")
                if creds.on_our_key:
                    # The allowance bought nothing: no post reached the user, so
                    # hand it back. Deliberately only on failure — a refund after
                    # a successful generation would mint free posts out of a
                    # retry, which is the direction that costs money.
                    await _refund_free_generation(db, user)
                await queue.put({"type": "error",
                                 "message": "Generation failed. Please try again."})
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


@router.get("", response_model=list[PostSummary])
async def list_posts(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None, description="filter by post status, e.g. 'failed'"),
) -> list[PostSummary]:
    # Paginated newest-first. Default 100 is generous enough that the SPA's
    # calendar/grid (which fetch without paging) keep working at small scale;
    # callers can page with ?limit=&offset= as volume grows.
    stmt = (select(PostModel).order_by(PostModel.created_at.desc())
            .options(selectinload(PostModel.slides)).limit(limit).offset(offset))
    if status is not None:
        # Reject an unknown value rather than returning []: a typo would otherwise
        # read as "you have no failed posts", which is the opposite of the truth.
        try:
            wanted = PostStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown status: {status!r}") from None
        stmt = stmt.where(PostModel.status == wanted.value)
    if not user.is_local:
        stmt = stmt.where(PostModel.user_id == user.id)
        # Agency multi-account (Phase 7): scope the view to the active brand. NULL =
        # Personal → only posts with no managed account. user_id stays the security gate.
        active = user.active_account_id
        stmt = stmt.where(PostModel.managed_account_id == active if active
                          else PostModel.managed_account_id.is_(None))
    result = await db.execute(stmt)
    posts = result.scalars().all()
    out = []
    for p in posts:
        first = min(p.slides, key=lambda s: s.slide_number) if p.slides else None
        thumb = f"/api/posts/{p.id}/slides/{first.slide_number}/image" if first else None
        out.append(PostSummary(
            id=p.id,
            topic=p.topic,
            format=p.format,
            status=PostStatus(p.status),
            platform=p.platform or "instagram",
            # Built field by field, so widening the schema alone changes nothing
            # here — the same trap that left `platform` and `published_url`
            # unsent in phase 3. Read straight through, no COALESCE: see _to_preview.
            variant_group_id=p.variant_group_id,
            thumb_url=thumb,
            scheduled_at=p.scheduled_at,
            published_at=p.published_at,
            published_url=p.published_url,
            created_at=p.created_at or datetime.now(timezone.utc),
            schedule_error=p.schedule_error,
        ))
    return out


@router.get("/{post_id}", response_model=PostPreview)
async def get_post(
    post_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> PostPreview:
    post = await owned_post(db, post_id, user, options=_preview_opts())
    return _to_preview(post, await _group_variants(db, post))


@router.put("/{post_id}/caption", response_model=PostPreview)
async def update_caption(
    post_id: str,
    update: CaptionUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> PostPreview:
    post = await owned_post(db, post_id, user, options=_preview_opts())
    if update.caption is not None:
        post.caption = update.caption
    if update.hashtags is not None:
        post.hashtags = update.hashtags
    if update.cta is not None:
        post.cta = update.cta
    if update.thread_parts is not None:
        post.thread_parts = update.thread_parts or None
        # keep the flattened caption in step with the edited tweets
        if update.thread_parts:
            post.caption = "\n\n".join(update.thread_parts)
    if update.seo_keywords is not None:
        post.seo_keywords = update.seo_keywords
    await db.commit()
    post = await owned_post(db, post_id, user, options=_preview_opts())
    return _to_preview(post)


@router.post("/{post_id}/export")
async def export_post(
    post_id: str,
    engine: Annotated[ContentEngine, Depends(get_content_engine)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> StreamingResponse:
    post = await owned_post(db, post_id, user, options=_preview_opts())

    images = []
    for slide in sorted(post.slides, key=lambda s: s.slide_number):
        p = Path(slide.image_path)
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"Image file missing for slide {slide.slide_number}")
        images.append(p.read_bytes())

    zip_bytes = await engine.exporter.export_package(
        images=images,
        caption=post.caption or "",
        hashtags=post.hashtags or [],
        post_name=(post.topic or "post")[:50],
    )
    filename = f"{(post.topic or 'post')[:40].replace(' ', '_')}_template.zip"
    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{post_id}/publish", response_model=PublishResult,
             dependencies=[Depends(require_verified)])
@limiter.limit("10/minute;60/hour")
async def publish_post(
    post_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> PublishResult:
    """Publish immediately: slides → imgbb (public URLs) → Instagram."""
    from services.publisher_flow import publish_now, PublishError
    from services.publishing.factory import PUBLISHABLE_PLATFORMS
    from services.scheduler import cancel_publish
    post = await owned_post(db, post_id, user)   # ownership gate before touching the job/publish
    if (post.platform or "instagram") not in PUBLISHABLE_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"Publishing to {post.platform} isn't available yet — export or copy the post.")
    # Business posts require a human sign-off: only an approved workspace post may publish
    # (no auto-publish without a person — doc §8/§13).
    # "failed" is allowed too: reaching it means the post was already approved and
    # the publish attempt itself broke. Without this a workspace post that hits a
    # transient network error is stuck forever — nothing transitions it back.
    if post.workspace_id and post.status not in ("approved", "failed"):
        raise HTTPException(status_code=409,
                            detail="This post must be approved before it can be published.")
    # Business publishing-frequency cap (doc §9): don't flood a channel.
    if post.workspace_id:
        from datetime import datetime, timezone
        from models.database import Workspace as WorkspaceModel
        from services.workspace import within_frequency_cap
        ws = await db.get(WorkspaceModel, post.workspace_id)
        reason = await within_frequency_cap(db, ws, datetime.now(timezone.utc)) if ws else None
        if reason:
            raise HTTPException(status_code=409, detail=reason)
    # Drop any pending scheduled job so it can't fire and double-publish.
    cancel_publish(post_id)
    sessionmaker = request.app.state.sessionmaker
    try:
        media_id = await publish_now(sessionmaker, post_id)
        row = await db.execute(select(PostModel.published_url).where(PostModel.id == post_id))
        return PublishResult(success=True, instagram_media_id=media_id,
                             published_url=row.scalar_one_or_none())
    except PublishError as e:
        # Publishing failed → signal it with a 502 (the post is marked failed in DB),
        # not a 200 with success=False, so the failure isn't mistaken for success.
        # PublishError carries our own, safe messages.
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception:
        log.exception("Publish failed: post=%s", post_id)
        raise HTTPException(status_code=502, detail="Publishing failed. Please try again.")


@router.post("/{post_id}/schedule", response_model=PostPreview,
             dependencies=[Depends(require_verified)])
async def schedule_post_endpoint(
    post_id: str,
    body: ScheduleRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> PostPreview:
    """Schedule a post for future publishing (10 min – 75 days ahead)."""
    from services.publishing.factory import PUBLISHABLE_PLATFORMS
    from services.scheduler import schedule_publish

    post = await owned_post(db, post_id, user, options=_preview_opts())
    if (post.platform or "instagram") not in PUBLISHABLE_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"Publishing to {post.platform} isn't available yet — export or copy the post.")

    when = body.publish_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = (when - now).total_seconds()
    if delta < 600:
        raise HTTPException(status_code=400, detail="Schedule time must be at least 10 minutes ahead")
    if delta > 75 * 24 * 3600:
        raise HTTPException(status_code=400, detail="Schedule time must be within 75 days")

    try:
        schedule_publish(post_id, when)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"Scheduler unavailable: {e}") from e

    post.status = "scheduled"
    post.scheduled_at = when
    post.schedule_error = None
    # A newly-chosen slot deserves a full retry budget; otherwise a post that
    # blipped once would give up early the next time it runs.
    post.publish_attempts = 0
    await db.commit()
    post = await owned_post(db, post_id, user, options=_preview_opts())
    return _to_preview(post)


@router.delete("/{post_id}/schedule", response_model=PostPreview)
async def unschedule_post(
    post_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> PostPreview:
    from services.scheduler import cancel_publish

    post = await owned_post(db, post_id, user, options=_preview_opts())
    cancel_publish(post_id)
    if post.status == "scheduled":
        post.status = "preview"
    post.scheduled_at = None
    await db.commit()
    post = await owned_post(db, post_id, user, options=_preview_opts())
    return _to_preview(post)


@router.get("/{post_id}/slides/{slide_num}/image")
async def get_slide_image(
    post_id: str,
    slide_num: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    # Intentionally UNGATED (like get_reel_video): a browser <img src> cannot send
    # the Bearer token, so the SPA relies on this being reachable without auth. The
    # URL carries an unguessable post UUID and the image becomes public on publish
    # anyway (same posture as the imgbb URLs). Post/list/usage isolation is
    # unaffected — only raw slide bytes are reachable by UUID.
    result = await db.execute(
        select(SlideModel)
        .where(SlideModel.post_id == post_id, SlideModel.slide_number == slide_num)
    )
    slide = result.scalar_one_or_none()
    if not slide:
        raise HTTPException(status_code=404, detail="Slide not found")
    path = Path(slide.image_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Image file not found on disk")
    return StreamingResponse(io.BytesIO(path.read_bytes()), media_type="image/jpeg")


# ─────────────────────────────────────────────────────────────────────────────
# Per-slide replace / upload (no need to regenerate the whole post)
# ─────────────────────────────────────────────────────────────────────────────

_MAX_UPLOAD_BYTES = 20 * 1024 * 1024     # 20 MB
_ACCEPTED_UPLOAD_TYPES = {"image/jpeg", "image/png", "image/webp"}
#: A carousel tops out at 10 slides, so nobody needs to stage more in one go.
_MAX_UPLOAD_FILES = 10


def _validated_upload(file: UploadFile, data: bytes) -> None:
    """The three checks every upload path here shares."""
    if file.content_type not in _ACCEPTED_UPLOAD_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type {file.content_type!r}. Allowed: jpeg, png, webp.",
        )
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 20 MB)")


@router.post("/uploads", response_model=list[StagedUpload])
async def stage_uploads(
    user: Annotated[UserModel, Depends(get_current_user)],
    files: list[UploadFile] = File(...),
) -> list[StagedUpload]:
    """Park the user's own photos so `generate` can refer to them by id.

    Generation streams over SSE with a JSON body, so the files cannot travel with
    it. They land in the tenant's staging folder and are swept after a day if the
    generation never happens.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(files) > _MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=422,
            detail=f"Too many files: {len(files)}. A carousel takes at most {_MAX_UPLOAD_FILES}.",
        )

    staged: list[StagedUpload] = []
    for file in files:
        data = await file.read()
        _validated_upload(file, data)
        upload_id = staging.save(str(user.id), data, file.content_type)
        staged.append(StagedUpload(id=upload_id, filename=file.filename or "", bytes=len(data)))
    return staged


async def _slide_with_post(
    db: AsyncSession, post_id: str, slide_num: int, user: UserModel,
) -> tuple[PostModel, SlideModel]:
    post = await owned_post(db, post_id, user, options=(selectinload(PostModel.slides),))
    slide = next((s for s in post.slides if s.slide_number == slide_num), None)
    if not slide:
        raise HTTPException(status_code=404, detail="Slide not found")
    return post, slide


def _rebrand_slide_bytes(
    raw_bytes: bytes,
    render_params: Optional[dict],
    brand_engine: PillowBrandEngine,
) -> bytes:
    """Re-apply the SAME branded card to a fresh image using stored render params.
    Falls back to unbranded JPEG bytes when params are missing (e.g. apply_branding=False)."""
    if not render_params or render_params.get("template_style") != "branded_card":
        return raw_bytes
    return brand_engine.create_branded_card(
        background_image=raw_bytes,
        niche_text=render_params.get("niche_text", ""),
        description_text=render_params.get("overlay_text", ""),
        niche_box_color=render_params.get("niche_box_color"),
        show_logo=render_params.get("show_logo"),
        show_niche_box=bool(render_params.get("show_niche_box", False)),
        page_number=render_params.get("page_number"),
        total_slides=render_params.get("total_slides"),
    )


@router.post(
    "/{post_id}/slides/{slide_num}/regenerate",
    response_model=SlidePreview,
)
async def regenerate_slide(
    post_id: str,
    slide_num: int,
    body: ReplaceSlideRequest,
    engine: Annotated[ContentEngine, Depends(get_content_engine)],
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> SlidePreview:
    """Replace a single slide's image (stock or AI) WITHOUT touching the rest of the post."""
    post, slide = await _slide_with_post(db, post_id, slide_num, user)

    # Build a SlideImageConfig from the existing slide, overridden by request body.
    image_source = body.image_source or ImageSource(slide.image_source)
    cfg = SlideImageConfig(
        slide_number=slide.slide_number,
        image_source=image_source,
        search_query=body.search_query or slide.search_query,
        stock_source=body.stock_source or "auto",
        gen_prompt=body.gen_prompt or slide.gen_prompt,
        gen_model=body.image_model or resolve_ai_choice(user, settings, "image")[1],
        page_number=slide.page_number,
    )

    try:
        result = await engine.image_router.fetch_image(cfg)
    except (OpenRouterError, StockError) as exc:
        raise HTTPException(status_code=502, detail=f"Image fetch failed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if isinstance(result, tuple):
        raw_bytes, attribution = result
    else:
        raw_bytes, attribution = result, None

    # Re-apply the same branded card with stored render params, under the brand
    # the post was made for — not whichever one the user has selected now.
    brand_cfg = apply_brand_slide_style(await load_brand_config(
        db, post.brand_engine if isinstance(post.brand_engine, str) and len(post.brand_engine) > 20 else None),
        await brand_for_post(db, post, user), is_local=bool(user.is_local))
    brand_engine = PillowBrandEngine(brand_cfg)
    branded = _rebrand_slide_bytes(raw_bytes, slide.render_params, brand_engine)

    # Overwrite the file and update the DB row.
    path = Path(slide.image_path) if slide.image_path else _slide_path(post.id, slide.slide_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(branded)
    # Persist the new raw background so PUT /overlay can re-render later.
    raw_path = _slide_raw_path(post.id, slide.slide_number)
    raw_path.write_bytes(raw_bytes)

    slide.image_source = image_source.value
    slide.search_query = cfg.search_query
    slide.gen_prompt = cfg.gen_prompt
    slide.attribution = attribution
    slide.raw_image_path = str(raw_path)
    await db.commit()
    await db.refresh(slide)
    return _build_slide_preview(post, slide, cache_bust=True)


@router.post(
    "/{post_id}/slides/{slide_num}/upload",
    response_model=SlidePreview,
)
async def upload_slide(
    post_id: str,
    slide_num: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    file: UploadFile = File(...),
) -> SlidePreview:
    """Replace a single slide with a user-uploaded image."""
    if file.content_type not in _ACCEPTED_UPLOAD_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type {file.content_type!r}. Allowed: jpeg, png, webp.",
        )
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 20 MB)")

    post, slide = await _slide_with_post(db, post_id, slide_num, user)

    brand_cfg = apply_brand_slide_style(
        await load_brand_config(db, None), await brand_for_post(db, post, user),
        is_local=bool(user.is_local))
    brand_engine = PillowBrandEngine(brand_cfg)
    branded = _rebrand_slide_bytes(raw_bytes, slide.render_params, brand_engine)

    path = Path(slide.image_path) if slide.image_path else _slide_path(post.id, slide.slide_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(branded)
    # Save the uploaded image as the new raw so PUT /overlay can re-brand later.
    raw_path = _slide_raw_path(post.id, slide.slide_number)
    raw_path.write_bytes(raw_bytes)

    # Custom upload — no stock attribution, no search query.
    slide.image_source = ImageSource.UPLOAD.value
    slide.search_query = None
    slide.gen_prompt = None
    slide.attribution = None
    slide.raw_image_path = str(raw_path)
    await db.commit()
    await db.refresh(slide)
    return _build_slide_preview(post, slide, cache_bust=True)


@router.post(
    "/{post_id}/slides/{slide_num}/from-library",
    response_model=SlidePreview,
)
async def slide_from_library(
    post_id: str,
    slide_num: int,
    body: UseAssetRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> SlidePreview:
    """Replace a slide with a COPY of a library asset's bytes.

    A copy, not a reference — same posture as upload_slide just above. Both
    orphan cleanup and GDPR erasure rmtree uploads/posts/<post_id>; a slide
    that merely pointed at the library file would let post cleanup delete a
    library asset out from under the user.
    """
    post, slide = await _slide_with_post(db, post_id, slide_num, user)
    asset = await _owned_media_asset(db, body.asset_id, user)
    if asset.kind != "image":
        raise HTTPException(status_code=400, detail="Only an image asset can replace a slide.")
    if asset.status != "ready":
        raise HTTPException(status_code=400, detail="That asset isn't ready yet.")
    raw_bytes = media_store.read(asset.user_id, asset.id)

    brand_cfg = apply_brand_slide_style(
        await load_brand_config(db, None), await brand_for_post(db, post, user),
        is_local=bool(user.is_local))
    brand_engine = PillowBrandEngine(brand_cfg)
    branded = _rebrand_slide_bytes(raw_bytes, slide.render_params, brand_engine)

    path = Path(slide.image_path) if slide.image_path else _slide_path(post.id, slide.slide_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(branded)
    # Save the library image as the new raw so PUT /overlay can re-brand later.
    raw_path = _slide_raw_path(post.id, slide.slide_number)
    raw_path.write_bytes(raw_bytes)

    # Same posture as a manual upload — no stock attribution, no search query.
    slide.image_source = ImageSource.UPLOAD.value
    slide.search_query = None
    slide.gen_prompt = None
    slide.attribution = None
    slide.raw_image_path = str(raw_path)
    slide.media_asset_id = asset.id
    await db.commit()
    await db.refresh(slide)
    return _build_slide_preview(post, slide, cache_bust=True)


@router.put(
    "/{post_id}/slides/{slide_num}/overlay",
    response_model=SlidePreview,
)
async def update_slide_overlay(
    post_id: str,
    slide_num: int,
    body: OverlayUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> SlidePreview:
    """Re-render the overlay (niche box + description box) on top of the slide's
    stored raw image — no new image fetch. Used when the user types a new
    overlay caption in the preview UI and hits Apply."""
    post, slide = await _slide_with_post(db, post_id, slide_num, user)

    if not slide.raw_image_path:
        raise HTTPException(
            status_code=409,
            detail="No raw background stored for this slide. Click Replace first.",
        )
    raw_path = Path(slide.raw_image_path)
    if not raw_path.exists():
        raise HTTPException(status_code=409, detail="Raw background file missing on disk.")
    raw_bytes = raw_path.read_bytes()

    # Merge new overlay/niche text into the stored render_params.
    rp = dict(slide.render_params or {})
    if body.overlay_text is not None:
        rp["overlay_text"] = body.overlay_text
    if body.niche_text is not None:
        rp["niche_text"] = body.niche_text

    brand_cfg = apply_brand_slide_style(
        await load_brand_config(db, None), await brand_for_post(db, post, user),
        is_local=bool(user.is_local))
    brand_engine = PillowBrandEngine(brand_cfg)
    branded = _rebrand_slide_bytes(raw_bytes, rp, brand_engine)

    path = Path(slide.image_path) if slide.image_path else _slide_path(post.id, slide.slide_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(branded)

    slide.render_params = rp
    await db.commit()
    await db.refresh(slide)
    return _build_slide_preview(post, slide, cache_bust=True)


# ─────────────────────────────────────────────────────────────────────────────
# Export-to-disk (saves the ZIP straight to the OS Downloads folder)
# ─────────────────────────────────────────────────────────────────────────────

def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\-.\s]", "", name).strip().replace(" ", "_")
    return name[:60] or "post"


def _unique_path(folder: Path, stem: str, suffix: str) -> Path:
    """Return folder/<stem><suffix> with _2 / _3 / … appended if needed."""
    candidate = folder / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{n}{suffix}"
        n += 1
    return candidate


@router.post("/{post_id}/export-to-disk", dependencies=[Depends(require_local)])
async def export_post_to_disk(
    post_id: str,
    engine: Annotated[ContentEngine, Depends(get_content_engine)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    """Build the ZIP and save it directly to ~/Downloads. Returns the absolute path.
    Used by the desktop window (pywebview) where blob downloads don't surface a
    Save-As dialog and end up in unclear locations."""
    post = await owned_post(db, post_id, user, options=(selectinload(PostModel.slides),))

    images: list[bytes] = []
    for slide in sorted(post.slides, key=lambda s: s.slide_number):
        p = Path(slide.image_path) if slide.image_path else None
        if not p or not p.exists():
            raise HTTPException(status_code=404, detail=f"Image file missing for slide {slide.slide_number}")
        images.append(p.read_bytes())

    zip_bytes = await engine.exporter.export_package(
        images=images,
        caption=post.caption or "",
        hashtags=post.hashtags or [],
        post_name=(post.topic or "post")[:50],
    )
    downloads = Path.home() / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(post.topic or "post")[:40]
    out = _unique_path(downloads, stem, "_template.zip")
    out.write_bytes(zip_bytes)
    return {"path": str(out), "filename": out.name, "size_bytes": len(zip_bytes)}


@router.post("/open-folder", dependencies=[Depends(require_token), Depends(require_local)])
async def open_folder(path: str = Body(..., embed=True)) -> dict:
    """Open the OS file explorer at the given file (highlighted) or directory.
    Only allowed for paths under Downloads (defence-in-depth — desktop-only API)."""
    target = Path(path).resolve()
    downloads = (Path.home() / "Downloads").resolve()
    try:
        target.relative_to(downloads)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path is outside the Downloads folder")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path does not exist")
    try:
        if sys.platform == "win32":
            # /select highlights the file in Explorer
            subprocess.Popen(["explorer", "/select,", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent if target.is_file() else target)])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not open: {exc}") from exc
    return {"opened": str(target)}


# ─────────────────────────────────────────────────────────────────────────────
# Insights (on-demand refresh + history)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{post_id}/insights/refresh", response_model=PostInsightSchema)
async def refresh_insights(
    post_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> PostInsightSchema:
    """Pull the latest Instagram metrics for a published post and store a snapshot."""
    post = await owned_post(db, post_id, user)
    if not post.instagram_media_id:
        raise HTTPException(status_code=409, detail="Post is not published to Instagram yet")
    # Use the caller's OWN Instagram credentials (multi-tenant), matching how the
    # post was published — not the platform .env global.
    settings = await build_settings_for_user(db, user)
    if not settings.instagram_access_token or not settings.instagram_user_id:
        raise HTTPException(status_code=409, detail="Instagram credentials not configured")

    publisher = InstagramPublisher(
        access_token=settings.instagram_access_token,
        ig_user_id=settings.instagram_user_id,
    )
    try:
        is_video = (post.format or "").startswith("reel") or "video" in (post.format or "")
        metrics = await publisher.get_insights(post.instagram_media_id, is_video=is_video)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Insights fetch failed: {e}") from e
    finally:
        await publisher.close()

    snap = PostInsightModel(
        post_id=post.id,
        reach=metrics.get("reach"),
        impressions=metrics.get("impressions") or metrics.get("views"),
        likes=metrics.get("likes"),
        comments=metrics.get("comments"),
        saved=metrics.get("saved"),
        shares=metrics.get("shares"),
        total_interactions=metrics.get("total_interactions"),
        plays=metrics.get("plays"),
        video_views=metrics.get("views"),
        raw=metrics.get("raw"),
    )
    db.add(snap)
    await db.commit()
    await db.refresh(snap)
    return PostInsightSchema.model_validate(snap)


@router.get("/{post_id}/insights", response_model=list[PostInsightSchema])
async def list_insights(
    post_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> list[PostInsightSchema]:
    await owned_post(db, post_id, user)   # ownership gate on the parent post
    result = await db.execute(
        select(PostInsightModel).where(PostInsightModel.post_id == post_id)
        .order_by(PostInsightModel.snapshot_at.desc())
    )
    return [PostInsightSchema.model_validate(r) for r in result.scalars().all()]


# ─────────────────────────────────────────────────────────────────────────────
# Regenerate a single field (caption / hook / cta / hashtags / seo_keywords)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{post_id}/regenerate-field", response_model=RegenFieldResponse)
@limiter.limit("15/minute;150/hour")
async def regenerate_field(
    post_id: str,
    request: Request,
    body: RegenFieldRequest,
    engine: Annotated[ContentEngine, Depends(get_content_engine)],
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> RegenFieldResponse:
    """Cheap targeted regeneration: returns N alternatives for one field.
    Does not persist — the client applies a chosen variant via PUT /caption."""
    post = await owned_post(db, post_id, user)

    current = {
        "caption": post.caption,
        "hook": post.hook,
        "cta": post.cta,
        "hashtags": post.hashtags or [],
        "seo_keywords": post.seo_keywords or [],
    }.get(body.field)

    _require_text_provider(engine, resolve_ai_choice(user, settings, "text")[0])
    try:
        variants = await engine.caption_gen.regenerate_field(
            field=body.field,
            topic=post.topic,
            current_value=current,
            caption=post.caption or "",
            platform=Platform(post.platform or "instagram"),
            tone="professional",
            text_model=post.text_model or resolve_ai_choice(user, settings, "text")[1],
            count=body.count,
            brand_voice=resolve_user_brand_voice(await brand_for_post(db, post, user)),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Regeneration failed: {e}") from e

    return RegenFieldResponse(field=body.field, variants=variants)


# ─────────────────────────────────────────────────────────────────────────────
# Adapt one idea to a second network
# ─────────────────────────────────────────────────────────────────────────────

#: Networks a sibling may be written for. Aliased to the publishable set rather
#: than restated: LinkedIn generates but has no publisher, so a LinkedIn sibling
#: would be a row that could never leave the Queue. When a publisher lands, this
#: stops being a restriction on its own.
ADAPTABLE_PLATFORMS = PUBLISHABLE_PLATFORMS


def _slides_for_platform(
    slides: list[SlideModel], platform: Platform, source_format: str,
) -> tuple[list[SlideModel], str]:
    """Which of the source's slides the sibling gets, and in what format.

    X collapses to one. It has never had more than a single image in this
    product — the composer already forces a carousel down to `single` the moment
    you switch it to X — so a ten-slide X post would be a shape neither the
    publisher nor the preview has ever seen.
    """
    ordered = sorted(slides, key=lambda s: s.slide_number)
    if platform is Platform.X:
        return ordered[:1], "single"
    return ordered, source_format


def _sibling_slides(
    source_slides: list[SlideModel], sibling_id: str, overlays: list[str],
    brand_engine: PillowBrandEngine,
) -> list[SlideModel]:
    """Copy the source's pictures into the sibling's own directory, re-rendering
    the overlay from the newly generated caption.

    The bytes are copied rather than the path shared: `regenerate_slide` and
    `PUT /overlay` write in place, so a shared file means editing one network's
    picture silently rewrites the other's — and deleting either post would
    orphan the other's pixels, since cleanup keys orphan directories on post id.

    Re-rendering rather than copying the finished JPEG is what keeps Instagram
    wording, written for a caption that no longer exists, off the X post. It
    needs the unbranded original: with no `raw_image_path` on disk there is
    nothing to re-render from, so the finished picture is copied as-is and the
    old overlay rides along. That is the honest degradation — losing the whole
    adaptation because a file is missing would be worse.
    """
    out: list[SlideModel] = []
    for i, src in enumerate(source_slides):
        num = i + 1
        dst = _slide_path(sibling_id, num)
        dst.parent.mkdir(parents=True, exist_ok=True)

        overlay = overlays[i] if i < len(overlays) else (src.original_overlay_text or "")
        params = dict(src.render_params or {})
        raw_src = Path(src.raw_image_path) if src.raw_image_path else None
        raw_dst: Optional[str] = None

        if raw_src and raw_src.exists():
            raw_bytes = raw_src.read_bytes()
            raw_dst_path = _slide_raw_path(sibling_id, num)
            raw_dst_path.write_bytes(raw_bytes)
            raw_dst = str(raw_dst_path)
            if overlay:
                params["overlay_text"] = overlay
            dst.write_bytes(_rebrand_slide_bytes(raw_bytes, params, brand_engine))
        else:
            src_path = Path(src.image_path) if src.image_path else None
            if src_path and src_path.exists():
                dst.write_bytes(src_path.read_bytes())
            else:
                continue                      # nothing on disk; the row would point at air
            overlay = src.original_overlay_text or ""

        out.append(SlideModel(
            post_id=sibling_id,
            slide_number=num,
            image_source=src.image_source,
            image_path=str(dst),
            raw_image_path=raw_dst,
            search_query=src.search_query,
            gen_prompt=src.gen_prompt,
            gen_model=src.gen_model,
            attribution=src.attribution,
            render_params=params,
            original_overlay_text=overlay,
            original_niche_text=src.original_niche_text,
            page_number=src.page_number,
            canva_template_id=src.canva_template_id,
            media_asset_id=src.media_asset_id,
        ))
    return out


async def _sibling_for(
    db: AsyncSession, group_id: str, platform: str, user_id: Optional[str],
) -> Optional[PostModel]:
    """The group's post for this network, if it already has one.

    One query answers two questions: adapting twice returns the first sibling
    instead of spending a second generation, and adapting a post to its OWN
    network returns the post itself — no separate branch for either.
    """
    stmt = (
        select(PostModel)
        .where(PostModel.variant_group_id == group_id,
               PostModel.platform == platform)
        .order_by(PostModel.created_at.asc())
        .options(*_preview_opts())
    )
    if user_id is not None:
        stmt = stmt.where(PostModel.user_id == user_id)
    return (await db.execute(stmt)).scalars().first()


@router.post("/{post_id}/adapt/{platform}", response_model=PostPreview)
@limiter.limit("15/minute;150/hour")
async def adapt_post(
    post_id: str,
    platform: Platform,
    request: Request,
    engine: Annotated[ContentEngine, Depends(get_content_engine)],
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> PostPreview:
    """Write the same idea for another network, as a sibling post.

    The sibling is an ordinary `Post` sharing the source's `variant_group_id`,
    which is the whole design: publishing, scheduling, business approval and
    analytics keep working on it with nothing changed. A child table holding
    per-network text would have meant rewriting every place that writes publish
    state.

    Costs one caption generation on the user's own key, so the SPA only calls it
    from an explicit "Adapt" button — never on hover, never prefetched.
    """
    source = await owned_post(db, post_id, user, options=_preview_opts())

    if platform.value not in ADAPTABLE_PLATFORMS:
        raise HTTPException(
            status_code=422,
            detail=f"{platform.value} posts can't be published yet, so there's "
                   f"nothing to adapt them for.",
        )
    # A Business draft carries an LLM verdict (claim_check) about the exact text
    # this would replace. Copying it attributes that verdict to words nobody
    # checked; dropping it lets an approver sign off on an unchecked draft.
    if source.workspace_id:
        raise HTTPException(
            status_code=422,
            detail="Business drafts can't be adapted yet — their fact check "
                   "belongs to the text that's already there.",
        )

    group_id = source.variant_group_id or source.id
    existing = await _sibling_for(db, group_id, platform.value, source.user_id)
    if existing is not None:
        return _to_preview(existing, await _group_variants(db, existing))

    text_model = source.text_model or resolve_ai_choice(user, settings, "text")[1]
    if not text_model:
        raise HTTPException(
            status_code=400,
            detail="No text model selected. Choose a provider and model in Account → AI models.",
        )
    _require_text_provider(engine, resolve_ai_choice(user, settings, "text")[0])

    acct = await brand_for_post(db, source, user)
    profile = resolve_user_profile(acct)
    target_slides, target_format = _slides_for_platform(
        list(source.slides), platform, source.format)

    try:
        caption = await engine.caption_gen.generate(
            topic=source.topic,
            format=PostFormat(target_format),
            num_slides=len(target_slides),
            text_model=text_model,
            tone=source.tone or "professional",
            niche=profile["niche"],
            target_audience=profile["target_audience"],
            brand_voice=resolve_user_brand_voice(acct),
            brand_name=profile["brand_name"],
            platform=platform,
            # One short post, never a thread. The user reaches a thread through
            # the composer's existing Split button, which also keeps this route
            # clear of the X-Premium gate that only the LONG mode needs.
            x_mode=XPostMode.SHORT,
            x_style=XStyle.STANDARD,
        )
    except Exception as e:
        log.exception("Adapt failed for post=%s platform=%s", post_id, platform.value)
        raise HTTPException(status_code=502, detail=f"Adaptation failed: {e}") from e

    # Two clicks a millisecond apart both miss the lookup above and both pay for
    # a generation. Re-checking here means the work is duplicated but the row
    # never is — and the second caller still gets a sibling back.
    raced = await _sibling_for(db, group_id, platform.value, source.user_id)
    if raced is not None:
        return _to_preview(raced, await _group_variants(db, raced))

    sibling_id = str(uuid.uuid4())
    sibling = PostModel(
        id=sibling_id,
        user_id=source.user_id,
        managed_account_id=source.managed_account_id,
        variant_group_id=group_id,
        topic=source.topic,
        format=target_format,
        status="preview",          # it has never been anywhere
        platform=platform.value,
        caption=caption.caption,
        thread_parts=caption.thread_parts or None,
        hashtags=caption.hashtags,
        seo_keywords=caption.seo_keywords,
        sources=caption.sources or source.sources,
        cta=caption.cta,
        hook=caption.hook,
        alt_text=caption.alt_text,
        tone=source.tone,
        template_style=source.template_style,
        brand_engine=source.brand_engine,
        text_model=text_model,
        image_model=source.image_model,
        pillar=classify_pillar(source.topic, caption.caption),
    )
    db.add(sibling)

    brand_cfg = apply_brand_slide_style(
        await load_brand_config(db, None), acct, is_local=bool(user.is_local))
    for slide in _sibling_slides(target_slides, sibling_id,
                                 caption.slide_overlays or [],
                                 PillowBrandEngine(brand_cfg)):
        db.add(slide)

    await db.commit()
    fresh = await owned_post(db, sibling_id, user, options=_preview_opts())
    return _to_preview(fresh, await _group_variants(db, fresh))


# ─────────────────────────────────────────────────────────────────────────────
# Batch: propose a week of topics (cheap — no posts created here)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/plan", response_model=PlanResponse)
@limiter.limit("15/minute;150/hour")
async def plan_week(
    request: Request,
    body: PlanRequest,
    text_provider: Annotated[object, Depends(get_text_provider)],
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> PlanResponse:
    """Propose `count` post topics, balanced across pillars and on-brand. Creates
    NO posts — the user reviews and prunes this list, then generates the approved
    topics one by one through the normal pipeline."""
    _tp, text_model, _key = resolve_ai_choice(user, settings, "text")
    if text_provider is None or not text_model:
        raise HTTPException(
            status_code=400,
            detail="No text model selected. Choose a provider and model in Account → AI models.",
        )
    # The db dependency exists only for this: topics have to be planned for the
    # brand the user is actually working in, which is a row now.
    brand = await resolve_active_account(db, user)
    profile = resolve_user_profile(brand)
    try:
        topics = await plan_topics(
            text_provider,
            niche=profile["niche"],
            target_audience=profile["target_audience"],
            theme=body.theme,
            platform=body.platform.value,
            count=body.count,
            text_model=text_model,
            brand_voice=resolve_user_brand_voice(brand),
        )
    except Exception as e:
        log.exception("Topic planning failed")
        raise HTTPException(status_code=502, detail="Could not plan topics. Try again.") from e

    items = [
        PlanItem(
            topic=t["topic"],
            pillar=t["pillar"],
            pillar_label=_PILLAR_BY_KEY.get(t["pillar"], {}).get("label", t["pillar"]),
            angle=t["angle"],
            date=body.start_date + timedelta(days=i * body.cadence_days),
        )
        for i, t in enumerate(topics)
    ]
    return PlanResponse(items=items)


# ─────────────────────────────────────────────────────────────────────────────
# Content pillars mix + "what to post today"
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/pillars/mix")
async def pillars_mix(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    stmt = select(PostModel.pillar, PostModel.topic, PostModel.caption)
    if not user.is_local:
        stmt = stmt.where(PostModel.user_id == user.id)
        active = user.active_account_id                       # Phase 7: scope to active brand
        stmt = stmt.where(PostModel.managed_account_id == active if active
                          else PostModel.managed_account_id.is_(None))
    result = await db.execute(stmt)
    rows = result.all()
    pillars = [
        (p if p else classify_pillar(topic, caption))
        for (p, topic, caption) in rows
    ]
    mix = pillar_mix(pillars)
    return {"pillars": mix, "suggestion": suggest_today(mix), "total": len(pillars)}


# ─────────────────────────────────────────────────────────────────────────────
# Reels — render a vertical video from the post's slides (Ken Burns), serve it,
# and publish it to Instagram (cloud mode, where the video URL is public).
# ─────────────────────────────────────────────────────────────────────────────

def _reel_path(post_id: str) -> Path:
    return UPLOADS_DIR / post_id / "reel.mp4"


@router.post("/{post_id}/reel")
@limiter.limit("6/minute;30/hour")
async def make_reel(
    post_id: str,
    request: Request,
    settings: Annotated[Settings, Depends(get_effective_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    text_provider: Annotated[object, Depends(get_text_provider)],
    body: Optional[ReelRequest] = None,
) -> dict:
    """Render a Reel MP4 from the post's slides and store it on disk. With
    `voiceover` the reel gets TTS narration (ElevenLabs, the user's key), each
    slide lasts exactly its narration segment, and subtitles are burned in."""
    from services.video import get_video_provider, VideoError

    post = await owned_post(db, post_id, user, options=(selectinload(PostModel.slides),))

    slides = sorted(post.slides, key=lambda s: s.slide_number)
    images: list[bytes] = []
    overlays: list[str] = []
    for s in slides:
        p = Path(s.image_path) if s.image_path else None
        if not p or not p.exists():
            raise HTTPException(status_code=404, detail=f"Image missing for slide {s.slide_number}")
        images.append(p.read_bytes())
        rp = s.render_params or {}
        overlays.append(rp.get("overlay_text") or "")
    if not images:
        raise HTTPException(status_code=400, detail="No slides to build a reel from")

    opts = body or ReelRequest()
    provider = get_video_provider(settings.video_provider)

    if opts.visuals == "broll" and not opts.voiceover:
        raise HTTPException(status_code=400,
                            detail="Stock b-roll needs voiceover — tick 🎙 first.")

    if not opts.voiceover:
        try:
            mp4 = await provider.make_reel(images, overlays=overlays, duration_per=3.0)
        except VideoError as e:
            raise HTTPException(status_code=502, detail=f"Reel render failed: {e}") from e
        extra: dict = {}
    else:
        mp4, total, credits = await _make_voiceover_reel(
            settings=settings, user=user, text_provider=text_provider, post=post,
            provider=provider, images=images, overlays=overlays,
            voice_id=(opts.voice_id or "").strip(), visuals=opts.visuals,
            music=opts.music, cover=opts.cover)
        extra = {"voiceover": True, "duration_sec": round(total, 2)}
        if credits:
            # Pexels attribution rides the existing sources panel in the UI.
            post.sources = (post.sources or []) + credits
            extra["broll_clips"] = len(credits)
        if opts.visuals == "broll":
            # Tell the UI how many scenes fell back to a slide so it can warn the
            # user their stock clips didn't all land (vs. silently degrading).
            extra["broll_clips"] = len(credits)
            extra["broll_fallbacks"] = len(images) - len(credits)

    path = _reel_path(post.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(mp4)
    post.video_path = str(path)
    await db.commit()
    ts = int(datetime.now(timezone.utc).timestamp())
    return {"video_url": f"/api/posts/{post.id}/reel/video?t={ts}",
            "size_bytes": len(mp4), **extra}


@router.put("/{post_id}/reel/from-library")
async def reel_from_library(
    post_id: str,
    body: UseAssetRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    """Attach a library video to this post's Reel slot — the same slot a
    rendered Reel occupies, so GET .../reel/video and POST .../publish-reel
    work on the result unchanged. A copy, not a reference, for the same reason
    as slide_from_library above."""
    post = await owned_post(db, post_id, user)
    asset = await _owned_media_asset(db, body.asset_id, user)
    if asset.kind != "video":
        raise HTTPException(status_code=400, detail="Only a video asset can become a Reel.")
    if asset.status != "ready":
        raise HTTPException(status_code=400, detail="That asset isn't ready yet.")
    data = media_store.read(asset.user_id, asset.id)

    path = _reel_path(post.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    post.video_path = str(path)
    post.video_asset_id = asset.id
    await db.commit()
    ts = int(datetime.now(timezone.utc).timestamp())
    return {"video_url": f"/api/posts/{post.id}/reel/video?t={ts}", "size_bytes": len(data)}


async def _make_voiceover_reel(*, settings, user, text_provider, post, provider,
                               images, overlays, voice_id, visuals: str = "slides",
                               music: bool = False,
                               cover: bool = False) -> tuple[bytes, float, list[dict]]:
    """Script → TTS → visuals (slides OR stock b-roll) → [cover] → ASS → mux with
    voice or a ducked voice+music mix. Returns (mp4 bytes, total duration, b-roll
    credits). Temp files cleaned in finally; b-roll degrades per segment."""
    import shutil as _shutil
    import tempfile as _tempfile

    from services import music_store
    from services.caption_generator import CaptionParseError
    from services.reel_script import build_voiceover_script
    from services.subtitles import chunk_segments, write_ass
    from services.tts import (
        ElevenLabsTTS, TTSError, concat_wavs, mix_with_music, mp3_to_wav,
    )
    from services.video import VideoError
    from services.video.assemble import mux_reel, prepend_cover, render_cover

    if text_provider is None:
        raise HTTPException(
            status_code=400,
            detail="Voiceover needs a text model — choose one in Account → AI models.")
    if not settings.elevenlabs_api_key:
        raise HTTPException(
            status_code=400,
            detail="Voiceover needs an ElevenLabs API key — add it in Account → API keys.")
    if visuals == "broll" and not settings.pexels_api_key:
        raise HTTPException(
            status_code=400,
            detail="Stock b-roll needs a Pexels API key — add it in Account → API keys.")
    music_path = music_store.path_for(str(user.id)) if music else None
    if music and music_path is None:
        raise HTTPException(
            status_code=400,
            detail="Background music needs an uploaded track — add one in Account.")
    _tp, text_model, _key = resolve_ai_choice(user, settings, "text")
    voice = voice_id or settings.elevenlabs_voice_id
    gap = 0.35

    tmpdir = Path(_tempfile.mkdtemp(prefix="reelvo_"))
    try:
        try:
            segments = await build_voiceover_script(
                text_provider, topic=post.topic or "", caption=post.caption or "",
                slide_texts=overlays, text_model=text_model or "")
        except CaptionParseError as e:
            raise HTTPException(status_code=502,
                                detail=f"Voiceover script failed: {e}") from e

        try:
            tts = ElevenLabsTTS(settings.elevenlabs_api_key,
                                ssl_verify=settings.ssl_verify)
            wavs: list[Path] = []
            durations: list[float] = []
            for i, seg in enumerate(segments):
                mp3 = await tts.synthesize(seg.text, voice_id=voice)
                wav = tmpdir / f"seg_{i:02d}.wav"
                durations.append(await mp3_to_wav(mp3, wav))
                wavs.append(wav)
            track = tmpdir / "voice.m4a"
            total = await concat_wavs(wavs, track, gap_sec=gap)
        except TTSError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        slide_durs = [d + gap for d in durations]
        video_tmp = tmpdir / "silent.mp4"
        credits: list[dict] = []
        if visuals == "broll":
            credits = await _build_broll_video(
                settings=settings, text_provider=text_provider, provider=provider,
                segments=segments, slide_durs=slide_durs, images=images,
                overlays=overlays, tmpdir=tmpdir, out_path=video_tmp)
        else:
            try:
                # No overlays: the burned subtitles are the only text now (the
                # slide already carries its headline). fit="pad" keeps the whole
                # 4:5 slide visible in 9:16 instead of cropping its sides.
                silent = await provider.make_reel(images, overlays=None,
                                                  duration_per=slide_durs, fit="pad")
            except VideoError as e:
                raise HTTPException(status_code=502,
                                    detail=f"Reel render failed: {e}") from e
            video_tmp.write_bytes(silent)

        if cover:
            # slide 1 REPLACES the first 0.5s — voice/subs stay at t=0
            try:
                cover_mp4 = tmpdir / "cover.mp4"
                await render_cover(images[0], cover_mp4)
                covered = tmpdir / "covered.mp4"
                await prepend_cover(cover_mp4, video_tmp, covered, sum(slide_durs))
                video_tmp = covered
            except VideoError as e:
                raise HTTPException(status_code=502,
                                    detail=f"Cover render failed: {e}") from e

        audio_in = track
        if music_path is not None:
            try:
                mixed = tmpdir / "mixed.m4a"
                await mix_with_music(track, music_path, mixed, total_dur=total)
                audio_in = mixed
            except TTSError as e:
                raise HTTPException(status_code=502,
                                    detail=f"Music mix failed: {e}") from e

        ass_path = tmpdir / "subs.ass"
        # Time subtitles to the clean speech durations, but advance the segment
        # clock by slide_durs (speech + gap) so subs hug the voice and don't
        # linger through the silent gap between segments.
        ass_path.write_text(
            write_ass(chunk_segments([s.text for s in segments], durations,
                                     advance_durs=slide_durs)),
            encoding="utf-8")
        out_tmp = tmpdir / "reel.mp4"
        try:
            await mux_reel(video_tmp, audio_in, ass_path, out_tmp)
        except VideoError as e:
            raise HTTPException(status_code=502,
                                detail=f"Reel assembly failed: {e}") from e
        return out_tmp.read_bytes(), total, credits
    finally:
        _shutil.rmtree(tmpdir, ignore_errors=True)


async def _build_broll_video(*, settings, text_provider, provider, segments,
                             slide_durs, images, overlays, tmpdir: Path,
                             out_path: Path) -> list[dict]:
    """One stock clip per narration segment (search → judge → download →
    normalize); any per-segment failure falls back to rendering that segment
    from its slide. Segments are joined with SYNC-PRESERVING crossfades: every
    clip except the last renders `XFADE_SEC` longer, the fade consumes exactly
    that surplus, so the timeline still matches the voice/subtitles. Returns
    Pexels credits for the clips actually used."""
    from services.broll import PexelsAuthError, PexelsVideoSearch, pick_with_judge
    from services.video import VideoError
    from services.video.normalize import (
        XFADE_SEC, align_to_duration, concat_clips_xfade, normalize_clip,
    )

    search = PexelsVideoSearch(settings.pexels_api_key,
                               ssl_verify=settings.ssl_verify)
    clip_paths: list[Path] = []
    credits: list[dict] = []
    n = len(segments)
    for i, seg in enumerate(segments):
        dur = slide_durs[i]
        render_dur = dur + (XFADE_SEC if i < n - 1 else 0.0)   # fade surplus
        clip = tmpdir / f"clip_{i:02d}.mp4"
        used_broll = False
        try:
            cands = await search.candidates(seg.query, dur)
            cand = await pick_with_judge(
                text_provider, cands, segment_text=seg.text, query=seg.query,
                judge_model=settings.broll_judge_model)
            if cand is not None:
                raw = tmpdir / f"raw_{i:02d}.mp4"
                await search.download(cand.url, raw)
                await normalize_clip(raw, clip, target_duration=render_dur,
                                     segment_id=i + 1)
                raw.unlink(missing_ok=True)
                credits.append({"title": f"Pexels video #{cand.video_id}",
                                "url": cand.page_url})
                used_broll = True
        except PexelsAuthError as e:
            # A bad key would fail every segment — surface it once, don't render
            # a whole silent-fallback reel the user thinks is "b-roll".
            raise HTTPException(
                status_code=400,
                detail="Pexels rejected the API key — check it in Account → "
                       "API keys.") from e
        except Exception as e:  # noqa: BLE001 — b-roll degrades, never crashes
            log.warning("B-roll segment %d failed (%s); falling back to slide", i, e)
        if not used_broll:
            # graceful fallback: this segment shows its slide (no overlay — the
            # burned subtitles carry the text), padded to match the timeline.
            idx = min(i, len(images) - 1)
            silent = await provider.make_reel(
                [images[idx]], overlays=None,
                duration_per=[render_dur], fit="pad")
            clip.write_bytes(silent)
        clip_paths.append(clip)
    try:
        joined = tmpdir / "joined.mp4"
        await concat_clips_xfade(clip_paths, slide_durs, joined)
        await align_to_duration(joined, out_path, sum(slide_durs))
    except VideoError as e:
        raise HTTPException(status_code=502,
                            detail=f"B-roll assembly failed: {e}") from e
    return credits


@router.get("/{post_id}/reel/video")
async def get_reel_video(
    post_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FileResponse:
    # Intentionally UNGATED: Instagram's servers fetch this URL directly (no auth
    # header possible) when publishing a Reel in cloud mode. The post_id is an
    # unguessable UUID and the content is about to be public — same posture as the
    # imgbb public image URLs used for photo publishing.
    post = await db.get(PostModel, post_id)
    if not post or not post.video_path:
        raise HTTPException(status_code=404, detail="Reel not rendered yet")
    p = Path(post.video_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Reel file missing on disk")
    return FileResponse(str(p), media_type="video/mp4", filename="reel.mp4")


@router.post("/{post_id}/publish-reel", response_model=PublishResult,
             dependencies=[Depends(require_verified)])
@limiter.limit("10/minute;60/hour")
async def publish_reel(
    post_id: str,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> PublishResult:
    """Publish the rendered Reel to Instagram. Requires a publicly reachable
    video URL — only works in cloud mode (PUBLIC_BASE_URL set)."""
    from services.publisher_flow import publish_reel_now, PublishError

    post = await owned_post(db, post_id, user)
    if not post.video_path:
        raise HTTPException(status_code=409, detail="Render the reel first (Make Reel)")

    base = (settings.public_base_url or "").rstrip("/")
    if not base:
        raise HTTPException(
            status_code=409,
            detail="Reel publishing needs a public video URL. Set PUBLIC_BASE_URL "
                   "(cloud mode) — Instagram cannot fetch a video from localhost.",
        )
    video_url = f"{base}/api/posts/{post_id}/reel/video"
    try:
        media_id = await publish_reel_now(request.app.state.sessionmaker, post_id, video_url)
        return PublishResult(success=True, instagram_media_id=media_id)
    except PublishError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/{post_id}/publish-video", response_model=VideoPublishJobStatus,
             status_code=202, dependencies=[Depends(require_verified)])
@limiter.limit("10/minute;60/hour")
async def publish_video_post(
    post_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> VideoPublishJobStatus:
    """Publish an X post's rendered Reel to X. Unlike publish_reel (Instagram,
    which pulls the MP4 from a public URL), X takes the bytes directly and
    processes asynchronously — this enqueues a job the poller in
    services/x_video_publish.py drives, rather than publishing in-request."""
    from api.routes.publish_jobs import build_job_status
    from services.publisher_flow import build_x_text
    from services.scheduler import cancel_publish
    from services.user_settings import settings_for_post_owner
    from services.x_video_publish import XVideoRejected, enqueue, validate_video_for_x

    post = await owned_post(db, post_id, user)
    if (post.platform or "instagram") != "x":
        raise HTTPException(status_code=400,
                            detail="This post is for Instagram — use Publish Reel.")
    if not post.video_path:
        raise HTTPException(status_code=409, detail="Render the reel first (Make Reel)")
    if post.status == "published" and post.instagram_media_id:
        raise HTTPException(status_code=409, detail="Already published.")

    # Same two Business gates as publish_post: a human sign-off before
    # anything goes out, and a cap so a channel isn't flooded.
    if post.workspace_id and post.status not in ("approved", "failed"):
        raise HTTPException(status_code=409,
                            detail="This post must be approved before it can be published.")
    if post.workspace_id:
        from models.database import Workspace as WorkspaceModel
        from services.workspace import within_frequency_cap
        ws = await db.get(WorkspaceModel, post.workspace_id)
        reason = await within_frequency_cap(db, ws, datetime.now(timezone.utc)) if ws else None
        if reason:
            raise HTTPException(status_code=409, detail=reason)

    settings = await settings_for_post_owner(db, post)
    if not all((settings.x_api_key, settings.x_api_secret,
                settings.x_access_token, settings.x_access_token_secret)):
        raise HTTPException(status_code=400,
                            detail="X (Twitter) API credentials not configured")

    path = Path(post.video_path)
    try:
        warning = await validate_video_for_x(path)
    except XVideoRejected as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    owner = await db.get(UserModel, post.user_id) if post.user_id else None
    caption, thread_parts, long_form = build_x_text(post, owner)

    # A pending scheduled (photo) publish for this post must not fire later
    # and double-post now that a video publish is in flight.
    cancel_publish(post_id)

    job = await enqueue(
        db, user_id=user.id, video_path=str(path), total_bytes=path.stat().st_size,
        post_id=post.id, caption=caption, thread_parts=thread_parts or None,
        alt_text=post.alt_text, long_form=long_form,
    )
    await db.commit()
    return build_job_status(job, warning=warning)


class VerifyPostRequest(BaseModel):
    # Optional: the author's own source, pasted. Most creator posts cite nothing,
    # and this is the only way such a post can be checked at all.
    source_text: str = Field("", max_length=20000)


@router.post("/{post_id}/verify", response_model=PostPreview)
async def verify_post_claims(
    post_id: str,
    body: VerifyPostRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    engine: Annotated[ContentEngine, Depends(get_content_engine)],
    settings: Annotated[Settings, Depends(get_effective_settings)],
) -> PostPreview:
    """Check this post's factual claims against real source material. Opt-in.

    Business drafts are verified automatically because they are written *from* a
    source. A creator post is written from a topic, so the material has to be
    assembled here: the pages a web-grounded model cited, plus anything the author
    pastes. With neither, the result is an honest "nothing to check against" —
    we do not ask the model to grade its own work from memory.
    """
    from services.fact_check import verify_post

    post = await owned_post(db, post_id, user, options=_preview_opts())
    draft_text = "\n".join([post.caption or "", *(post.thread_parts or [])]).strip()
    urls = [s.get("url") for s in (post.sources or []) if isinstance(s, dict)]
    _p, text_model, _k = resolve_ai_choice(user, settings, "text")

    result = await verify_post(
        getattr(engine.caption_gen, "text_provider", None), draft_text=draft_text,
        source_urls=urls, pasted=body.source_text, text_model=text_model,
        ssl_verify=settings.ssl_verify)

    # Keep any brand flags a Business draft already carries — this endpoint only
    # owns the claim side.
    existing = post.claim_check if isinstance(post.claim_check, dict) else {}
    post.claim_check = {**existing, "claims": result["claims"],
                        "check": {k: v for k, v in result.items() if k != "claims"}}
    await db.commit()
    await db.refresh(post)
    return _to_preview(post)
