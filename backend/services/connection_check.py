"""The daily "are the publishing credentials still alive?" sweep.

Publishing is the one thing that fails silently and late: a token dies quietly,
and the user finds out when a scheduled post doesn't appear. Checking once a day
turns that into a warning they get while there's still time to fix it.

Read-only by design — `verify_credentials()` on both publishers only reads. The
sweep never writes to the credential store; renewing an Instagram token rotates
it, so that stays an explicit user action (see api/routes/settings.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from models.database import UserCredentials as CredsModel, User as UserModel
from services.connection_health import became_broken, make_record
from services.publishing.factory import PUBLISHABLE_PLATFORMS
from services.user_settings import build_settings_for_user

log = logging.getLogger(__name__)


async def _check_one(platform: str, settings) -> dict:
    """Verify one network. Never raises — a failure IS the result."""
    from services.publishing.factory import make_publisher_for

    publisher = None
    try:
        publisher = make_publisher_for(platform, settings)
        info = await publisher.verify_credentials()
        handle = info.get("username") or info.get("handle") or ""
        return make_record(ok=True, handle=handle)
    except Exception as e:
        # Includes "credentials not configured" from the factory, which is a real
        # answer: the network isn't usable, even if nothing is technically broken.
        return make_record(ok=False, error=str(e))
    finally:
        if publisher is not None:
            try:
                await publisher.close()
            except Exception:
                pass


async def check_user(db, user) -> dict:
    """Check every publishable network for one user and persist the result.

    Returns {platform: record} for the networks that were checked.
    """
    creds = await db.get(CredsModel, user.id)
    if creds is None:
        return {}
    settings = await build_settings_for_user(db, user)
    previous = dict(creds.connection_health or {})
    current = dict(previous)
    newly_broken: list[str] = []

    for platform in sorted(PUBLISHABLE_PLATFORMS):
        record = await _check_one(platform, settings)
        # Carry the expiry forward: this sweep can't read it (no introspection),
        # so overwriting it with None would erase what "Renew" learned.
        prev = previous.get(platform) or {}
        record["expires_at"] = prev.get("expires_at")
        record["expires_estimated"] = prev.get("expires_estimated", False)
        if became_broken(prev or None, record):
            newly_broken.append(platform)
        current[platform] = record

    creds.connection_health = current
    await db.commit()

    if newly_broken:
        await _notify(user, {p: current[p] for p in newly_broken})
    return current


async def _notify(user, broken: dict) -> None:
    from services.email import send_connection_broken_email

    email = getattr(user, "email", None)
    if not email:
        return
    for platform, record in broken.items():
        await send_connection_broken_email(email, platform, record.get("error") or "")


async def run_connection_check(sessionmaker) -> dict:
    """Sweep every cloud user that has credentials. Returns a small summary."""
    checked = broken = 0
    async with sessionmaker() as db:
        users = (await db.execute(
            select(UserModel).join(CredsModel, CredsModel.user_id == UserModel.id)
            .where(UserModel.is_active.is_(True))
        )).scalars().all()
        for user in users:
            try:
                result = await check_user(db, user)
            except Exception:
                log.exception("Connection check failed for user=%s", user.id)
                continue
            checked += 1
            broken += sum(1 for r in result.values() if not r.get("ok"))
    log.info("Connection check: %d user(s), %d broken connection(s) at %s",
             checked, broken, datetime.now(timezone.utc).isoformat())
    return {"users": checked, "broken": broken}
