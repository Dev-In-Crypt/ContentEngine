"""Per-user effective Settings: platform config overlaid with a user's own,
decrypted API keys. Lives in the services layer (not api.deps) so both the FastAPI
DI (get_effective_settings) and the request-less publisher_flow/scheduler can use
it without importing the api layer.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from models.database import User as UserModel, UserCredentials as UserCredentialsModel
from services.secrets import decrypt

# Every Settings field a user may override with their own key, mapped to the
# UserCredentials column that stores it (encrypted).
_CRED_FIELDS: dict[str, str] = {
    "openrouter_api_key": "openrouter_api_key_enc",
    "openai_api_key": "openai_api_key_enc",
    "anthropic_api_key": "anthropic_api_key_enc",
    "google_api_key": "google_api_key_enc",
    "instagram_access_token": "instagram_access_token_enc",
    "instagram_user_id": "instagram_user_id_enc",
    "imgbb_api_key": "imgbb_api_key_enc",
    "x_api_key": "x_api_key_enc",
    "x_api_secret": "x_api_secret_enc",
    "x_access_token": "x_access_token_enc",
    "x_access_token_secret": "x_access_token_secret_enc",
    "unsplash_access_key": "unsplash_access_key_enc",
    "pexels_api_key": "pexels_api_key_enc",
    "elevenlabs_api_key": "elevenlabs_api_key_enc",
    "kling_api_key": "kling_api_key_enc",
}


async def build_settings_for_user(db: AsyncSession, user: Optional[UserModel]) -> Settings:
    """Platform Settings overlaid with the user's own decrypted API keys. Local
    user (or unknown) → platform .env as-is."""
    base = get_settings()
    if user is None or user.is_local:
        return base
    creds = await db.get(UserCredentialsModel, user.id)
    if creds is None:
        return base
    overrides: dict[str, str] = {}
    for field, column in _CRED_FIELDS.items():
        decrypted = decrypt(getattr(creds, column) or "")
        if decrypted:   # None (tamper) or "" (unset) → keep platform default
            overrides[field] = decrypted
    return base.model_copy(update=overrides) if overrides else base


async def settings_for_post_owner(db: AsyncSession, post) -> Settings:
    """Effective Settings from the post owner's stored keys, for publishing outside
    a request (publisher_flow / scheduler). Owner is the local user or unknown →
    platform .env."""
    user = await db.get(UserModel, post.user_id) if post.user_id else None
    return await build_settings_for_user(db, user)


def resolve_user_brand_voice(user: Optional[UserModel]) -> str:
    """The brand-voice text to generate a user's content in. Reads the user's saved
    preset/custom (defaults to the balanced preset). Lives on User, so it's read
    directly — not part of the _CRED_FIELDS/Settings overlay."""
    from services.brand_voice import resolve_brand_voice
    if user is None:
        return resolve_brand_voice(None)
    return resolve_brand_voice(user.brand_voice_preset, user.brand_voice_custom)


def resolve_ai_choice(user: Optional[UserModel], settings: Settings,
                      kind: str = "text") -> tuple[Optional[str], Optional[str], str]:
    """Which (provider, model, api_key) this user generates `kind` with.

    Local/desktop users keep using the .env values so the offline app is unaffected.
    Cloud users must choose explicitly — an unset provider or model returns None so
    the caller can raise a clear "configure it in Account" error rather than
    silently spending on a model the user never picked.
    """
    from services.ai.catalog import key_field_for

    if user is None or getattr(user, "is_local", False):
        provider = (settings.default_text_provider if kind == "text"
                    else settings.default_image_provider)
        model = (settings.default_text_model if kind == "text"
                 else settings.default_image_model)
    else:
        provider = user.text_provider if kind == "text" else user.image_provider
        model = user.text_model if kind == "text" else user.image_model
    if not provider or not model:
        return provider or None, model or None, ""
    field = key_field_for(provider)
    api_key = getattr(settings, field, "") if field else ""
    return provider, model, api_key or ""


def apply_user_slide_style(cfg, user: Optional[UserModel]):
    """Overlay the tenant's own slide style — colours and logo — onto a loaded
    BrandConfig. Unset colours keep the platform default preset. Mutates and
    returns `cfg`."""
    if user is None:
        return cfg
    if getattr(user, "slide_accent_color", None):
        cfg.niche_box_color = user.slide_accent_color
    if getattr(user, "slide_text_box_color", None):
        cfg.desc_box_color = user.slide_text_box_color
    if not getattr(user, "is_local", False):
        # A cloud tenant gets exactly their own logo, or none — clearing the path
        # so the platform's default logo never leaks onto someone else's brand.
        # The desktop/local user keeps whatever the loaded config carries.
        from pathlib import Path
        logo = getattr(user, "logo_path", None)
        cfg.logo_path = Path(logo) if logo else None
    return cfg


def resolve_user_profile(user: Optional[UserModel]) -> dict[str, Optional[str]]:
    """The user's saved brand profile (niche / audience / brand name), used to default
    the composer and steer generation into their niche. Read directly off User; all
    keys None when unset or no user."""
    if user is None:
        return {"niche": None, "target_audience": None, "brand_name": None}
    return {
        "niche": user.niche,
        "target_audience": user.target_audience,
        "brand_name": user.brand_name,
    }
