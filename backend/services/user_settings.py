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
    """Platform Settings with the cloud user's own decrypted API keys, and with
    every credential they have NOT stored blanked out. Local user (or unknown) →
    platform .env as-is.

    Blanking is the point, and it is why this is not a plain overlay. The
    platform value used to survive wherever the user had none, and nothing
    downstream could tell the difference: `resolve_ai_choice` reads the merged
    object and sees one string. That made the app's own key reachable by any
    account in three moves — register, name a provider and model (neither needs
    a key), generate — and `record_usage` filed our spend under their name. The
    browser's `guardGenerateKeys` asks about the account's OWN credentials and
    so refused correctly, but a wall that only exists in the SPA is not a wall.

    Downstream needs no changes: `_ai_provider_or_none` already turns an empty
    key into None, and every route already turns None into "choose a provider
    and model in Account". An empty credential means "you have not configured
    this", which is exactly what it now is.

    The desktop owner keeps the whole .env — there the platform and the user are
    the same person, and the offline app is configured entirely that way.
    """
    base = get_settings()
    if user is None or user.is_local:
        return base
    creds = await db.get(UserCredentialsModel, user.id)
    overrides: dict[str, str] = {}
    for field, column in _CRED_FIELDS.items():
        # None (tamper) or "" (unset) both mean "not this user's key", and so
        # does having no credentials row at all.
        decrypted = decrypt(getattr(creds, column) or "") if creds else ""
        overrides[field] = decrypted or ""
    return base.model_copy(update=overrides)


async def settings_for_post_owner(db: AsyncSession, post) -> Settings:
    """Effective Settings from the post owner's stored keys, for publishing outside
    a request (publisher_flow / scheduler). Owner is the local user or unknown →
    platform .env."""
    user = await db.get(UserModel, post.user_id) if post.user_id else None
    return await build_settings_for_user(db, user)


def resolve_user_brand_voice(brand) -> str:
    """The brand-voice text to generate this brand's content in (defaults to the
    balanced preset). Since UX phase 2 the argument is a ManagedAccount profile,
    not a User — brand identity is not part of the _CRED_FIELDS/Settings overlay
    either way."""
    from services.brand_voice import resolve_brand_voice
    if brand is None:
        return resolve_brand_voice(None)
    return resolve_brand_voice(brand.brand_voice_preset, brand.brand_voice_custom)


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


def apply_brand_slide_style(cfg, brand, *, is_local: bool = False):
    """Overlay a brand's slide style — colours and logo — onto a loaded
    BrandConfig. Unset colours keep the platform default preset. Mutates and
    returns `cfg`.

    `is_local` is a property of the deployment, not of a brand, so it arrives as
    an argument rather than being read off the object. It used to be duck-typed,
    which returned False for a profile — harmless while only a User was ever
    passed, and wrong the moment the desktop owner got a profile of its own (UX
    phase 2). The default is the strict cloud posture, so a caller who forgets
    the flag can only ever be too careful with someone's logo.
    """
    if brand is None:
        return cfg
    if getattr(brand, "slide_accent_color", None):
        cfg.niche_box_color = brand.slide_accent_color
    if getattr(brand, "slide_text_box_color", None):
        cfg.desc_box_color = brand.slide_text_box_color
    if not is_local:
        # A cloud tenant gets exactly their own logo, or none — clearing the path
        # so the platform's default logo never leaks onto someone else's brand.
        # The desktop/local user keeps whatever the loaded config carries.
        from pathlib import Path
        logo = getattr(brand, "logo_path", None)
        cfg.logo_path = Path(logo) if logo else None
    return cfg


def resolve_user_profile(brand) -> dict[str, Optional[str]]:
    """A brand's niche / audience / name, used to default the composer and steer
    generation. Since UX phase 2 the argument is a ManagedAccount profile, never
    a User — the columns of the same name on User are a rollback snapshot that
    nothing reads."""
    if brand is None:
        return {"niche": None, "target_audience": None, "brand_name": None}
    return {
        "niche": brand.niche,
        "target_audience": brand.target_audience,
        "brand_name": brand.brand_name,
    }
