"""The media library: standalone image/video assets, independent of any post.

Everything else in this schema ties media to a post — a Slide needs a post_id,
a reel is one column on Post. This is the other half: generate or upload
something now, decide later whether and where it goes. See MediaAsset's
docstring in models/database.py for why the row is the job and why nothing
points from an asset back to a post.

Route order matters: literal paths (`/uploads`) are declared before the
`/{asset_id}` family so a path segment is never matched as an id.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import (
    get_current_user, get_db, get_effective_settings, get_image_provider, get_text_provider,
)
from api.ratelimit import limiter
from config import Settings
from models.database import MediaAsset as MediaAssetModel, User as UserModel
from models.schemas import (
    EditVideoRequest, GenerateImageRequest, GenerateVideoRequest, MediaAssetDetail,
    MediaAssetSummary, StagedUpload, SuggestVideoIdeaRequest, SuggestVideoIdeaResponse,
)
from services import media_store, music_store, staging
from services.ai.base import AIError
from services.ai.catalog import estimate_video_cost
from services.subtitles import chunk_segments, write_ass
from services.tts import (
    ElevenLabsTTS, TTSError, concat_wavs, loop_music_only, mix_with_music, mp3_to_wav,
)
from services.user_settings import resolve_ai_choice, resolve_user_profile
from services.video.assemble import mux_reel, prepend_cover, render_cover
from services.video.base import VideoError
from services.video.clip_edit import grab_frame, reframe_clip, trim_clip
from services.video.genai.base import VideoGenError
from services.video.genai.factory import get_gen_video_provider
from services.video.normalize import (
    XFADE_SEC, align_to_duration, concat_clips, concat_clips_xfade, probe_video,
)

router = APIRouter(prefix="/api/media", tags=["media"])
log = logging.getLogger(__name__)

_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_VIDEO_TYPES = {"video/mp4"}
_ACCEPTED_TYPES = _IMAGE_TYPES | _VIDEO_TYPES
#: Matches posts._MAX_UPLOAD_BYTES for images; video gets more headroom.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_VIDEO_BYTES = 200 * 1024 * 1024
_MAX_UPLOAD_FILES = 10
_CHUNK_BYTES = 1024 * 1024


def _kind_for(content_type: str) -> str:
    return "video" if content_type in _VIDEO_TYPES else "image"


def _probe_image(data: bytes) -> tuple[str, int, int]:
    """The real mime + dimensions, read from the bytes themselves rather than
    assumed — a provider's docstring says "JPEG/PNG" but doesn't promise which,
    and storing the wrong one would both mislabel the asset and pick the wrong
    file extension in media_store."""
    from PIL import Image
    with Image.open(io.BytesIO(data)) as img:
        mime = Image.MIME.get(img.format, "image/jpeg")
        return mime, img.width, img.height


def _asset_url(asset: MediaAssetModel) -> Optional[str]:
    """None while an asset has no servable bytes yet — a pending or failed
    generation has nothing behind the URL, so there is no URL."""
    return f"/api/media/{asset.id}/file" if asset.status == "ready" else None


def _summary(asset: MediaAssetModel) -> MediaAssetSummary:
    return MediaAssetSummary(
        id=asset.id, kind=asset.kind, status=asset.status, source=asset.source,
        url=_asset_url(asset), title=asset.title, width=asset.width,
        height=asset.height, duration_sec=asset.duration_sec, bytes=asset.bytes,
        created_at=asset.created_at,
    )


def _detail(asset: MediaAssetModel) -> MediaAssetDetail:
    return MediaAssetDetail(
        **_summary(asset).model_dump(), provider=asset.provider, model=asset.model,
        prompt=asset.prompt, error=asset.error, parent_asset_id=asset.parent_asset_id,
    )


async def _owned_asset(db: AsyncSession, asset_id: str, user: UserModel) -> MediaAssetModel:
    """Fetch an asset the user is allowed to touch, else 404 (not 403 — don't
    reveal that another tenant's asset exists). Mirrors api.deps.owned_post."""
    stmt = select(MediaAssetModel).where(MediaAssetModel.id == asset_id)
    if not user.is_local:
        stmt = stmt.where(MediaAssetModel.user_id == user.id)
    asset = (await db.execute(stmt)).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


async def _stream_to_temp(file: UploadFile, max_bytes: int) -> tuple[Path, int]:
    """Write the upload to a temp file in bounded chunks, never holding more
    than one chunk in memory. A video-sized file read in one shot the way
    posts.stage_uploads reads images is a real memory-exhaustion vector this
    route cannot inherit."""
    fd, tmp_name = tempfile.mkstemp(prefix="media_upload_")
    tmp_path = Path(tmp_name)
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large (max {max_bytes // (1024 * 1024)} MB)")
                out.write(chunk)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    if total == 0:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Empty file")
    return tmp_path, total


# ─────────────────────────────────────────────────────────────────────────────
# Generate — an AI image, synchronous (unlike video, this returns in seconds)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/images", response_model=MediaAssetDetail)
@limiter.limit("15/minute;150/hour")
async def generate_image_asset(
    request: Request,
    body: GenerateImageRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_effective_settings)],
    image_provider: Annotated[object, Depends(get_image_provider)],
) -> MediaAssetDetail:
    # Same two-step resolution posts.py uses everywhere: the dependency builds
    # the provider client from the tenant's key, this call separately reads
    # which model they picked — resolve_ai_choice is cheap and side-effect-free,
    # so calling it twice costs nothing and keeps the two concerns apart.
    provider_name, model, _key = resolve_ai_choice(user, settings, "image")
    if image_provider is None:
        raise HTTPException(
            status_code=400,
            detail="No image provider configured. Choose one in Account → AI models.")
    if not model:
        raise HTTPException(
            status_code=400,
            detail="No image model selected. Choose one in Account → AI models.")

    try:
        image_bytes = await image_provider.generate_image(model=model, prompt=body.prompt)
    except AIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    mime, width, height = _probe_image(image_bytes)
    asset = MediaAssetModel(user_id=user.id, kind="image", source="ai_gen",
                            status="ready", provider=provider_name, model=model,
                            prompt=body.prompt, title=body.title or "",
                            mime=mime, width=width, height=height,
                            bytes=len(image_bytes))
    db.add(asset)
    await db.flush()   # need asset.id before the file can be named on disk
    try:
        path = media_store.save(str(user.id), asset.id, image_bytes, mime)
    except media_store.MediaError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    asset.file_path = str(path)
    await db.commit()
    return _detail(asset)


# ─────────────────────────────────────────────────────────────────────────────
# Generate — a video via Kling. Asynchronous, unlike images: this only creates
# the provider's task and a pending MediaAsset; services/video_poll.py finishes
# the job once Kling reports it done.
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/videos", response_model=MediaAssetDetail)
@limiter.limit("6/minute;30/hour")
async def generate_video_asset(
    request: Request,
    body: GenerateVideoRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_effective_settings)],
) -> MediaAssetDetail:
    # Kling isn't part of PROVIDERS/resolve_ai_choice — no text/image models, no
    # per-token pricing — so its key is read the same direct way as
    # elevenlabs_api_key/pexels_api_key, not through that machinery.
    if not settings.kling_api_key:
        raise HTTPException(
            status_code=400,
            detail="No Kling key configured. Add one in Account → API keys.")

    image_bytes = None
    if body.image_asset_id:
        seed = await _owned_asset(db, body.image_asset_id, user)
        if seed.kind != "image":
            raise HTTPException(status_code=400,
                                detail="Only an image asset can be animated into a video.")
        if seed.status != "ready":
            raise HTTPException(status_code=400, detail="That asset isn't ready yet.")
        image_bytes = media_store.read(seed.user_id, seed.id)

    model = body.model or "kling-v1-6"
    client = get_gen_video_provider("kling", settings.kling_api_key,
                                   ssl_verify=settings.ssl_verify)
    try:
        task_id = await client.create_task(
            prompt=body.prompt, model=model, duration_sec=body.duration_sec,
            aspect_ratio=body.aspect_ratio, image_bytes=image_bytes)
    except VideoGenError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    finally:
        await client.close()

    # Only on success: a failed create_task leaves no row behind, same as a
    # failed image generation — nothing for the user to find and be confused by.
    asset = MediaAssetModel(
        user_id=user.id, kind="video", source="ai_gen", status="pending",
        provider="kling", model=model, prompt=body.prompt, title=body.title or "",
        provider_task_id=task_id,
        cost_usd=estimate_video_cost("kling", model, body.duration_sec),
    )
    db.add(asset)
    await db.commit()
    return _detail(asset)


