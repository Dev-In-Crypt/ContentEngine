"""Is a network connection healthy, and is its token about to die?

Two separate questions with different levels of certainty, kept apart on purpose:

  * **Health** is a fact. `verify_credentials()` either works or it doesn't, so the
    daily check knows for sure — but only *after* something breaks.
  * **Expiry** is usually a guess. Instagram Login long-lived tokens last 60 days,
    and Meta offers no read-only introspection: the only endpoint that reveals the
    remaining lifetime is `refresh_access_token`, which ROTATES the token. So
    unless the user has pressed "Renew" (which does refresh, and then we know the
    real date), all we can do is count 60 days from when the token was saved —
    an upper bound, because a pasted token may already be weeks old.

That distinction is carried in the data (`expires_estimated`) and must reach the
UI: telling someone "expires in 3 days" as if it were certain, when the token
might already be dead, is worse than saying nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

#: Instagram long-lived token lifetime, per Meta's docs.
IG_TOKEN_LIFETIME_DAYS = 60
#: How early to start warning. A week is enough to act without nagging for two months.
EXPIRY_WARN_DAYS = 7

NETWORKS = ("instagram", "x")


def estimate_expiry(saved_at: datetime) -> datetime:
    """Upper bound on an Instagram token's life, counted from when we received it."""
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=timezone.utc)
    return saved_at + timedelta(days=IG_TOKEN_LIFETIME_DAYS)


def days_left(expires_at: Optional[datetime], now: Optional[datetime] = None) -> Optional[int]:
    """Whole days until expiry (negative once past). None when unknown."""
    if expires_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (expires_at - now).days


def is_expiring_soon(expires_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """True inside the warning window, including already expired."""
    left = days_left(expires_at, now)
    return left is not None and left <= EXPIRY_WARN_DAYS


def make_record(*, ok: bool, error: str = "", handle: str = "",
                expires_at: Optional[datetime] = None, estimated: bool = True,
                checked_at: Optional[datetime] = None) -> dict:
    """One network's health, JSON-safe for the credentials row."""
    return {
        "ok": bool(ok),
        "error": (error or "")[:400],
        "handle": handle or "",
        "checked_at": (checked_at or datetime.now(timezone.utc)).isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "expires_estimated": bool(estimated) if expires_at else False,
    }


def became_broken(previous: Optional[dict], current: dict) -> bool:
    """True only on the transition into failure.

    The daily job would otherwise email every single day for as long as a token
    stays dead, which trains the user to filter us into spam — and then they miss
    the one that matters. A connection that was already broken yesterday is not
    news; one that worked yesterday is.
    """
    if current.get("ok"):
        return False
    if previous is None:
        return True                      # first ever check, and it failed
    return bool(previous.get("ok"))
