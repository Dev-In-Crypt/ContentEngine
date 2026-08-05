"""Take your data with you, or take it off our servers.

Two obligations of a public SaaS that holds other people's credentials, in one
module so the rules stay in one place.

**Export** is deliberately credential-free. A user's API keys are their own data,
but writing them into a downloadable archive turns one careless forward into a
compromised Instagram account and someone else's paid X bill. The export says
which keys are set, never what they are.

**Erasure** walks the object graph explicitly instead of leaning on database
cascades. Cascades are configured per-dialect and silently off in SQLite unless a
pragma is set, so relying on them means the tests prove nothing about production.
Walking it by hand also yields the counts we show the user and log.
"""
from __future__ import annotations

import json
import logging
import shutil
import zipfile
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from models.database import (
    AuditEntry, BrandRules, Lead, LLMUsage, ManagedAccount, MediaAsset, Post,
    PostInsight, Slide, Source, SourceSnapshot, User, UserCredentials,
    VideoPublishJob, Workspace,
)
from services.scheduler import cancel_publish

log = logging.getLogger(__name__)

# backend/services/gdpr.py -> backend/uploads
UPLOADS_ROOT = Path(__file__).resolve().parent.parent / "uploads"

# Per-user directories, as laid out by logo_store / music_store / staging /
# media_store. A new per-user store must be added here in the same change that
# creates it — a directory that exists before erasure knows about it is a
# directory we cannot honour a deletion request for.
_USER_DIRS = ("logos", "music", "staging", "media")

# Columns we never put in an export, whatever table they turn up in.
_NEVER_EXPORT = {"password_hash"}


def _jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _row(obj, *, skip: Iterable[str] = ()) -> dict:
    """A SQLAlchemy row as a plain dict, minus anything secret."""
    drop = _NEVER_EXPORT | set(skip)
    out = {}
    for col in obj.__table__.columns:
        name = col.name
        # *_enc columns hold Fernet ciphertext. Shipping it is pointless (the user
        # has no key) and it is still a secret at rest.
        if name in drop or name.endswith("_enc"):
            continue
        out[name] = _jsonable(getattr(obj, name, None))
    return out


def _credential_summary(creds: Optional[UserCredentials]) -> dict:
    """Which keys are on file — never their values. See the module docstring."""
    summary = {}
    for col in UserCredentials.__table__.columns:
        if not col.name.endswith("_enc"):
            continue
        summary[col.name[:-4]] = {"set": bool(getattr(creds, col.name, None))}
    return summary


async def collect_user_data(db, user: User) -> dict:
    """Everything we hold about one account, JSON-safe and secret-free."""
    creds = await db.get(UserCredentials, user.id)

    posts = (await db.execute(
        select(Post).where(Post.user_id == user.id).order_by(Post.created_at)
    )).scalars().all()
    post_ids = [p.id for p in posts]

    slides_by_post: dict[str, list] = {}
    insights_by_post: dict[str, list] = {}
    if post_ids:
        for slide in (await db.execute(
            select(Slide).where(Slide.post_id.in_(post_ids))
            .order_by(Slide.slide_number)
        )).scalars().all():
            slides_by_post.setdefault(slide.post_id, []).append(_row(slide))
        for ins in (await db.execute(
            select(PostInsight).where(PostInsight.post_id.in_(post_ids))
        )).scalars().all():
            insights_by_post.setdefault(ins.post_id, []).append(_row(ins))

    post_docs = []
    for p in posts:
        doc = _row(p)
        doc["slides"] = slides_by_post.get(p.id, [])
        doc["insights"] = insights_by_post.get(p.id, [])
        post_docs.append(doc)

    return {
        "exported_at": datetime.now().astimezone().isoformat(),
        "note": ("API keys are listed as set/not set only — we never write a usable "
                 "credential into a file you might forward."),
        "account": _row(user),
        "credentials": _credential_summary(creds),
        "posts": post_docs,
        "managed_accounts": [_row(a) for a in (await db.execute(
            select(ManagedAccount).where(ManagedAccount.owner_user_id == user.id)
        )).scalars().all()],
        "llm_usage": [_row(u) for u in (await db.execute(
            select(LLMUsage).where(LLMUsage.user_id == user.id)
        )).scalars().all()],
        # The prompt is the part of a generated asset the user cannot recover
        # from the file itself, so the rows matter as much as the bytes.
        "media_assets": [_row(a) for a in (await db.execute(
            select(MediaAsset).where(MediaAsset.user_id == user.id)
            .order_by(MediaAsset.created_at)
        )).scalars().all()],
        "video_publish_jobs": [_row(j) for j in (await db.execute(
            select(VideoPublishJob).where(VideoPublishJob.user_id == user.id)
            .order_by(VideoPublishJob.created_at)
        )).scalars().all()],
        "workspace": await _collect_workspace(db, user),
    }


