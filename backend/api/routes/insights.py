"""The one aggregate in the product.

Everything else on Results is a list the browser filtered. Four numbers, a bar
per post and a best-post card cannot be done that way — not honestly, anyway:
the browser would have to pull every post ever published to add up a month.

What this route is careful about is not arithmetic, it is what the arithmetic
is allowed to claim.

**A total is only over what was measured.** Metrics arrive one post at a time,
when somebody presses Refresh on that post. A month with eighteen posts and
four snapshots has a reach figure covering four posts, and calling that "your
reach" would be wrong by whatever the other fourteen did. So the response
carries `measured_posts` beside `posts_out`, and the screen is obliged to print
the pair.

**X reports nothing.** Not zero — nothing. There is no insights API for it
here. A window containing X posts is a window this screen is partly blind to,
and `networks_without_metrics` says which ones, so a blind month cannot pass
for a quiet one.

**A delta needs the window before it.** Comparing against all of history is a
ratio, not a trend; comparing against an empty previous window is +infinity,
which is why that case returns None rather than a number.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, get_db
from models.database import LLMUsage, Post as PostModel, PostInsight, User as UserModel

router = APIRouter(prefix="/api/insights", tags=["insights"])

#: Networks with no insights API here. Instagram is the only one that has one,
#: so this is "everything else" written out rather than inferred — a new
#: publisher should have to decide which side it is on.
_NO_METRICS = {"x", "linkedin"}

#: How late a post may go out and still count as on time. The scheduler runs on
#: an interval, so "to the second" would measure the poller, not the product.
_ON_TIME_SLACK = timedelta(minutes=15)


class Trend(BaseModel):
    value: Optional[int] = None
    #: Change against the window immediately before this one. None when there
    #: was nothing there to compare with.
    delta_pct: Optional[float] = None


class PostReach(BaseModel):
    id: str
    topic: str
    reach: int


class InsightsResponse(BaseModel):
    days: int
    posts_out: int
    on_time: int
    #: How many of `posts_out` have any metrics at all. The gap between the two
    #: is the part of the window these numbers do not cover.
    measured_posts: int
    networks_without_metrics: list[str]
    reach: Trend
    saves: Trend
    spend_usd: float
    by_post: list[PostReach]
    best: Optional[PostReach] = None


async def _newest_per_post(db: AsyncSession, post_ids: list[str]) -> dict:
    """Newest snapshot per post. The oldest is the one taken minutes after
    publishing, when the number is nearly zero."""
    if not post_ids:
        return {}
    newest = (select(PostInsight.post_id,
                     func.max(PostInsight.snapshot_at).label("at"))
              .where(PostInsight.post_id.in_(post_ids))
              .group_by(PostInsight.post_id)).subquery()
    rows = (await db.execute(
        select(PostInsight).join(
            newest, and_(PostInsight.post_id == newest.c.post_id,
                         PostInsight.snapshot_at == newest.c.at)))).scalars().all()
    return {r.post_id: r for r in rows}


def _delta(now: int, before: int) -> Optional[float]:
    if not before:
        return None            # everything is up infinitely from nothing
    return round((now - before) / before * 100, 1)


async def _published_between(db: AsyncSession, user: UserModel,
                             start: datetime, end: datetime) -> list:
    stmt = (select(PostModel)
            .where(PostModel.status == "published")
            .where(PostModel.published_at >= start)
            .where(PostModel.published_at < end))
    if not user.is_local:
        stmt = stmt.where(PostModel.user_id == user.id)
        active = user.active_account_id
        stmt = stmt.where(PostModel.managed_account_id == active if active
                          else PostModel.managed_account_id.is_(None))
    return list((await db.execute(stmt)).scalars().all())


@router.get("", response_model=InsightsResponse)
async def get_insights(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    days: int = Query(30, ge=1, le=365),
) -> InsightsResponse:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    prev_start = start - timedelta(days=days)

    posts = await _published_between(db, user, start, now)
    prev = await _published_between(db, user, prev_start, start)

    snaps = await _newest_per_post(db, [p.id for p in posts])
    prev_snaps = await _newest_per_post(db, [p.id for p in prev])

    def total(rows, ids, field):
        return sum(getattr(rows[i], field) or 0 for i in ids if i in rows)

    ids = [p.id for p in posts]
    prev_ids = [p.id for p in prev]

    on_time = sum(1 for p in posts
                  if p.scheduled_at is None
                  or p.published_at <= p.scheduled_at + _ON_TIME_SLACK)

    by_post = sorted(
        (PostReach(id=p.id, topic=p.topic or "post", reach=snaps[p.id].reach or 0)
         for p in posts if p.id in snaps and (snaps[p.id].reach or 0) > 0),
        key=lambda r: r.reach, reverse=True)

    spend = (await db.execute(
        select(func.coalesce(func.sum(LLMUsage.cost), 0.0))
        .where(LLMUsage.created_at >= start)
        .where(LLMUsage.user_id == user.id if not user.is_local
               else LLMUsage.id.isnot(None)))).scalar_one()

    return InsightsResponse(
        days=days,
        posts_out=len(posts),
        on_time=on_time,
        measured_posts=sum(1 for i in ids if i in snaps),
        networks_without_metrics=sorted(
            {(p.platform or "instagram") for p in posts} & _NO_METRICS),
        reach=Trend(value=total(snaps, ids, "reach"),
                    delta_pct=_delta(total(snaps, ids, "reach"),
                                     total(prev_snaps, prev_ids, "reach"))),
        saves=Trend(value=total(snaps, ids, "saved"),
                    delta_pct=_delta(total(snaps, ids, "saved"),
                                     total(prev_snaps, prev_ids, "saved"))),
        spend_usd=round(float(spend or 0.0), 4),
        by_post=by_post[:20],
        best=by_post[0] if by_post else None,
    )
