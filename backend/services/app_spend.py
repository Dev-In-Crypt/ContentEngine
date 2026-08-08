"""What the application has spent today on its own key.

The product is bring-your-own-keys, so almost every model call is billed to the
tenant who made it. The exceptions are the ones we pay for: onboarding's sample
post (UX phase 5) and the free generations that follow it (UX phase 6). A
per-account allowance bounds what one person can take. This module bounds what
everyone can take together — the ceiling that stops a bad day from being an
expensive one.

Two properties of the existing accounting shape it:

**Cost is buffered in memory.** `record_usage` appends to a module-level list
and `drain_usage` empties it; until now the only caller that turned it into rows
was `GET /api/usage`, the cost dashboard. A ceiling read straight from the table
would therefore report zero for as long as nobody opened that tab — which is
precisely the window a runaway would run in. So every read here flushes first.

**Our spend is the rows with no user.** The auth dependency sets
`current_user_id` to the caller, and the routes that spend our money clear it
before the call: filing our bill under the name of somebody we told "you pay the
vendor directly" would be a lie in the interface. That leaves `user_id IS NULL`
meaning exactly "the application's own spend", so the ceiling needs no second
table and no second writer to find it.

`flush_usage` lives here rather than in the admin router because it is now read
by two callers. Two copies of "turn the buffer into rows" would drift, and the
drift would be silent in the worst direction — a ceiling that sees less than was
actually spent.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import LLMUsage
from services.openrouter import drain_usage


async def flush_usage(db: AsyncSession) -> None:
    """Write everything buffered since the last flush. No records → no write:
    a poll of the dashboard or of the ceiling must not touch the database."""
    records = drain_usage()
    if not records:
        return
    for rec in records:
        db.add(LLMUsage(
            id=str(uuid.uuid4()),
            user_id=rec.get("user_id"),
            model=rec.get("model"),
            prompt_tokens=rec.get("prompt_tokens"),
            completion_tokens=rec.get("completion_tokens"),
            total_tokens=rec.get("total_tokens"),
            cost=rec.get("cost") or 0.0,
            created_at=rec.get("at") or datetime.now(timezone.utc),
        ))
    await db.commit()


def _day_start() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


async def app_spend_today(db: AsyncSession) -> float:
    """USD spent on the application's own key since midnight UTC.

    Reads what is already written. Callers deciding whether to spend more want
    `flush_and_total`; this one exists for the tests and for a caller that has
    just flushed.
    """
    result = await db.execute(
        select(func.coalesce(func.sum(LLMUsage.cost), 0.0))
        .where(LLMUsage.user_id.is_(None))
        .where(LLMUsage.created_at >= _day_start())
    )
    return float(result.scalar_one() or 0.0)


async def flush_and_total(db: AsyncSession) -> float:
    """The number a spend decision should be made on: buffered calls included."""
    await flush_usage(db)
    return await app_spend_today(db)
