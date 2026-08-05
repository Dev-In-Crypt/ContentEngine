"""Workspace resolution + ownership gates for the Business module.

A business user gets exactly one workspace for now. It's created lazily (business
accounts from Phase 0 have none yet), so every business route starts by resolving
it. Ownership gates mirror api.deps.owned_post: filter by workspace_id, 404 (not
403) on a miss so one tenant can't probe another's rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Lead as LeadModel
from models.database import Post as PostModel
from models.database import Source as SourceModel
from models.database import User as UserModel
from models.database import Workspace as WorkspaceModel


async def get_or_create_workspace(db: AsyncSession, user: UserModel) -> WorkspaceModel:
    """The user's single workspace, created on first use."""
    ws = (await db.execute(
        select(WorkspaceModel).where(WorkspaceModel.owner_user_id == user.id)
    )).scalar_one_or_none()
    if ws is None:
        # The brand name lives on the profile now; User's copy is a rollback
        # snapshot nothing reads (UX phase 2).
        from services.managed_account import ensure_primary_profile
        brand = await ensure_primary_profile(db, user)
        name = (brand.brand_name
                or (user.email.split("@")[0] if user.email else "")
                or "My workspace")
        ws = WorkspaceModel(owner_user_id=user.id, name=name)
        db.add(ws)
        await db.commit()
        await db.refresh(ws)
    return ws


async def owned_source(db: AsyncSession, source_id: str, workspace: WorkspaceModel) -> SourceModel:
    src = (await db.execute(
        select(SourceModel).where(
            SourceModel.id == source_id, SourceModel.workspace_id == workspace.id)
    )).scalar_one_or_none()
    if src is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return src


async def owned_lead(db: AsyncSession, lead_id: str, workspace: WorkspaceModel) -> LeadModel:
    lead = (await db.execute(
        select(LeadModel).where(
            LeadModel.id == lead_id, LeadModel.workspace_id == workspace.id)
    )).scalar_one_or_none()
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


async def owned_business_post(db: AsyncSession, post_id: str, workspace: WorkspaceModel,
                              *, options=()) -> PostModel:
    stmt = select(PostModel).where(
        PostModel.id == post_id, PostModel.workspace_id == workspace.id)
    if options:
        stmt = stmt.options(*options)
    post = (await db.execute(stmt)).scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


async def _published_count(db: AsyncSession, workspace_id: str, since: datetime) -> int:
    return (await db.execute(
        select(func.count()).select_from(PostModel).where(
            PostModel.workspace_id == workspace_id,
            PostModel.status == "published",
            PostModel.published_at >= since,
        )
    )).scalar() or 0


async def within_frequency_cap(db: AsyncSession, workspace: WorkspaceModel,
                               now: datetime) -> Optional[str]:
    """Publishing-frequency guard (Phase 6). Returns a reason string if publishing
    now would exceed the workspace's daily/weekly cap, else None. NULL caps =
    unlimited. Counts already-published posts in the trailing window."""
    if workspace.max_per_day:
        if await _published_count(db, workspace.id, now - timedelta(days=1)) >= workspace.max_per_day:
            return f"Daily publishing limit reached ({workspace.max_per_day}/day)."
    if workspace.max_per_week:
        if await _published_count(db, workspace.id, now - timedelta(days=7)) >= workspace.max_per_week:
            return f"Weekly publishing limit reached ({workspace.max_per_week}/week)."
    return None