_IDEA_SYSTEM_PROMPT = (
    "You suggest a single vivid, concrete, filmable video concept for a short "
    "social clip (5-10 seconds). Answer with ONE or TWO sentences describing "
    "exactly what the camera sees — no preamble, no options, no surrounding quotes."
)


@router.post("/videos/suggest-idea", response_model=SuggestVideoIdeaResponse)
@limiter.limit("15/minute;150/hour")
async def suggest_video_idea(
    request: Request,
    body: SuggestVideoIdeaRequest,
    user: Annotated[UserModel, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_effective_settings)],
    text_provider: Annotated[object, Depends(get_text_provider)],
) -> SuggestVideoIdeaResponse:
    _provider_name, model, _key = resolve_ai_choice(user, settings, "text")
    if text_provider is None or not model:
        raise HTTPException(
            status_code=400,
            detail="No text model configured. Choose one in Account → AI models.")

    profile = resolve_user_profile(user)
    niche = body.niche or profile.get("niche")
    audience = profile.get("target_audience")
    user_prompt = f"Niche: {niche or 'general'}."
    if audience:
        user_prompt += f" Audience: {audience}."

    try:
        content, _citations = await text_provider.generate_text(
            model=model, system_prompt=_IDEA_SYSTEM_PROMPT, user_prompt=user_prompt,
            max_tokens=150)
    except AIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return SuggestVideoIdeaResponse(prompt=content.strip())


