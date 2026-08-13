"""How many free posts one address has already had from the landing.

The landing exists so somebody can see the product work before giving anything
up, which rules out an account — so the count has to hang off the only thing a
visitor has, their address. That is a weak hook, and this file is deliberate
about how weak:

  * **It is a speed bump, not a wall.** A different network defeats it in ten
    seconds. What it stops is the easy version — clearing the browser and going
    again, forever, which is all the previous counter (in localStorage) asked of
    anybody.
  * **Addresses are shared.** An office, a café, a mobile carrier: hundreds of
    people behind one address, and the second of them finds the product already
    spent. That is the real cost of this rule and it is paid by honest visitors,
    which is why the count expires (below) instead of lasting forever.
  * **The address is never stored.** Only a hash of it, salted with the
    application secret, so the table cannot be turned back into a list of who
    visited. Nothing here is tied to an account, so there is nothing for GDPR
    export or erasure to reach — and nothing anybody could ask us to hand over
    about themselves either.

The money is bounded elsewhere and better: the per-IP rate limit on the route,
and the daily spend ceiling. This decides when to ask for an account.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import AnonUsage

#: Free posts per address before the landing asks for an account.
FREE_TRIES = 2

#: How long a count is remembered. Not forever, on purpose: addresses are handed
#: around — a mobile carrier reassigns them hourly, an office keeps one for
#: years — so a permanent count would quietly retire whole buildings from ever
#: seeing the product work. Thirty days keeps the same defence against the
#: person who just wants more, and forgets the stranger who inherited a number.
WINDOW = timedelta(days=30)


def fingerprint(ip: str, secret: str) -> str:
    """A one-way name for an address. Salted so the table is not a rainbow table
    of every IPv4 in existence — there are only four billion, which is an
    afternoon of hashing without the salt."""
    return hashlib.sha256(f"{secret}:{ip or ''}".encode()).hexdigest()[:40]


async def _row(db: AsyncSession, key: str) -> Optional[AnonUsage]:
    return (await db.execute(
        select(AnonUsage).where(AnonUsage.ip_hash == key))).scalar_one_or_none()


def _expired(row: AnonUsage, now: datetime) -> bool:
    last = row.last_seen
    if last is None:
        return False
    if last.tzinfo is None:                      # SQLite hands back naive datetimes
        last = last.replace(tzinfo=timezone.utc)
    return now - last > WINDOW


async def remaining(db: AsyncSession, ip: str, secret: str) -> int:
    """Free posts left for this address, never negative."""
    row = await _row(db, fingerprint(ip, secret))
    if row is None or _expired(row, datetime.now(timezone.utc)):
        return FREE_TRIES
    return max(0, FREE_TRIES - (row.used or 0))


async def reserve(db: AsyncSession, ip: str, secret: str) -> bool:
    """Claim one free post for this address, or refuse. Commits before returning.

    Committed before the model is called, like the account-level allowance: a
    generation that dies halfway must not hand the try back, or a crash loop is
    free posts forever.
    """
    now = datetime.now(timezone.utc)
    key = fingerprint(ip, secret)
    row = await _row(db, key)
    if row is None:
        row = AnonUsage(ip_hash=key, used=0, first_seen=now, last_seen=now)
        db.add(row)
    elif _expired(row, now):
        row.used = 0
        row.first_seen = now
    if (row.used or 0) >= FREE_TRIES:
        return False
    row.used = (row.used or 0) + 1
    row.last_seen = now
    await db.commit()
    return True