async def _collect_workspace(db, user: User) -> Optional[dict]:
    ws = (await db.execute(
        select(Workspace).where(Workspace.owner_user_id == user.id)
    )).scalars().first()
    if ws is None:
        return None
    doc = _row(ws)
    doc["sources"] = [_row(s) for s in (await db.execute(
        select(Source).where(Source.workspace_id == ws.id))).scalars().all()]
    doc["leads"] = [_row(x) for x in (await db.execute(
        select(Lead).where(Lead.workspace_id == ws.id))).scalars().all()]
    doc["audit"] = [_row(a) for a in (await db.execute(
        select(AuditEntry).where(AuditEntry.workspace_id == ws.id))).scalars().all()]
    rules = (await db.execute(
        select(BrandRules).where(BrandRules.workspace_id == ws.id))).scalars().first()
    doc["brand_rules"] = _row(rules) if rules else None
    return doc


# ---------------------------------------------------------------- media


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def safe_media_files(paths: Iterable[Optional[str]], root: Path) -> list[Path]:
    """Existing files that genuinely live under `root`.

    The paths come out of the database as absolute strings. Resolving symlinks
    before the containment check is the point: a link inside uploads pointing at
    /etc is otherwise a file-read primitive with a download button on it.
    """
    root = root.resolve()
    picked: list[Path] = []
    for raw in paths:
        if not raw:
            continue
        try:
            p = Path(raw).resolve()
        except OSError:
            continue
        if _under(p, root) and p.is_file():
            picked.append(p)
    return picked


def arcname_for(path: Path, root: Path) -> str:
    return "media/" + path.resolve().relative_to(root.resolve()).as_posix()


async def user_media_paths(db, user: User) -> list[str]:
    """Every on-disk file this account owns, as raw DB strings."""
    post_ids = [p.id for p in (await db.execute(
        select(Post).where(Post.user_id == user.id))).scalars().all()]
    # Every brand's logo, not just the User row's copy. That copy is only a
    # rollback snapshot since UX phase 2, and an agency's client logos were
    # never in this list at all — so they were neither exported nor erased.
    paths: list[Optional[str]] = [a.logo_path for a in (await db.execute(
        select(ManagedAccount).where(ManagedAccount.owner_user_id == user.id)
    )).scalars().all()]
    paths.append(user.logo_path)
    if post_ids:
        paths += [p.video_path for p in (await db.execute(
            select(Post).where(Post.id.in_(post_ids)))).scalars().all()]
        for slide in (await db.execute(
            select(Slide).where(Slide.post_id.in_(post_ids)))).scalars().all():
            paths += [slide.image_path, slide.raw_image_path]
    # Library assets are not reachable through any post — that is the whole
    # point of them — so they have to be walked on their own.
    paths += [a.file_path for a in (await db.execute(
        select(MediaAsset).where(MediaAsset.user_id == user.id))).scalars().all()]
    return [p for p in paths if p]


def write_export_zip(target: Path, data: dict, files: Iterable[Path],
                     root: Path) -> Path:
    """Build the archive on disk. A file that disappears mid-build is skipped:
    the daily orphan sweep can delete one while we're writing."""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.json", json.dumps(data, indent=2, ensure_ascii=False,
                                            default=str))
        for f in files:
            try:
                zf.write(f, arcname_for(f, root))
            except (OSError, ValueError) as e:
                log.warning("Export skipped %s: %s", f, e)
    return target