# ─────────────────────────────────────────────────────────────────────────────
# Manual upload — the first source of assets, ahead of any generation
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/uploads", response_model=list[MediaAssetSummary])
async def upload_media(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    files: list[UploadFile] = File(...),
) -> list[MediaAssetSummary]:
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(files) > _MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=422,
            detail=f"Too many files: {len(files)}. At most {_MAX_UPLOAD_FILES} at a time.")

    out: list[MediaAssetSummary] = []
    for file in files:
        content_type = file.content_type or ""
        if content_type not in _ACCEPTED_TYPES:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported file type {content_type!r}. Allowed: jpeg, png, webp, mp4.",
            )
        kind = _kind_for(content_type)
        max_bytes = _MAX_VIDEO_BYTES if kind == "video" else _MAX_IMAGE_BYTES
        tmp_path, size = await _stream_to_temp(file, max_bytes)

        asset = MediaAssetModel(user_id=user.id, kind=kind, source="upload",
                                status="ready", title=file.filename or "",
                                mime=content_type, bytes=size)
        db.add(asset)
        await db.flush()   # need asset.id before the file can be named on disk
        try:
            path = media_store.adopt_file(str(user.id), asset.id, tmp_path, content_type)
        except media_store.MediaError as e:
            # Belt-and-suspenders: the allowlist above should make this
            # unreachable, but an unhandled MediaError would otherwise surface
            # as a bare 500 instead of a normal 4xx.
            raise HTTPException(status_code=400, detail=str(e)) from e
        # gdpr.user_media_paths() reads this column directly rather than asking
        # media_store, so a row with no file_path is invisible to data export
        # even though the file is sitting right there on disk.
        asset.file_path = str(path)
        out.append(_summary(asset))
    await db.commit()
    return out


