"""The personal cabinet: a user stores their own API keys here (cloud mode).

Keys are encrypted before storage (services/secrets.encrypt) and NEVER returned
in plaintext — GET reports only which keys are set, plus a short masked tail.
"""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import _CRED_FIELDS, get_current_user, get_db
from datetime import datetime, timedelta, timezone

from models.database import User as UserModel, UserCredentials as UserCredentialsModel
from models.schemas import (
    NICHE_BOX_PALETTE, AISettingsResponse, AISettingsUpdate, AITestRequest, AITestResponse,
    BrandVoiceResponse, BrandVoiceUpdate, LogoSettingsResponse, MusicSettingsResponse,
    PresetsResponse, PresetsUpdate, ProfileResponse, ProfileUpdate, PublishTestRequest,
    PublishTestResponse, SlideStyleResponse, SlideStyleUpdate, XSettingsResponse, XSettingsUpdate,
)
from services import logo_store, music_store
from services.ai.catalog import IMAGE, PROVIDERS, TEXT, is_valid_provider
from services.brand_engine import BrandConfig
from services.connection_health import days_left, estimate_expiry
from services.brand_voice import DEFAULT_PRESET, is_valid_preset, list_presets
from services.secrets import decrypt, encrypt

# Same limits as the composer's photo uploads (api/routes/posts.py).
_LOGO_MAX_BYTES = 20 * 1024 * 1024
_LOGO_TYPES = {"image/png", "image/webp", "image/jpeg"}

router = APIRouter(prefix="/api/settings", tags=["settings"])

# The plaintext field names a user may set (same keys as _CRED_FIELDS).
_FIELDS = list(_CRED_FIELDS.keys())


class CredentialsUpdate(BaseModel):
    """Every field optional. A present empty string clears that key; an omitted
    field leaves it unchanged."""
    openrouter_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    instagram_access_token: Optional[str] = None
    instagram_user_id: Optional[str] = None
    imgbb_api_key: Optional[str] = None
    x_api_key: Optional[str] = None
    x_api_secret: Optional[str] = None
    x_access_token: Optional[str] = None
    x_access_token_secret: Optional[str] = None
    unsplash_access_key: Optional[str] = None
    pexels_api_key: Optional[str] = None
    elevenlabs_api_key: Optional[str] = None
    kling_api_key: Optional[str] = None


