"""Status for a video publish job (Phase 8) — what the SPA polls while a
chunked X upload works through INIT/APPEND/FINALIZE/STATUS/tweet in the
background. Kept out of posts.py/media.py (already large) since this is its
own small resource.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, get_db
from models.database import User as UserModel, VideoPublishJob as VideoPublishJobModel
from models.schemas import VideoPublishJobStatus

router = APIRouter(prefix="/api/publish-jobs", tags=["publish-jobs"])

_ACTIVE = ("queued", "uploading", "processing", "tweeting")


def _progress_pct(job: VideoPublishJobModel) -> Optional[int]:
    if job.status != "uploading" or not job.total_bytes:
        return None
    from services.publishing.x import VIDEO_CHUNK_BYTES
    total_chunks = max(1, -(-job.total_bytes // VIDEO_CHUNK_BYTES))   # ceil division
    return min(100, round(job.chunk_index / total_chunks * 100))


def build_job_status(job: VideoPublishJobModel, *,
                     warning: Optional[str] = None) -> VideoPublishJobStatus:
    """Shared by this router's own GETs and the 202 responses from the
    publish-video/publish-x routes — one place builds the response shape."""
    return VideoPublishJobStatus(
        id=job.id, platform=job.platform, status=job.status,
        post_id=job.post_id, asset_id=job.asset_id,
        tweet_id=job.tweet_id, permalink=job.permalink, error=job.error,
        progress_pct=_progress_pct(job), warning=warning,
        created_at=job.created_at, updated_at=job.updated_at,
    )


@router.get("", response_model=list[VideoPublishJobStatus])
async def list_publish_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    active: bool = Query(False),
    asset_id: Optional[str] = Query(None),
    post_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> list[VideoPublishJobStatus]:
    stmt = (select(VideoPublishJobModel)
            .order_by(VideoPublishJobModel.created_at.desc()).limit(limit))
    if not user.is_local:
        stmt = stmt.where(VideoPublishJobModel.user_id == user.id)
    if active:
        stmt = stmt.where(VideoPublishJobModel.status.in_(_ACTIVE))
    if asset_id is not None:
        stmt = stmt.where(VideoPublishJobModel.asset_id == asset_id)
    if post_id is not None:
        stmt = stmt.where(VideoPublishJobModel.post_id == post_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [build_job_status(r) for r in rows]


@router.get("/{job_id}", response_model=VideoPublishJobStatus)
async def get_publish_job(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> VideoPublishJobStatus:
    stmt = select(VideoPublishJobModel).where(VideoPublishJobModel.id == job_id)
    if not user.is_local:
        stmt = stmt.where(VideoPublishJobModel.user_id == user.id)
    job = (await db.execute(stmt)).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Publish job not found")
    return build_job_status(job)
