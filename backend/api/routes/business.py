"""Business module API — sources + leads feed (Phase 2), draft + digest (Phase 3).

Every route is gated by require_business and scoped to the caller's single
workspace (resolved lazily). Ownership is enforced by workspace_id filters +
owned_source/owned_lead (404 on a miss) — the isolation seam is tested with a
mutation. The background poller is rules-only; the LLM spend lives in Phase 3's
draft/digest routes, which run on the user's own key via the normal engine.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_content_engine, get_current_user, get_db, get_settings, require_business
from api.routes.posts import _persist, _sse, _to_preview
from config import Settings
from models.database import AuditEntry as AuditEntryModel
from models.database import BrandRules as BrandRulesModel
from models.database import Lead as LeadModel
from models.database import Post as PostModel
from models.database import PostInsight as PostInsightModel
from models.database import Source as SourceModel
from models.database import User as UserModel
from models.schemas import (
    BrandRulesOut, BrandRulesUpdate, DigestRequest, DraftEditRequest, LeadOut, LimitsOut,
    LimitsUpdate, PostFormat, Platform, XPostMode, XStyle,
    SourceAnalyticsOut, SourceAnalyticsResponse,
    SourceCreate, SourceOut,
)
from services import milestones
from services.claim_check import apply_brand_rules, verify_claims
from services.content_engine import ContentEngine
from services.source_poller import poll_source
from services.sources import (
    SourceFetchError, SourceRateLimited, detect_source_type,
)
from services.user_settings import resolve_ai_choice
from services.workspace import (
    get_or_create_workspace, owned_business_post, owned_lead, owned_source,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/business", tags=["business"],
                   dependencies=[Depends(require_business)])


def _source_out(s: SourceModel) -> SourceOut:
    return SourceOut(id=s.id, url=s.url, kind=s.kind, status=s.status,
                     last_checked_at=s.last_checked_at, created_at=s.created_at)


def _lead_out(lead: LeadModel) -> LeadOut:
    missing = lead.missing if isinstance(lead.missing, list) else None
    return LeadOut(
        id=lead.id, what_happened=lead.what_happened, source_url=lead.source_url,
        quote=lead.quote, published_at=lead.published_at, why_interesting=lead.why_interesting,
        strength=lead.strength, reason=lead.reason, missing=missing,
        sensitive=bool(lead.sensitive), status=lead.status, created_at=lead.created_at)


# ── Sources ──────────────────────────────────────────────────────────────────

@router.post("/sources", response_model=dict)
async def add_source(
    body: SourceCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    ws = await get_or_create_workspace(db, user)
    kind = detect_source_type(body.url)
    source = SourceModel(workspace_id=ws.id, url=body.url, kind=kind, status="ok", active=True)
    db.add(source)
    await db.commit()
    await db.refresh(source)
    # Prime the feed immediately so it isn't empty until the next hourly poll.
    leads_found = 0
    try:
        leads_found = await poll_source(db, source, ssl_verify=settings.ssl_verify)
        await db.commit()
    except SourceRateLimited as e:
        # Before SourceFetchError, its parent. Being over quota is temporary, and
        # recording it as "unreachable" invites the user to delete a source that
        # works — the SPA already has a badge for this, it was just never set here.
        log.warning("Rate limited on first poll of %s: %s", body.url, e)
        source.status = "rate_limited"
        await db.commit()
    except SourceFetchError as e:
        log.warning("Initial poll failed for %s: %s", body.url, e)
        source.status = "unreachable"
        await db.commit()
    return {"source": _source_out(source).model_dump(mode="json"), "leads_found": leads_found}


@router.get("/sources", response_model=list[SourceOut])
async def list_sources(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> list[SourceOut]:
    ws = await get_or_create_workspace(db, user)
    rows = (await db.execute(
        select(SourceModel).where(SourceModel.workspace_id == ws.id)
        .order_by(SourceModel.created_at.desc())
    )).scalars().all()
    return [_source_out(s) for s in rows]


@router.delete("/sources/{source_id}", response_model=dict)
async def delete_source(
    source_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    ws = await get_or_create_workspace(db, user)
    source = await owned_source(db, source_id, ws)
    await db.delete(source)
    await db.commit()
    return {"status": "deleted"}


@router.post("/sources/{source_id}/refresh", response_model=dict)
async def refresh_source(
    source_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    ws = await get_or_create_workspace(db, user)
    source = await owned_source(db, source_id, ws)
    try:
        leads_found = await poll_source(db, source, ssl_verify=settings.ssl_verify)
        await db.commit()
    except SourceRateLimited as e:
        log.warning("Rate limited refreshing %s: %s", source.url, e)
        source.status = "rate_limited"
        await db.commit()
        # 503, not 429: our own limiter already answers 429 to mean "you are
        # clicking too fast", and reusing it for "the source's quota" would put
        # two different problems behind one code — the confusion this fixes.
        raise HTTPException(
            status_code=503,
            detail="That source is over its request quota. It'll be retried "
                   "automatically — nothing to fix.") from e
    except SourceFetchError as e:
        log.warning("Refresh failed for %s: %s", source.url, e)
        source.status = "unreachable"
        await db.commit()
        raise HTTPException(status_code=502, detail="Couldn't reach that source.") from e
    return {"leads_found": leads_found}


# ── Source analytics (Phase 8) ───────────────────────────────────────────────

@router.get("/source-analytics", response_model=SourceAnalyticsResponse)
async def source_analytics(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> SourceAnalyticsResponse:
    """Per-source pipeline funnel — which sources yield the best posts. Ranks on
    leads → worthy → drafts → approved/published and, when a Business post has been
    published + its insights refreshed, real engagement (reach + likes/comments/
    saves/shares from the latest PostInsight snapshot; Instagram-only, so sparse).
    Attribution is Post.lead_id → Lead.source_id; digest posts (lead_id NULL) span
    many leads and are reported separately. All scoped to the caller's workspace."""
    ws = await get_or_create_workspace(db, user)

    strength_rows = (await db.execute(
        select(LeadModel.source_id, LeadModel.strength, func.count())
        .where(LeadModel.workspace_id == ws.id)
        .group_by(LeadModel.source_id, LeadModel.strength)
    )).all()
    lstatus_rows = (await db.execute(
        select(LeadModel.source_id, LeadModel.status, func.count())
        .where(LeadModel.workspace_id == ws.id)
        .group_by(LeadModel.source_id, LeadModel.status)
    )).all()
    last_lead_rows = (await db.execute(
        select(LeadModel.source_id, func.max(LeadModel.created_at))
        .where(LeadModel.workspace_id == ws.id).group_by(LeadModel.source_id)
    )).all()
    # posts traced back to their source through the lead
    post_rows = (await db.execute(
        select(LeadModel.source_id, PostModel.status, func.count())
        .select_from(PostModel).join(LeadModel, PostModel.lead_id == LeadModel.id)
        .where(PostModel.workspace_id == ws.id)
        .group_by(LeadModel.source_id, PostModel.status)
    )).all()
    digests = (await db.execute(
        select(func.count()).select_from(PostModel).where(
            PostModel.workspace_id == ws.id, PostModel.lead_id.is_(None),
            PostModel.source_kind == "digest",
        )
    )).scalar() or 0

    # Engagement: the LATEST insight snapshot per published post, summed per source
    # (post → lead → source). Done in Python so it's DB-agnostic; the volume is a
    # workspace's own posts. Instagram-only + manual refresh → sparse but real.
    insight_rows = (await db.execute(
        select(LeadModel.source_id, PostInsightModel.post_id,
               PostInsightModel.snapshot_at, PostInsightModel.reach,
               PostInsightModel.likes, PostInsightModel.comments,
               PostInsightModel.saved, PostInsightModel.shares)
        .select_from(PostInsightModel)
        .join(PostModel, PostInsightModel.post_id == PostModel.id)
        .join(LeadModel, PostModel.lead_id == LeadModel.id)
        .where(PostModel.workspace_id == ws.id)
    )).all()
    latest: dict[str, tuple] = {}          # post_id → (snapshot_at, source_id, row)
    for sid, pid, snap, reach, likes, comments, saved, shares in insight_rows:
        prev = latest.get(pid)
        if prev is None or (snap and prev[0] and snap > prev[0]) or prev[0] is None:
            latest[pid] = (snap, sid, (reach, likes, comments, saved, shares))
    eng_by_source: dict[str, dict] = {}
    for _snap, sid, (reach, likes, comments, saved, shares) in latest.values():
        e = eng_by_source.setdefault(sid, {"reach": 0, "engagement": 0, "measured": 0})
        e["reach"] += reach or 0
        e["engagement"] += (likes or 0) + (comments or 0) + (saved or 0) + (shares or 0)
        e["measured"] += 1

    sources = (await db.execute(
        select(SourceModel).where(SourceModel.workspace_id == ws.id)
        .order_by(SourceModel.created_at.desc())
    )).scalars().all()

    acc: dict[str, dict] = {}

    def _a(sid: str) -> dict:
        return acc.setdefault(sid, {"strength": {}, "lstatus": {}, "pstatus": {}, "last": None})

    for sid, strength, n in strength_rows:
        _a(sid)["strength"][strength or ""] = n
    for sid, st, n in lstatus_rows:
        _a(sid)["lstatus"][st or ""] = n
    for sid, dt in last_lead_rows:
        _a(sid)["last"] = dt
    for sid, st, n in post_rows:
        _a(sid)["pstatus"][st or ""] = n

    out: list[SourceAnalyticsOut] = []
    tot = {"sources": 0, "leads": 0, "worthy": 0, "drafts": 0, "approved": 0,
           "published": 0, "reach": 0, "engagement": 0, "measured_posts": 0}
    for s in sources:
        a = acc.get(s.id) or {"strength": {}, "lstatus": {}, "pstatus": {}, "last": None}
        strength, ps = a["strength"], a["pstatus"]
        leads_total = sum(strength.values())
        worthy = strength.get("worthy", 0)
        draft = ps.get("draft", 0)
        in_review = ps.get("in_review", 0)
        published = ps.get("published", 0)
        total_posts = (draft + in_review + ps.get("approved", 0)
                       + published + ps.get("rejected", 0))
        approved = ps.get("approved", 0) + published   # reached the approval milestone
        eng = eng_by_source.get(s.id) or {"reach": 0, "engagement": 0, "measured": 0}
        out.append(SourceAnalyticsOut(
            source_id=s.id, url=s.url, kind=s.kind, status=s.status,
            last_checked_at=s.last_checked_at, last_lead_at=a["last"],
            leads_total=leads_total, worthy=worthy, weak=strength.get("weak", 0),
            dismissed=a["lstatus"].get("dismissed", 0),
            drafts=total_posts, in_review=in_review, approved=approved, published=published,
            reach=eng["reach"], engagement=eng["engagement"], measured_posts=eng["measured"],
            worthy_rate=round(worthy / leads_total, 3) if leads_total else 0.0,
            approve_rate=round(approved / total_posts, 3) if total_posts else 0.0,
        ))
        tot["sources"] += 1
        tot["leads"] += leads_total
        tot["worthy"] += worthy
        tot["drafts"] += total_posts
        tot["approved"] += approved
        tot["published"] += published
        tot["reach"] += eng["reach"]
        tot["engagement"] += eng["engagement"]
        tot["measured_posts"] += eng["measured"]

    # best first: most engagement, then published, approved, worthy leads
    out.sort(key=lambda r: (r.engagement, r.published, r.approved, r.worthy, r.leads_total),
             reverse=True)
    return SourceAnalyticsResponse(sources=out, totals=tot, digests=int(digests))