def _mask(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    tail = value[-4:] if len(value) >= 4 else value
    return f"••••{tail}"


@router.get("/credentials")
async def get_credentials(
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Report which keys are set (never the raw values)."""
    creds = await db.get(UserCredentialsModel, user.id)
    out: dict[str, dict] = {}
    for field, column in _CRED_FIELDS.items():
        raw = decrypt(getattr(creds, column) or "") if creds else ""
        out[field] = {"set": bool(raw), "masked": _mask(raw)}
    return out


@router.put("/credentials")
async def put_credentials(
    body: CredentialsUpdate,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    creds = await db.get(UserCredentialsModel, user.id)
    if creds is None:
        creds = UserCredentialsModel(user_id=user.id)
        db.add(creds)
    for field in _FIELDS:
        value = getattr(body, field)
        if value is None:              # omitted → leave unchanged
            continue
        setattr(creds, _CRED_FIELDS[field], encrypt(value))   # "" clears it
        if field == "instagram_access_token" and value:
            # Meta offers no way to read a token's remaining life without rotating
            # it, so the best we can do is assume the full 60 days from now. Flagged
            # as an estimate — a pasted token may already be weeks old.
            _set_health(creds, "instagram",
                        expires_at=estimate_expiry(datetime.now(timezone.utc)),
                        expires_estimated=True)
    await db.commit()
    return {"status": "ok"}


def _set_health(creds, platform: str, **fields) -> None:
    """Merge fields into one network's health record (JSON column, replace whole)."""
    health = dict(creds.connection_health or {})
    record = dict(health.get(platform) or {})
    for key, value in fields.items():
        record[key] = value.isoformat() if isinstance(value, datetime) else value
    health[platform] = record
    creds.connection_health = health          # reassign: SQLAlchemy JSON is not tracked


# ── Brand voice (generation style preference — NOT a secret, stored plain) ──

@router.get("/brand-voice", response_model=BrandVoiceResponse)
async def get_brand_voice(
    user: Annotated[UserModel, Depends(get_current_user)],
) -> BrandVoiceResponse:
    return BrandVoiceResponse(
        preset=user.brand_voice_preset or DEFAULT_PRESET,
        custom=user.brand_voice_custom or "",
        presets=list_presets(),
    )


@router.put("/brand-voice")
async def put_brand_voice(
    body: BrandVoiceUpdate,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    if body.preset is not None:
        if not is_valid_preset(body.preset):
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="Unknown brand voice preset")
        user.brand_voice_preset = body.preset
    if body.custom is not None:            # "" clears the custom text
        user.brand_voice_custom = body.custom.strip() or None
    # A named preset means the custom text no longer applies; keep it stored but it's
    # only used when preset == "custom".
    await db.commit()
    return {"status": "ok"}


# ── Brand profile (niche/audience/brand — generation defaults, NOT a secret) ──

@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    user: Annotated[UserModel, Depends(get_current_user)],
) -> ProfileResponse:
    return ProfileResponse(
        niche=user.niche or "",
        target_audience=user.target_audience or "",
        brand_name=user.brand_name or "",
    )


@router.put("/profile")
async def put_profile(
    body: ProfileUpdate,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    if body.niche is not None:                     # "" clears
        user.niche = body.niche.strip() or None
    if body.target_audience is not None:
        user.target_audience = body.target_audience.strip() or None
    if body.brand_name is not None:
        user.brand_name = body.brand_name.strip() or None
    await db.commit()
    return {"status": "ok"}


# ── Slide colours (per-tenant branding of generated slides) ──────────────────

@router.get("/slide-style", response_model=SlideStyleResponse)
async def get_slide_style(
    user: Annotated[UserModel, Depends(get_current_user)],
) -> SlideStyleResponse:
    return SlideStyleResponse(
        accent_color=user.slide_accent_color or "",
        text_box_color=user.slide_text_box_color or "",
        default_accent_color=BrandConfig().niche_box_color,
        palette=NICHE_BOX_PALETTE,
    )


@router.put("/slide-style")
async def put_slide_style(
    body: SlideStyleUpdate,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    if body.accent_color is not None:          # "" resets to the platform default
        user.slide_accent_color = body.accent_color or None
    if body.text_box_color is not None:
        user.slide_text_box_color = body.text_box_color or None
    await db.commit()
    return {"status": "ok"}


# ── X plan (kept apart from the brand profile: it's platform mechanics) ─────

@router.get("/x", response_model=XSettingsResponse)
async def get_x_settings(
    user: Annotated[UserModel, Depends(get_current_user)],
) -> XSettingsResponse:
    return XSettingsResponse(x_premium=bool(user.x_premium))


@router.put("/x")
async def put_x_settings(
    body: XSettingsUpdate,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    user.x_premium = body.x_premium
    await db.commit()
    return {"status": "ok"}


# ── Saved composer presets (per-tenant convenience) ─────────────────────────

@router.get("/presets", response_model=PresetsResponse)
async def get_presets(
    user: Annotated[UserModel, Depends(get_current_user)],
) -> PresetsResponse:
    return PresetsResponse(presets=user.post_presets or [])


@router.put("/presets", response_model=PresetsResponse)
async def put_presets(
    body: PresetsUpdate,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PresetsResponse:
    # Whole-list replace (like /slide-style): the client sends the full array
    # after an add/rename/delete. Pydantic already validated and de-duped.
    user.post_presets = [p.model_dump(mode="json") for p in body.presets] or None
    await db.commit()
    return PresetsResponse(presets=body.presets)


# ── Brand logo (per-tenant, drawn in the corner of every slide) ─────────────

_LOGO_URL = "/api/settings/logo/image"


@router.get("/logo", response_model=LogoSettingsResponse)
async def get_logo(
    user: Annotated[UserModel, Depends(get_current_user)],
) -> LogoSettingsResponse:
    has = bool(logo_store.path_for(str(user.id)))
    return LogoSettingsResponse(set=has, url=_LOGO_URL if has else None)


@router.post("/logo", response_model=LogoSettingsResponse)
async def put_logo(
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
) -> LogoSettingsResponse:
    if file.content_type not in _LOGO_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type {file.content_type!r}. Allowed: png, webp, jpeg.",
        )
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > _LOGO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 20 MB)")

    path = logo_store.save(str(user.id), data, file.content_type)
    user.logo_path = str(path)
    await db.commit()
    return LogoSettingsResponse(set=True, url=_LOGO_URL)


@router.delete("/logo", response_model=LogoSettingsResponse)
async def delete_logo(
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LogoSettingsResponse:
    logo_store.delete(str(user.id))
    user.logo_path = None
    await db.commit()
    return LogoSettingsResponse(set=False, url=None)


@router.get("/logo/image")
async def get_logo_image(
    user: Annotated[UserModel, Depends(get_current_user)],
) -> FileResponse:
    # Behind auth, unlike slide images: a logo is an account setting, not part of
    # an already-published post (where it's baked into the slide JPEG anyway).
    path = logo_store.path_for(str(user.id))
    if not path:
        raise HTTPException(status_code=404, detail="No logo set")
    return FileResponse(path)


# ── Reel background music (per-tenant, ducked under the voiceover) ───────────
# The tenant uploads a track THEY have the rights to — we ship no music library
# (nothing we could legally license for every tenant). No DB column: the file
# store keyed by user id is the source of truth.

_MUSIC_MAX_BYTES = 20 * 1024 * 1024


@router.get("/music", response_model=MusicSettingsResponse)
async def get_music(
    user: Annotated[UserModel, Depends(get_current_user)],
) -> MusicSettingsResponse:
    return MusicSettingsResponse(set=bool(music_store.path_for(str(user.id))))


@router.post("/music", response_model=MusicSettingsResponse)
async def put_music(
    user: Annotated[UserModel, Depends(get_current_user)],
    file: UploadFile = File(...),
) -> MusicSettingsResponse:
    if file.content_type not in music_store.EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type {file.content_type!r}. Allowed: mp3, m4a, wav.",
        )
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > _MUSIC_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 20 MB)")
    music_store.save(str(user.id), data, file.content_type)
    return MusicSettingsResponse(set=True)


@router.delete("/music", response_model=MusicSettingsResponse)
async def delete_music(
    user: Annotated[UserModel, Depends(get_current_user)],
) -> MusicSettingsResponse:
    music_store.delete(str(user.id))
    return MusicSettingsResponse(set=False)


# ── AI provider + model (each tenant picks, and pays for, their own) ─────────

@router.get("/ai", response_model=AISettingsResponse)
async def get_ai_settings(
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AISettingsResponse:
    creds = await db.get(UserCredentialsModel, user.id)
    keys: dict[str, dict] = {}
    for provider, meta in PROVIDERS.items():
        column = _CRED_FIELDS.get(meta["key_field"])
        raw = decrypt(getattr(creds, column) or "") if (creds and column) else ""
        keys[provider] = {"set": bool(raw), "masked": _mask(raw)}
    return AISettingsResponse(
        text_provider=user.text_provider or "",
        text_model=user.text_model or "",
        image_provider=user.image_provider or "",
        image_model=user.image_model or "",
        keys=keys,
    )


@router.put("/ai")
async def put_ai_settings(
    body: AISettingsUpdate,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from fastapi import HTTPException

    if body.text_provider is not None:
        if body.text_provider and not is_valid_provider(body.text_provider, TEXT):
            raise HTTPException(status_code=422, detail="Unknown text provider")
        user.text_provider = body.text_provider or None
    if body.image_provider is not None:
        if body.image_provider and not is_valid_provider(body.image_provider, IMAGE):
            raise HTTPException(
                status_code=422,
                detail="Unknown image provider (note: Anthropic cannot generate images)")
        user.image_provider = body.image_provider or None
    # Any model id is accepted — the catalogue is a shortlist, not a whitelist, so a
    # newly released model is usable without waiting for a release.
    if body.text_model is not None:
        user.text_model = body.text_model.strip() or None
    if body.image_model is not None:
        user.image_model = body.image_model.strip() or None
    await db.commit()
    return {"status": "ok"}


@router.post("/ai/test", response_model=AITestResponse)
async def test_ai_settings(
    body: AITestRequest,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AITestResponse:
    """Prove the provider + model + key actually work, before the user relies on
    them. This is what catches a retired model id up front instead of mid-generation."""
    from services.ai.base import AIError
    from services.ai.factory import make_image_provider, make_text_provider
    from services.user_settings import build_settings_for_user, resolve_ai_choice

    kind = "image" if body.kind == "image" else "text"
    settings = await build_settings_for_user(db, user)
    provider, model, api_key = resolve_ai_choice(user, settings, kind)
    if not provider or not model:
        return AITestResponse(ok=False, message=f"Choose a {kind} provider and model first.")
    if not api_key:
        label = PROVIDERS[provider]["label"] if provider in PROVIDERS else provider
        return AITestResponse(ok=False, message=f"Add your {label} API key first.")

    client = None
    try:
        if kind == "image":
            client = make_image_provider(provider, api_key, ssl_verify=settings.ssl_verify)
            await client.generate_image(model=model, prompt="A single small grey square.")
        else:
            client = make_text_provider(provider, api_key, ssl_verify=settings.ssl_verify)
            await client.generate_text(model=model, system_prompt="Reply with OK.",
                                       user_prompt="Say OK.", max_tokens=16)
        return AITestResponse(ok=True, message=f"{model} works.")
    except AIError as exc:
        return AITestResponse(ok=False, message=str(exc)[:400])
    except Exception as exc:                      # never leak a stack trace to the UI
        return AITestResponse(ok=False, message=f"Test failed: {type(exc).__name__}")
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


@router.post("/publish/test", response_model=PublishTestResponse)
async def test_publish_connection(
    body: PublishTestRequest,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PublishTestResponse:
    """Read-only preflight: prove this network's saved keys actually work, WITHOUT
    publishing anything. Confirms the account before the user risks a real post —
    the same idea as POST /ai/test for models."""
    from services.instagram import InstagramError, InstagramPublisher
    from services.publishing.base import PublisherError
    from services.publishing.x import XPublisher
    from services.user_settings import build_settings_for_user

    platform = (body.platform or "").strip().lower()
    if platform not in ("x", "instagram"):
        return PublishTestResponse(ok=False, message="Unknown platform.")

    settings = await build_settings_for_user(db, user)
    client = None
    try:
        if platform == "x":
            if not all((settings.x_api_key, settings.x_api_secret,
                        settings.x_access_token, settings.x_access_token_secret)):
                return PublishTestResponse(
                    ok=False, message="Add all four X API keys first.")
            client = XPublisher(settings.x_api_key, settings.x_api_secret,
                                settings.x_access_token, settings.x_access_token_secret)
            info = await client.verify_credentials()
            handle = info.get("username")
            return PublishTestResponse(
                ok=True, handle=handle,
                message=f"Connected as @{handle}." if handle else "Connected.")
        # instagram
        if not (settings.instagram_access_token and settings.instagram_user_id):
            return PublishTestResponse(
                ok=False, message="Add your Instagram access token and user id first.")
        client = InstagramPublisher(settings.instagram_access_token,
                                    settings.instagram_user_id)
        info = await client.verify_credentials()
        handle = info.get("username")
        tail = "" if settings.imgbb_api_key else " (add an imgbb key to publish images)"
        return PublishTestResponse(
            ok=True, handle=handle,
            message=(f"Connected as @{handle}." if handle else "Connected.") + tail)
    except (PublisherError, InstagramError) as exc:
        return PublishTestResponse(ok=False, message=str(exc)[:400])
    except Exception as exc:                      # never leak a stack trace to the UI
        return PublishTestResponse(ok=False, message=f"Test failed: {type(exc).__name__}")
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


# ── Connection health + Instagram token renewal ─────────────────────────────

@router.get("/connections")
async def connection_status(
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Last known health per network, plus how close the Instagram token is to
    expiring. `expires_estimated` says whether that date is a guess — the UI must
    not present an estimate as a fact."""
    creds = await db.get(UserCredentialsModel, user.id)
    health = dict((creds.connection_health if creds else None) or {})
    for record in health.values():
        raw = record.get("expires_at")
        record["days_left"] = days_left(_parse_iso(raw)) if raw else None
    return {"connections": health}


def _parse_iso(value: str):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


@router.post("/instagram/refresh-token", response_model=PublishTestResponse)
async def refresh_instagram_token(
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PublishTestResponse:
    """Extend the Instagram token by 60 days and store the new one.

    Explicitly user-triggered: refreshing ROTATES the token, so the background
    health sweep must never do it — a half-finished rotation would leave the
    account with a token nobody has. Doing it here also gives us the real expiry
    date instead of the 60-days-from-saving estimate.
    """
    from services.instagram import InstagramError, InstagramPublisher
    from services.user_settings import build_settings_for_user

    creds = await db.get(UserCredentialsModel, user.id)
    settings = await build_settings_for_user(db, user)
    if creds is None or not settings.instagram_access_token:
        return PublishTestResponse(ok=False, message="Add your Instagram access token first.")

    client = InstagramPublisher(settings.instagram_access_token,
                                settings.instagram_user_id or "")
    try:
        result = await client.refresh_token()
    except InstagramError as exc:
        return PublishTestResponse(ok=False, message=str(exc)[:400])
    except Exception as exc:
        return PublishTestResponse(ok=False, message=f"Refresh failed: {type(exc).__name__}")
    finally:
        try:
            await client.close()
        except Exception:
            pass

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=result["expires_in"])
    creds.instagram_access_token_enc = encrypt(result["access_token"])
    _set_health(creds, "instagram", expires_at=expires_at,
                expires_estimated=False, ok=True, error="")
    await db.commit()
    return PublishTestResponse(
        ok=True, message=f"Token renewed — valid until {expires_at:%Y-%m-%d}.")