# ---------------------------------------------------------------- erasure


def delete_user_files(user_id: str, post_ids: Iterable[str],
                      root: Path = UPLOADS_ROOT) -> int:
    """Remove the account's directories. Returns how many were removed.

    Every candidate is re-checked for containment: post ids are database strings,
    and one shaped like `../..` must not turn housekeeping into an rm -rf.
    """
    root = root.resolve()
    targets = [root / "posts" / pid for pid in post_ids]
    targets += [root / name / user_id for name in _USER_DIRS]
    removed = 0
    for d in targets:
        try:
            resolved = d.resolve()
        except OSError:
            continue
        if not _under(resolved, root) or resolved == root or not resolved.is_dir():
            continue
        try:
            shutil.rmtree(resolved)
            removed += 1
        except OSError as e:
            log.warning("Erase could not remove %s: %s", resolved, e)
    return removed


async def delete_user_data(db, user: User, *, root: Path = UPLOADS_ROOT) -> dict:
    """Erase the account and everything hanging off it. Caller commits.

    Children go before parents so the delete never depends on a cascade being
    configured, and so the counts are real rather than assumed.
    """
    counts: dict[str, int] = {}

    async def _delete(model, clause, key) -> list:
        rows = (await db.execute(select(model).where(clause))).scalars().all()
        for row in rows:
            await db.delete(row)
        counts[key] = len(rows)
        return rows

    posts = (await db.execute(
        select(Post).where(Post.user_id == user.id))).scalars().all()
    post_ids = [p.id for p in posts]

    # Disarm before destroying: a live job for a deleted post wakes up, finds
    # nothing, and logs a failure nobody can act on.
    for pid in post_ids:
        try:
            cancel_publish(pid)
        except Exception:                       # a missing scheduler is not fatal
            log.debug("No scheduler job to cancel for post=%s", pid)

    ws = (await db.execute(
        select(Workspace).where(Workspace.owner_user_id == user.id))).scalars().first()

    if ws is not None:
        src_ids = [s.id for s in (await db.execute(
            select(Source).where(Source.workspace_id == ws.id))).scalars().all()]
        if src_ids:
            await _delete(SourceSnapshot, SourceSnapshot.source_id.in_(src_ids),
                          "source_snapshots")
        else:
            counts["source_snapshots"] = 0
        await _delete(Lead, Lead.workspace_id == ws.id, "leads")
        await _delete(AuditEntry, AuditEntry.workspace_id == ws.id, "audit_entries")
        await _delete(BrandRules, BrandRules.workspace_id == ws.id, "brand_rules")
        await _delete(Source, Source.workspace_id == ws.id, "sources")
    else:
        for key in ("source_snapshots", "leads", "audit_entries", "brand_rules",
                    "sources"):
            counts[key] = 0

    if post_ids:
        await _delete(Slide, Slide.post_id.in_(post_ids), "slides")
        await _delete(PostInsight, PostInsight.post_id.in_(post_ids), "insights")
    else:
        counts["slides"] = counts["insights"] = 0

    # Before posts and media_assets: a job carries FKs to both (post_id CASCADE,
    # asset_id SET NULL), and the rule here is children first, never leaning on
    # a cascade being configured.
    await _delete(VideoPublishJob, VideoPublishJob.user_id == user.id,
                  "video_publish_jobs")

    for p in posts:
        await db.delete(p)
    counts["posts"] = len(posts)

    await _delete(LLMUsage, LLMUsage.user_id == user.id, "llm_usage")
    # Before managed_accounts: an asset carries a nullable FK to one, and the
    # rule in this function is children first, never leaning on a cascade.
    await _delete(MediaAsset, MediaAsset.user_id == user.id, "media_assets")
    await _delete(ManagedAccount, ManagedAccount.owner_user_id == user.id,
                  "managed_accounts")
    await _delete(UserCredentials, UserCredentials.user_id == user.id, "credentials")
    if ws is not None:
        await db.delete(ws)
    counts["workspaces"] = 1 if ws is not None else 0

    await db.delete(user)
    await db.flush()

    counts["files"] = delete_user_files(user.id, post_ids, root)
    return counts