# ── Leads feed ───────────────────────────────────────────────────────────────

@router.get("/leads", response_model=list[LeadOut])
async def list_leads(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    strength: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[LeadOut]:
    ws = await get_or_create_workspace(db, user)
    stmt = select(LeadModel).where(LeadModel.workspace_id == ws.id)
    if strength:
        stmt = stmt.where(LeadModel.strength == strength)
    if status:
        stmt = stmt.where(LeadModel.status == status)
    if since:
        stmt = stmt.where(LeadModel.created_at >= since)
    stmt = stmt.order_by(LeadModel.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [_lead_out(lead) for lead in rows]


@router.get("/leads/{lead_id}", response_model=LeadOut)
async def get_lead(
    lead_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> LeadOut:
    ws = await get_or_create_workspace(db, user)
    lead = await owned_lead(db, lead_id, ws)
    return _lead_out(lead)


@router.post("/leads/{lead_id}/dismiss", response_model=dict)
async def dismiss_lead(
    lead_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    ws = await get_or_create_workspace(db, user)
    lead = await owned_lead(db, lead_id, ws)
    lead.status = "dismissed"
    await db.commit()
    return {"status": "dismissed"}


@router.post("/leads/{lead_id}/snooze-kind", response_model=dict)
async def snooze_lead_kind(
    lead_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    ws = await get_or_create_workspace(db, user)
    lead = await owned_lead(db, lead_id, ws)
    lead.status = "snoozed_kind"
    await db.commit()
    return {"status": "snoozed_kind"}


# ── Draft + digest (Phase 3) — LLM here, on the user's own key ────────────────

_GROUND = ("Write ONLY from the facts in the SOURCE below. Do not invent numbers, "
           "names, dates, or claims; if a detail isn't in the source, leave it out.")


async def _get_brand_rules(db: AsyncSession, ws) -> dict:
    row = (await db.execute(
        select(BrandRulesModel).where(BrandRulesModel.workspace_id == ws.id)
    )).scalar_one_or_none()
    if row is None:
        return {"forbidden": [], "required_disclaimers": []}
    return {"forbidden": row.forbidden or [], "required_disclaimers": row.required_disclaimers or []}


def _parse_platform(value: str) -> Platform:
    return Platform.X if (value or "").strip().lower() == "x" else Platform.INSTAGRAM


def _parse_x_shape(platform: Platform, x_mode: str, x_style: str) -> tuple:
    """X-only post shape. On Instagram it's always a single post, so the mode/style
    are ignored (kept at defaults)."""
    if platform != Platform.X:
        return XPostMode.SHORT, XStyle.STANDARD
    try:
        mode = XPostMode((x_mode or "short").strip().lower())
    except ValueError:
        mode = XPostMode.SHORT
    try:
        style = XStyle((x_style or "standard").strip().lower())
    except ValueError:
        style = XStyle.STANDARD
    return mode, style


async def _generate_business_post(
    engine: ContentEngine, db: AsyncSession, user: UserModel, ws, *,
    topic: str, instructions: str, source_text: str, source_kind: str,
    source_url: Optional[str], lead_id: Optional[str], text_model: str,
    platform: Platform, progress,
    x_mode: XPostMode = XPostMode.SHORT, x_style: XStyle = XStyle.STANDARD,
) -> PostModel:
    """Generate a post via the shared engine, verify its claims, persist + tag it.
    On X the draft can be a thread/long-form via x_mode (x_style frames it)."""
    generated = await engine.generate_post(
        topic=topic, format=PostFormat.SINGLE, text_model=text_model,
        additional_instructions=instructions, platform=platform,
        x_mode=x_mode, x_style=x_style, progress=progress,
    )
    await progress("Checking claims…")
    # Verify factual claims against the source (blocking accuracy layer, Phase 4) +
    # apply the workspace's brand rules. A claim-check failure must not lose the draft.
    try:
        claims = await verify_claims(
            engine.caption_gen.text_provider, draft_text=generated.caption,
            source_text=source_text, text_model=text_model)
    except Exception:
        log.exception("Claim check failed; draft kept without verdicts")
        claims = []
    brand = apply_brand_rules(generated.caption, await _get_brand_rules(db, ws))

    await progress("Saving draft…")
    post = await _persist(generated, db, "branded_card", user_id=user.id)
    post.workspace_id = ws.id
    post.source_kind = source_kind
    post.lead_id = lead_id
    post.status = "draft"                 # enter the Business approval workflow (Phase 5)
    post.ai_caption = generated.caption   # snapshot for the audit journal
    post.claim_check = {"claims": claims, "brand": brand}
    if source_url:
        post.sources = [{"title": topic[:120], "url": source_url}]
    await db.commit()
    return post


def _draft_stream(db, user, ws, engine, text_model, *, topic, instructions,
                  source_text, source_kind, source_url, lead, platform=Platform.INSTAGRAM,
                  x_mode=XPostMode.SHORT, x_style=XStyle.STANDARD, digest_leads=None):
    async def event_stream() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = asyncio.Queue()

        async def progress(message: str) -> None:
            await queue.put({"type": "progress", "message": message})

        async def run() -> None:
            try:
                post = await _generate_business_post(
                    engine, db, user, ws, topic=topic, instructions=instructions,
                    source_text=source_text, source_kind=source_kind, source_url=source_url,
                    lead_id=(lead.id if lead else None), text_model=text_model,
                    platform=platform, x_mode=x_mode, x_style=x_style, progress=progress)
                if lead is not None:
                    lead.status = "drafted"
                for dl in (digest_leads or []):
                    dl.status = "digested"
                await db.commit()
                await queue.put({"type": "complete",
                                 "post": _to_preview(post).model_dump(mode="json")})
            except Exception:
                log.exception("Business draft failed")
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


@router.post("/leads/{lead_id}/draft")
async def draft_lead(
    lead_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    engine: Annotated[ContentEngine, Depends(get_content_engine)],
    platform: str = Query("instagram"),
    x_mode: str = Query("short"),
    x_style: str = Query("standard"),
) -> StreamingResponse:
    ws = await get_or_create_workspace(db, user)
    lead = await owned_lead(db, lead_id, ws)
    _p, text_model, _k = resolve_ai_choice(user, settings, "text")
    if not text_model:
        raise HTTPException(status_code=400,
                            detail="No text model selected. Choose one in Account → AI models.")
    topic = lead.what_happened or "Company update"
    source_text = f"{lead.what_happened or ''}\n{lead.quote or ''}".strip()
    instructions = (f"This is a public update from the company's own source. {_GROUND}\n\n"
                    f"SOURCE:\n{source_text}")
    plat = _parse_platform(platform)
    mode, style = _parse_x_shape(plat, x_mode, x_style)
    return _draft_stream(db, user, ws, engine, text_model, topic=topic,
                         instructions=instructions, source_text=source_text,
                         source_kind="lead", source_url=lead.source_url, lead=lead,
                         platform=plat, x_mode=mode, x_style=style)


@router.post("/digest")
async def draft_digest(
    body: DigestRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    engine: Annotated[ContentEngine, Depends(get_content_engine)],
    platform: str = Query("instagram"),
    x_mode: str = Query("short"),
    x_style: str = Query("standard"),
) -> StreamingResponse:
    ws = await get_or_create_workspace(db, user)
    leads = [await owned_lead(db, lid, ws) for lid in body.lead_ids]
    _p, text_model, _k = resolve_ai_choice(user, settings, "text")
    if not text_model:
        raise HTTPException(status_code=400,
                            detail="No text model selected. Choose one in Account → AI models.")
    bullets = "\n".join(f"- {ld.what_happened or ''}: {ld.quote or ''}" for ld in leads)
    instructions = (f"Write one 'what's new this week' post summarising these updates. {_GROUND}\n\n"
                    f"SOURCE UPDATES:\n{bullets}")
    plat = _parse_platform(platform)
    mode, style = _parse_x_shape(plat, x_mode, x_style)
    return _draft_stream(db, user, ws, engine, text_model, topic="What's new this week",
                         instructions=instructions, source_text=bullets,
                         source_kind="digest", source_url=None, lead=None,
                         platform=plat, x_mode=mode, x_style=style, digest_leads=leads)


@router.get("/drafts", response_model=list[dict])
async def list_drafts(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> list[dict]:
    ws = await get_or_create_workspace(db, user)
    rows = (await db.execute(
        select(PostModel).where(PostModel.workspace_id == ws.id)
        .order_by(PostModel.created_at.desc()).limit(100)
    )).scalars().all()
    out = []
    for p in rows:
        src = p.sources[0] if isinstance(p.sources, list) and p.sources else {}
        cc = p.claim_check if isinstance(p.claim_check, dict) else {}
        out.append({
            "id": p.id, "topic": p.topic, "hook": p.hook, "caption": p.caption,
            "thread_parts": p.thread_parts or [],
            "hashtags": p.hashtags or [], "source_kind": p.source_kind,
            "source_url": src.get("url") if isinstance(src, dict) else None,
            "platform": p.platform, "status": p.status,
            # Why the last publish failed — a "failed" badge with no reason sends
            # the user hunting through the single-post view.
            "schedule_error": p.schedule_error,
            "checked_claims": cc.get("claims") or [],
            "brand_flags": cc.get("brand") or {},
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })
    return out


# ── Brand rules (Phase 4) ────────────────────────────────────────────────────

@router.get("/brand-rules", response_model=BrandRulesOut)
async def get_brand_rules(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> BrandRulesOut:
    ws = await get_or_create_workspace(db, user)
    rules = await _get_brand_rules(db, ws)
    return BrandRulesOut(forbidden=rules["forbidden"],
                         required_disclaimers=rules["required_disclaimers"])


@router.put("/brand-rules", response_model=BrandRulesOut)
async def put_brand_rules(
    body: BrandRulesUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> BrandRulesOut:
    ws = await get_or_create_workspace(db, user)
    row = (await db.execute(
        select(BrandRulesModel).where(BrandRulesModel.workspace_id == ws.id)
    )).scalar_one_or_none()
    if row is None:
        row = BrandRulesModel(workspace_id=ws.id)
        db.add(row)
    row.forbidden = body.forbidden
    row.required_disclaimers = body.required_disclaimers
    await db.commit()
    return BrandRulesOut(forbidden=body.forbidden,
                         required_disclaimers=body.required_disclaimers)


# ── Publishing limits (Phase 6) ──────────────────────────────────────────────

@router.get("/limits", response_model=LimitsOut)
async def get_limits(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> LimitsOut:
    ws = await get_or_create_workspace(db, user)
    return LimitsOut(max_per_day=ws.max_per_day, max_per_week=ws.max_per_week)


@router.put("/limits", response_model=LimitsOut)
async def put_limits(
    body: LimitsUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> LimitsOut:
    ws = await get_or_create_workspace(db, user)
    ws.max_per_day = body.max_per_day
    ws.max_per_week = body.max_per_week
    await db.commit()
    return LimitsOut(max_per_day=body.max_per_day, max_per_week=body.max_per_week)


# ── Approval workflow + audit journal (Phase 5) ──────────────────────────────
# Solo model: the workspace owner is author + approver. Roles/invites are deferred.

def _has_brand_issue(post: PostModel) -> bool:
    cc = post.claim_check if isinstance(post.claim_check, dict) else {}
    brand = cc.get("brand") or {}
    return bool(brand.get("forbidden")) or bool(brand.get("missing_disclaimers"))


@router.put("/drafts/{post_id}", response_model=dict)
async def edit_draft(
    post_id: str,
    body: DraftEditRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    ws = await get_or_create_workspace(db, user)
    post = await owned_business_post(db, post_id, ws)
    if post.status not in ("draft", "in_review"):
        raise HTTPException(status_code=409, detail="Only a draft or in-review post can be edited.")
    post.caption = body.caption          # human edit; ai_caption is preserved for the audit
    await db.commit()
    return {"status": "ok"}


@router.post("/posts/{post_id}/submit", response_model=dict)
async def submit_post(
    post_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    ws = await get_or_create_workspace(db, user)
    post = await owned_business_post(db, post_id, ws)
    if post.status != "draft":
        raise HTTPException(status_code=409, detail="Only a draft can be submitted for review.")
    post.status = "in_review"
    await db.commit()
    return {"status": "in_review"}


@router.post("/posts/{post_id}/approve", response_model=dict)
async def approve_post(
    post_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    ws = await get_or_create_workspace(db, user)
    post = await owned_business_post(db, post_id, ws)
    if post.status != "in_review":
        raise HTTPException(status_code=409, detail="Only an in-review post can be approved.")
    if _has_brand_issue(post):
        raise HTTPException(status_code=409,
                            detail="Fix the brand-rule issues before approving.")
    post.status = "approved"
    src = post.sources[0] if isinstance(post.sources, list) and post.sources else {}
    db.add(AuditEntryModel(
        workspace_id=ws.id, post_id=post.id, lead_id=post.lead_id,
        source_url=src.get("url") if isinstance(src, dict) else None,
        ai_draft=post.ai_caption, human_edits=post.caption,
        approved_by=user.id, approved_at=datetime.now(timezone.utc),
    ))
    await db.commit()
    # The row above is the journal's first content, and UX phase 8 hangs the
    # Journal tab on that rather than on a publication count: the journal is
    # written here, at sign-off, and a workspace can approve nine posts without
    # publishing one. Recorded after the commit and inside a try — the sign-off
    # is the fact worth keeping, and it is the one with legal weight.
    try:
        await milestones.record(db, user, milestones.JOURNAL_UNLOCKED)
    except Exception:
        log.exception("Could not record the journal milestone for user=%s", user.id)
    return {"status": "approved"}


@router.post("/posts/{post_id}/reject", response_model=dict)
async def reject_post(
    post_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> dict:
    ws = await get_or_create_workspace(db, user)
    post = await owned_business_post(db, post_id, ws)
    post.status = "rejected"
    await db.commit()
    return {"status": "rejected"}


def _audit_row(a: AuditEntryModel) -> dict:
    return {
        "id": a.id, "post_id": a.post_id, "source_url": a.source_url,
        "ai_draft": a.ai_draft, "human_edits": a.human_edits,
        "approved_by": a.approved_by,
        "approved_at": a.approved_at.isoformat() if a.approved_at else None,
        "published_url": a.published_url,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


async def _journal_rows(db, ws, since: Optional[datetime], until: Optional[datetime]):
    stmt = select(AuditEntryModel).where(AuditEntryModel.workspace_id == ws.id)
    if since:
        stmt = stmt.where(AuditEntryModel.created_at >= since)
    if until:
        stmt = stmt.where(AuditEntryModel.created_at <= until)
    stmt = stmt.order_by(AuditEntryModel.created_at.desc())
    return (await db.execute(stmt)).scalars().all()


@router.get("/journal", response_model=list[dict])
async def get_journal(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    since: Optional[datetime] = Query(None, alias="from"),
    until: Optional[datetime] = Query(None, alias="to"),
) -> list[dict]:
    ws = await get_or_create_workspace(db, user)
    return [_audit_row(a) for a in await _journal_rows(db, ws, since, until)]


@router.get("/journal/export")
async def export_journal(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    format: str = Query("csv"),
    since: Optional[datetime] = Query(None, alias="from"),
    until: Optional[datetime] = Query(None, alias="to"),
) -> Response:
    ws = await get_or_create_workspace(db, user)
    rows = [_audit_row(a) for a in await _journal_rows(db, ws, since, until)]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    if format == "json":
        return Response(
            content=json.dumps(rows, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="journal_{ts}.json"'})
    cols = ["created_at", "approved_at", "approved_by", "source_url",
            "ai_draft", "human_edits", "published_url", "post_id"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="journal_{ts}.csv"'})
