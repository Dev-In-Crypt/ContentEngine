"""Background source polling — rules only, never LLM (a firm cost decision).

For each active Source: fetch recent items, diff them against SourceSnapshot to
find what's new or changed, run the (free) event_selector rules, and write a Lead
for anything not a duplicate. The LLM (lead card + draft) happens only later, when
the user clicks "make a post" (Phase 3), on the user's own key. So a background
poll costs nothing but network + CPU.

Each source is polled in its own try/except and failures are logged (never a
silent `continue` — the lesson from the removed trend_provider). The caller
commits; poll_source only stages rows.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from models.database import Lead as LeadModel
from models.database import Source as SourceModel
from models.database import SourceSnapshot as SnapshotModel
from services.event_selector import detect_bad_news, score_item
from services.sources import SourceFetchError, get_source_fetcher
from services.sources.base import SourceRateLimited

log = logging.getLogger(__name__)

_LOOKBACK_DAYS = 90
_RECENT_DAYS = 30
_QUOTE_LEN = 300


def _fingerprint(title: str, body: str) -> str:
    return hashlib.sha1(f"{title}\n{body}".encode()).hexdigest()


async def poll_source(db, source: SourceModel, ssl_verify: bool = True) -> int:
    """Fetch a source, create Leads for new/changed items. Returns the Lead count.
    Stages rows on `db` (caller commits). Raises SourceFetchError on a fetch failure."""
    now = datetime.now(timezone.utc)
    fetcher = get_source_fetcher(source.kind, ssl_verify=ssl_verify)
    items = await fetcher.fetch(source.url, since=now - timedelta(days=_LOOKBACK_DAYS))

    # Existing snapshots for this source (external_id → snapshot row).
    snaps = {
        s.external_id: s for s in (await db.execute(
            select(SnapshotModel).where(SnapshotModel.source_id == source.id)
        )).scalars().all()
    }
    # Recent lead titles in this workspace feed the duplicate rule.
    recent_titles = list((await db.execute(
        select(LeadModel.what_happened).where(
            LeadModel.workspace_id == source.workspace_id,
            LeadModel.created_at >= now - timedelta(days=_RECENT_DAYS),
        )
    )).scalars().all())

    created = 0
    for item in items:
        ext = item.external_id
        fp = _fingerprint(item.title, item.body)
        snap = snaps.get(ext)
        if snap is None:
            db.add(SnapshotModel(source_id=source.id, external_id=ext, fingerprint=fp))
        elif snap.fingerprint != fp:
            snap.fingerprint = fp          # the item changed → treat as a fresh lead
        else:
            continue                       # seen, unchanged → skip

        strength, reason = score_item(item, recent_titles)
        recent_titles.append(item.title)   # catch duplicates within this run too
        if strength == "duplicate":
            continue
        db.add(LeadModel(
            workspace_id=source.workspace_id, source_id=source.id, external_id=ext,
            what_happened=item.title, source_url=item.url,
            quote=(item.body or "")[:_QUOTE_LEN], published_at=item.published_at,
            strength=strength, reason=reason, status="new",
            sensitive=detect_bad_news(item),
            raw=item.raw if isinstance(item.raw, dict) else {},
        ))
        created += 1

    source.status = "ok"
    source.last_checked_at = now
    return created


async def poll_all(sessionmaker, ssl_verify: bool = True) -> dict:
    """Poll every active source once. Never lets one bad source stop the rest."""
    now = datetime.now(timezone.utc)
    sources_polled = 0
    leads_created = 0
    async with sessionmaker() as db:
        sources = (await db.execute(
            select(SourceModel).where(SourceModel.active.is_(True))
        )).scalars().all()
        for source in sources:
            try:
                leads_created += await poll_source(db, source, ssl_verify)
                sources_polled += 1
            except SourceRateLimited as e:
                # Must precede SourceFetchError (its parent): being over quota is
                # temporary, and reporting it as "unreachable" invites the user to
                # delete a source that works.
                log.warning("Rate limited on source %s (%s): %s", source.id, source.url, e)
                source.status = "rate_limited"
                source.last_checked_at = now
            except SourceFetchError as e:
                log.warning("Poll failed for source %s (%s): %s", source.id, source.url, e)
                source.status = "unreachable"
                source.last_checked_at = now
            except Exception:
                log.exception("Unexpected error polling source %s (%s)", source.id, source.url)
                source.last_checked_at = now
        await db.commit()
    log.info("Source poll: %d source(s), %d new lead(s)", sources_polled, leads_created)
    return {"sources": sources_polled, "leads": leads_created}
