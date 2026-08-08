"""Admin: LLM cost tracking + backup/restore."""
from __future__ import annotations

import io
import os
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import (
    get_current_user, get_db, get_effective_settings, get_settings, require_admin,
)
from config import Settings
from models.database import LLMUsage, User as UserModel
from services.app_spend import flush_usage
from services.generation_credits import free_allowance

router = APIRouter(prefix="/api", tags=["admin"])

_BACKEND_DIR = Path(__file__).resolve().parents[2]   # backend/
_UPLOADS_DIR = _BACKEND_DIR / "uploads"


# ─────────────────────────────────────────────────────────────────────────────
# Cost tracking
# ─────────────────────────────────────────────────────────────────────────────

#: Turning the in-memory usage buffer into rows now has a second reader — the
#: daily ceiling on the application's own spend (UX phase 6.1) — so it lives in
#: services/app_spend.py. Two copies would drift, and the ceiling seeing less
#: than was actually spent is the direction that costs money.
_flush_usage = flush_usage


@router.get("/usage")
async def get_usage(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    effective: Annotated[Settings, Depends(get_effective_settings)],
) -> dict:
    """Flush buffered LLM usage and return today/month aggregates + by-model,
    scoped to the current user (the local desktop user sees everything).

    Also carries what is left of the free allowance (UX phase 6.4). It rides
    here rather than on an endpoint of its own because the header already polls
    this one on a timer, and a second poll for one integer would be a second
    thing to keep in step with the first.
    """
    await _flush_usage(db)
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _scope(stmt):
        return stmt if user.is_local else stmt.where(LLMUsage.user_id == user.id)

    async def _agg(since):
        r = await db.execute(_scope(
            select(func.coalesce(func.sum(LLMUsage.cost), 0.0),
                   func.coalesce(func.sum(LLMUsage.total_tokens), 0),
                   func.count(LLMUsage.id))
            .where(LLMUsage.created_at >= since)
        ))
        cost, tokens, calls = r.one()
        return {"cost": round(float(cost or 0), 4), "tokens": int(tokens or 0), "calls": int(calls or 0)}

    by_model_rows = await db.execute(_scope(
        select(LLMUsage.model, func.coalesce(func.sum(LLMUsage.cost), 0.0), func.count(LLMUsage.id))
        .where(LLMUsage.created_at >= month_start)
        .group_by(LLMUsage.model).order_by(func.sum(LLMUsage.cost).desc())
    ))
    by_model = [
        {"model": m, "cost": round(float(c or 0), 4), "calls": int(n)}
        for (m, c, n) in by_model_rows.all()
    ]
    return {
        "today": await _agg(day_start),
        "month": await _agg(month_start),
        "by_model": by_model,
        # None when the subject does not apply — desktop, an account with its
        # own key, or a deployment that offers nothing free. The SPA shows
        # nothing at all in that case rather than "0 left".
        "free": free_allowance(user, effective, settings),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Backup / restore
# ─────────────────────────────────────────────────────────────────────────────

def _is_sqlite(database_url: str) -> bool:
    return database_url.startswith("sqlite")


def _sqlite_file(database_url: str) -> Path:
    # sqlite+aiosqlite:///./insta.db  →  backend/insta.db
    tail = database_url.split(":///", 1)[-1]
    p = Path(tail)
    return p if p.is_absolute() else (_BACKEND_DIR / p).resolve()


def _pg_command(database_url: str) -> tuple[str, dict]:
    """Return a (password-free URL, env-with-PGPASSWORD) for pg_dump/psql so the
    DB password never lands in the process argv (visible via `ps`)."""
    from urllib.parse import urlsplit, urlunsplit

    url = database_url.replace("+asyncpg", "").replace("postgres://", "postgresql://")
    parts = urlsplit(url)
    env = dict(os.environ)
    if parts.password:
        env["PGPASSWORD"] = parts.password
        netloc = parts.hostname or ""
        if parts.username:
            netloc = f"{parts.username}@{netloc}"
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url, env


@router.get("/admin/backup", dependencies=[Depends(require_admin)])
async def backup(settings: Annotated[Settings, Depends(get_settings)]) -> StreamingResponse:
    """Download a backup ZIP.

    Local (sqlite): the db file + uploads/ tree.
    Cloud (Postgres): a pg_dump .sql + uploads/ if present.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if _is_sqlite(settings.database_url):
            dbfile = _sqlite_file(settings.database_url)
            if dbfile.exists():
                zf.write(dbfile, "insta.db")
        else:
            # Postgres dump via pg_dump (needs postgresql-client in the image).
            # --clean --if-exists so restoring drops+recreates instead of colliding
            # with tables the app already made via create_all.
            dump_url, pg_env = _pg_command(settings.database_url)
            try:
                res = subprocess.run(
                    ["pg_dump", "--clean", "--if-exists", "--no-owner",
                     "--no-privileges", dump_url],
                    capture_output=True, text=True, timeout=120, env=pg_env,
                )
                if res.returncode != 0:
                    raise HTTPException(status_code=500, detail=f"pg_dump failed: {res.stderr[:300]}")
                zf.writestr("dump.sql", res.stdout)
            except FileNotFoundError:
                raise HTTPException(status_code=500, detail="pg_dump not installed in this environment")

        # uploads/ (slides, raw, reels) — best-effort, may be large.
        if _UPLOADS_DIR.exists():
            for path in _UPLOADS_DIR.rglob("*"):
                if path.is_file():
                    zf.write(path, str(path.relative_to(_BACKEND_DIR)))

    buf.seek(0)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="insta_backup_{ts}.zip"'},
    )


@router.post("/admin/restore", dependencies=[Depends(require_admin)])
async def restore(
    settings: Annotated[Settings, Depends(get_settings)],
    file: UploadFile = File(...),
) -> dict:
    """Restore from a backup ZIP. Local: swaps insta.db (old kept as .bak) and
    uploads/. Cloud: replays dump.sql via psql. Requires an app restart to pick
    up a swapped sqlite file."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Not a valid ZIP")

    names = set(zf.namelist())

    if _is_sqlite(settings.database_url):
        if "insta.db" not in names:
            raise HTTPException(status_code=400, detail="Backup has no insta.db")
        # Write the new db beside the live one; swap requires a restart because
        # the engine holds the current file open (esp. on Windows).
        target = _sqlite_file(settings.database_url)
        restored = target.with_suffix(".restored.db")
        restored.write_bytes(zf.read("insta.db"))
        _extract_uploads(zf)
        return {
            "ok": True,
            "restart_required": True,
            "detail": f"Restored DB written to {restored.name}. "
                      f"Stop the app, replace {target.name} with it, and relaunch.",
        }
    else:
        if "dump.sql" not in names:
            raise HTTPException(status_code=400, detail="Backup has no dump.sql")
        dump_url, pg_env = _pg_command(settings.database_url)
        sql = zf.read("dump.sql").decode("utf-8", "ignore")
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as tmp:
            tmp.write(sql)
            tmp_path = tmp.name
        try:
            res = subprocess.run(["psql", dump_url, "-f", tmp_path],
                                 capture_output=True, text=True, timeout=180, env=pg_env)
            if res.returncode != 0:
                raise HTTPException(status_code=500, detail=f"psql restore failed: {res.stderr[:300]}")
        except FileNotFoundError:
            raise HTTPException(status_code=500, detail="psql not installed in this environment")
        finally:
            os.unlink(tmp_path)
        _extract_uploads(zf)
        return {"ok": True, "restart_required": False, "detail": "Database restored."}


def _extract_uploads(zf: zipfile.ZipFile) -> None:
    uploads_root = (_BACKEND_DIR / "uploads").resolve()
    for name in zf.namelist():
        if name.startswith("uploads/") and not name.endswith("/"):
            dest = (_BACKEND_DIR / name).resolve()
            # zip-slip guard: reject anything that escapes uploads/, however it
            # got there. `uploads/../main.py` resolves inside backend/ and must be
            # dropped just like `uploads/../../etc`.
            if not dest.is_relative_to(uploads_root):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(name))