# ─────────────────────────────────────────────────────────────────────────────
# List / get / delete
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[MediaAssetSummary])
async def list_media(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    kind: Optional[str] = Query(None),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[MediaAssetSummary]:
    if kind is not None and kind not in ("image", "video"):
        raise HTTPException(status_code=400, detail=f"Unknown kind: {kind!r}")
    stmt = (select(MediaAssetModel).order_by(MediaAssetModel.created_at.desc())
            .limit(limit).offset(offset))
    if not user.is_local:
        stmt = stmt.where(MediaAssetModel.user_id == user.id)
    if kind is not None:
        stmt = stmt.where(MediaAssetModel.kind == kind)
    rows = (await db.execute(stmt)).scalars().all()
    return [_summary(a) for a in rows]


@router.get("/{asset_id}", response_model=MediaAssetDetail)
async def get_media(
    asset_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> MediaAssetDetail:
    asset = await _owned_asset(db, asset_id, user)
    return _detail(asset)


@router.delete("/{asset_id}", status_code=204)
async def delete_media(
    asset_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> None:
    asset = await _owned_asset(db, asset_id, user)
    media_store.delete(asset.user_id, asset.id)
    await db.delete(asset)
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Serve bytes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{asset_id}/file")
async def get_media_file(
    asset_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FileResponse:
    # Intentionally UNGATED, same posture as get_slide_image and get_reel_video:
    # an <img>/<video> src cannot carry a Bearer token. The blast radius here is
    # larger than those two — a library accumulates everything a tenant ever
    # generated, not just what is about to publish — so this route additionally
    # (a) serves only status="ready" rows, never a pending or failed one, and
    # (b) re-derives the path from media_store rather than trusting the
    # file_path column, so a bad write can't point this at an arbitrary file.
    asset = await db.get(MediaAssetModel, asset_id)
    if asset is None or asset.status != "ready":
        raise HTTPException(status_code=404, detail="Asset not found")
    path = media_store.path_for(asset.user_id, asset.id)
    if path is None:
        raise HTTPException(status_code=404, detail="Asset file not found on disk")
    # Headers go on the FileResponse itself: a value set on an injected
    # `response: Response` dependency is silently dropped once the endpoint
    # returns a different Response object, which FileResponse always is.
    return FileResponse(
        str(path), media_type=asset.mime or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=300",
                "X-Content-Type-Options": "nosniff"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stage — hand a library image to a fresh generation via the existing upload_ids
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{asset_id}/stage", response_model=StagedUpload)
async def stage_media_asset(
    asset_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> StagedUpload:
    """Copy a library image into staging so /api/posts/generate's upload_ids
    can refer to it by id, same as a photo picked fresh off disk.

    Composes two stores that each already prove their own containment in
    tests, rather than teaching generation a third source of media — staging
    and media_store are different namespaces on purpose, so a staged id and a
    library asset id are never interchangeable.
    """
    asset = await _owned_asset(db, asset_id, user)
    if asset.kind != "image":
        raise HTTPException(status_code=400,
                            detail="Only an image asset can be used to generate a post.")
    if asset.status != "ready":
        raise HTTPException(status_code=400, detail="That asset isn't ready yet.")
    data = media_store.read(asset.user_id, asset.id)
    upload_id = staging.save(str(user.id), data, asset.mime or "image/jpeg")
    return StagedUpload(id=upload_id, filename=asset.title or "", bytes=len(data))


# ─────────────────────────────────────────────────────────────────────────────
# Edit — trim/reframe/concat library video clips into a new asset, optionally
# adding voiceover, music and a cover (Phase 6). Synchronous, no poller: this
# is seconds of local ffmpeg work, the same order of magnitude as
# posts._make_voiceover_reel, which does the same thing synchronously today.
# ─────────────────────────────────────────────────────────────────────────────

def _split_script_into_segments(script: str) -> list[str]:
    """A blank line separates segments; no blank line at all → one segment for
    the whole script. No LLM rewriting: these are the user's own words."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", script) if p.strip()]
    return paragraphs or [script.strip()]


@router.post("/{asset_id}/edit", response_model=MediaAssetDetail)
@limiter.limit("6/minute;30/hour")
async def edit_video_asset(
    asset_id: str,
    request: Request,
    body: EditVideoRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserModel, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_effective_settings)],
) -> MediaAssetDetail:
    """The URL asset anchors the route (owned_post-style guard); every clip in
    the body is independently owned-checked before any ffmpeg call runs — a
    foreign id anywhere in the list rejects the whole request up front."""
    await _owned_asset(db, asset_id, user)

    owned_clips: list[MediaAssetModel] = []
    for clip in body.clips:
        clip_asset = await _owned_asset(db, clip.asset_id, user)
        if clip_asset.kind != "video":
            raise HTTPException(status_code=400,
                                detail="Only video assets can be edited into a clip.")
        if clip_asset.status != "ready":
            raise HTTPException(status_code=400, detail="That asset isn't ready yet.")
        owned_clips.append(clip_asset)

    if body.voiceover and not (body.voiceover_script or "").strip():
        raise HTTPException(status_code=400, detail="Voiceover needs a script.")
    if body.voiceover and not settings.elevenlabs_api_key:
        raise HTTPException(
            status_code=400,
            detail="Voiceover needs an ElevenLabs API key — add it in Account → API keys.")
    music_path = music_store.path_for(str(user.id)) if body.music else None
    if body.music and music_path is None:
        raise HTTPException(
            status_code=400,
            detail="Background music needs an uploaded track — add one in Account.")

    n = len(owned_clips)
    tmpdir = Path(tempfile.mkdtemp(prefix="clipedit_"))
    try:
        raw_paths: list[Path] = []
        src_durs: list[float] = []
        for i, clip_asset in enumerate(owned_clips):
            data = media_store.read(clip_asset.user_id, clip_asset.id)
            raw = tmpdir / f"raw_{i:02d}.mp4"
            raw.write_bytes(data)
            raw_paths.append(raw)
            src_durs.append(probe_video(raw)[2])

        starts = [c.trim_start_sec for c in body.clips]
        user_ends = [c.trim_end_sec if c.trim_end_sec is not None else src_durs[i]
                    for i, c in enumerate(body.clips)]
        durations = [max(0.0, ue - s) for s, ue in zip(starts, user_ends, strict=True)]

        fade = 0.0
        if body.transitions:
            # each non-last clip's surplus of real source material past its
            # requested trim end, capped at XFADE_SEC — the crossfade eats
            # this, never the content the user actually asked to keep.
            capacities = [max(0.0, min(XFADE_SEC, src_durs[i] - user_ends[i]))
                         for i in range(n - 1)]
            fade = min(capacities)

        edited_paths: list[Path] = []
        for i in range(n):
            end = user_ends[i]
            if body.transitions and i < n - 1:
                end = min(src_durs[i], user_ends[i] + fade)
            trimmed = tmpdir / f"trim_{i:02d}.mp4"
            try:
                await trim_clip(raw_paths[i], trimmed, start_sec=starts[i], end_sec=end)
            except VideoError as e:
                raise HTTPException(status_code=502, detail=f"Trim failed: {e}") from e
            reframed = tmpdir / f"reframe_{i:02d}.mp4"
            try:
                await reframe_clip(trimmed, reframed)
            except VideoError as e:
                raise HTTPException(status_code=502, detail=f"Reframe failed: {e}") from e
            edited_paths.append(reframed)

        video_tmp = tmpdir / "video.mp4"
        try:
            if body.transitions:
                await concat_clips_xfade(edited_paths, durations, video_tmp, fade=fade)
                aligned = tmpdir / "aligned.mp4"
                await align_to_duration(video_tmp, aligned, sum(durations))
                video_tmp = aligned
                video_dur = sum(durations)
            else:
                await concat_clips(edited_paths, video_tmp)
                video_dur = probe_video(video_tmp)[2]
        except VideoError as e:
            raise HTTPException(status_code=502, detail=f"Concat failed: {e}") from e

        audio_in: Optional[Path] = None
        ass_path: Optional[Path] = None
        if body.voiceover:
            segments = _split_script_into_segments(body.voiceover_script or "")
            voice = (body.voice_id or "").strip() or settings.elevenlabs_voice_id
            gap = 0.35
            try:
                tts = ElevenLabsTTS(settings.elevenlabs_api_key,
                                    ssl_verify=settings.ssl_verify)
                wavs: list[Path] = []
                seg_durs: list[float] = []
                for i, text in enumerate(segments):
                    mp3 = await tts.synthesize(text, voice_id=voice)
                    wav = tmpdir / f"seg_{i:02d}.wav"
                    seg_durs.append(await mp3_to_wav(mp3, wav))
                    wavs.append(wav)
                track = tmpdir / "voice.m4a"
                total_voice = await concat_wavs(wavs, track, gap_sec=gap)
            except TTSError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

            aligned_to_voice = tmpdir / "video_voice_aligned.mp4"
            try:
                await align_to_duration(video_tmp, aligned_to_voice, total_voice)
            except VideoError as e:
                raise HTTPException(status_code=502, detail=f"Align failed: {e}") from e
            video_tmp = aligned_to_voice
            video_dur = total_voice

            ass_path = tmpdir / "subs.ass"
            advance = [d + gap for d in seg_durs]
            ass_path.write_text(
                write_ass(chunk_segments(segments, seg_durs, advance_durs=advance)),
                encoding="utf-8")
            audio_in = track

        if body.music:
            if audio_in is not None:
                mixed = tmpdir / "mixed.m4a"
                try:
                    await mix_with_music(audio_in, music_path, mixed, total_dur=video_dur)
                except TTSError as e:
                    raise HTTPException(status_code=502,
                                        detail=f"Music mix failed: {e}") from e
                audio_in = mixed
            else:
                looped = tmpdir / "music_only.m4a"
                try:
                    await loop_music_only(music_path, looped, video_dur)
                except TTSError as e:
                    raise HTTPException(status_code=502,
                                        detail=f"Music mix failed: {e}") from e
                audio_in = looped

        if body.cover:
            try:
                frame = grab_frame(video_tmp, at_sec=0.0)
                cover_mp4 = tmpdir / "cover.mp4"
                await render_cover(frame, cover_mp4)
                covered = tmpdir / "covered.mp4"
                await prepend_cover(cover_mp4, video_tmp, covered, video_dur)
                video_tmp = covered
            except VideoError as e:
                raise HTTPException(status_code=502,
                                    detail=f"Cover render failed: {e}") from e

        # §6.3: with neither voiceover nor music, the trimmed/concatenated
        # video IS the final asset — no mux_reel call, the result is silent.
        if audio_in is not None:
            out_tmp = tmpdir / "final.mp4"
            try:
                await mux_reel(video_tmp, audio_in, ass_path, out_tmp)
            except VideoError as e:
                raise HTTPException(status_code=502, detail=f"Assembly failed: {e}") from e
            final_path = out_tmp
        else:
            final_path = video_tmp

        data = final_path.read_bytes()
        _w, _h, real_dur = probe_video(final_path)

        asset = MediaAssetModel(
            user_id=user.id, kind="video", source="edited", status="ready",
            parent_asset_id=owned_clips[0].id, title=body.title or "",
            mime="video/mp4", width=1080, height=1920,
            duration_sec=real_dur, bytes=len(data),
        )
        db.add(asset)
        await db.flush()   # need asset.id before the file can be named on disk
        try:
            path = media_store.save(str(user.id), asset.id, data, "video/mp4")
        except media_store.MediaError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        asset.file_path = str(path)
        await db.commit()
        return _detail(asset)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
