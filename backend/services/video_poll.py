"""Server-side polling for prompt/image-to-video generation (Kling and, later,
whatever else lands in services/video/genai/).

Generation takes minutes and the provider's result URL is temporary, so this
cannot run in a browser tab — a closed tab would mean a lost, already-paid-for
video. Runs every ~20s from services/scheduler.py.

Each MediaAsset row is polled independently with its own try/except: one
tenant's crash (or a permanent per-asset failure) must never stop the next
asset — of theirs or anyone else's — from being checked in the same sweep.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from services import media_store
from services.user_settings import build_settings_for_user
from services.video.genai.base import VideoGenError
from services.video.genai.factory import get_gen_video_provider
from services.video.normalize import probe_video

log = logging.getLogger(__name__)

#: After this long with no terminal result, stop waiting — a permanently wedged
#: task (or one whose provider never answers) must not keep a row pending forever.
_TIMEOUT = timedelta(minutes=10)

#: Which Settings field holds each provider's key. Only Kling exists today;
#: Runway/Luma/Veo (phase 7) each add their own entry when their adapter does.
_KEY_FIELDS = {"kling": "kling_api_key"}


def _key_for(settings, provider: str) -> str:
    field = _KEY_FIELDS.get(provider)
    return getattr(settings, field, "") if field else ""


def _is_overdue(asset) -> bool:
    created = asset.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > _TIMEOUT


async def run_video_poll(sessionmaker) -> dict:
    """Check every video MediaAsset still waiting on its provider.

    Returns {"ready": n, "failed": n} for the caller to log — not raised on
    error, since a crash here must not take the scheduler down with it (the
    wrapper in services/scheduler.py holds that guarantee at the outer level;
    this one holds it per-row, which is the finer-grained half of the same
    promise).
    """
    from models.database import MediaAsset as MediaAssetModel, User as UserModel

    counts = {"ready": 0, "failed": 0}
    async with sessionmaker() as db:
        rows = (await db.execute(select(MediaAssetModel).where(
            MediaAssetModel.kind == "video",
            MediaAssetModel.status.in_(("pending", "running")),
            MediaAssetModel.provider_task_id.isnot(None),
        ))).scalars().all()

        for asset in rows:
            try:
                await _poll_one(db, asset, UserModel)
            except Exception as e:
                log.error("Video poll crashed for asset=%s: %s", asset.id, e)
                continue
            if asset.status == "ready":
                counts["ready"] += 1
            elif asset.status == "failed":
                counts["failed"] += 1

        await db.commit()
    return counts


async def _poll_one(db, asset, user_model) -> None:
    user = await db.get(user_model, asset.user_id)
    settings = await build_settings_for_user(db, user)
    key = _key_for(settings, asset.provider)
    if not key:
        asset.status = "failed"
        asset.error = f"The {asset.provider} key was removed before generation finished."
        return

    client = get_gen_video_provider(asset.provider, key)
    try:
        try:
            status = await client.poll(asset.provider_task_id)
        except VideoGenError as e:
            # Transient (a network blip talking to the provider) is not the
            # same as permanently stuck — only the timeout gets to call it a
            # failure; otherwise leave the row untouched for the next tick.
            if _is_overdue(asset):
                asset.status = "failed"
                asset.error = f"Timed out waiting for {asset.provider}: {e}"
            return

        if status.state == "succeed":
            try:
                video_bytes = await client.download(status.video_url)
            except VideoGenError as e:
                asset.status = "failed"
                asset.error = f"Download failed: {e}"
                return
            path = media_store.save(asset.user_id, asset.id, video_bytes, "video/mp4")
            # Not the duration that was requested: the provider can round or
            # extend it, so the real file is the only source of truth.
            width, height, duration_sec = await asyncio.to_thread(probe_video, path)
            asset.file_path = str(path)
            asset.width, asset.height, asset.duration_sec = width, height, duration_sec
            asset.bytes = len(video_bytes)
            asset.status = "ready"
        elif status.state == "failed":
            asset.status = "failed"
            asset.error = status.error or "Generation failed"
        elif _is_overdue(asset):
            asset.status = "failed"
            asset.error = f"Timed out waiting for {asset.provider}"
        else:
            # Confirmed in flight (as opposed to "pending" — not yet polled at
            # all), so the UI can tell the two states apart.
            asset.status = "running"
    finally:
        await client.close()
