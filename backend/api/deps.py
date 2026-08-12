from functools import lru_cache
from pathlib import Path
from typing import Annotated, Optional
from collections.abc import AsyncGenerator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from models.database import (
    BrandConfig as BrandConfigModel,
    Post as PostModel,
    User as UserModel,
)
# The acting user's id for the current async task (set in get_current_user).
# Defined in services.openrouter to avoid a circular import; re-exported here.
from services.openrouter import current_user_id
# Per-user effective Settings live in the services layer; re-exported so
# api/routes/settings.py (imports _CRED_FIELDS) and get_effective_settings work.
from services.user_settings import (  # noqa: F401
    _CRED_FIELDS, build_settings_for_user, resolve_ai_choice,
)
from services.auth import decode_access_token_claims
from services.openrouter import OpenRouterClient
from services.caption_generator import CaptionGenerator
from services.image_router import ImageFetchError, ImageRouter
from services import staging
from services.brand_engine import PillowBrandEngine, BrandConfig
from services.exporter import TemplateExporter
from services.stock import UnsplashClient, PexelsClient, StockClient
from services.content_engine import ContentEngine


# ---- leaf service singletons (created once per process) ----

@lru_cache
def _get_openrouter(api_key: str, referer: str, title: str, ssl_verify: bool = True) -> OpenRouterClient:
    return OpenRouterClient(api_key=api_key, referer=referer, app_title=title,
                            ssl_verify=ssl_verify)


@lru_cache
def _get_stock_client(unsplash_key: str, pexels_key: str, ssl_verify: bool = True) -> StockClient:
    unsplash = UnsplashClient(unsplash_key, ssl_verify=ssl_verify) if unsplash_key else None
    pexels = PexelsClient(pexels_key, ssl_verify=ssl_verify) if pexels_key else None
    return StockClient(unsplash=unsplash, pexels=pexels)


@lru_cache
def _get_brand_engine() -> PillowBrandEngine:
    return PillowBrandEngine(BrandConfig())


def _to_path(value) -> Optional[Path]:
    return Path(value) if value else None


def _row_to_brand_config(row: BrandConfigModel) -> BrandConfig:
    return BrandConfig(
        logo_path=_to_path(row.logo_path),
        primary_color=row.primary_color or BrandConfig.primary_color,
        secondary_color=row.secondary_color or BrandConfig.secondary_color,
        accent_color=row.accent_color or BrandConfig.accent_color,
        heading_font_path=_to_path(row.heading_font_path),
        body_font_path=_to_path(row.body_font_path),
        logo_position=row.logo_position or "bottom_right",
        logo_scale=row.logo_scale if row.logo_scale is not None else 0.15,
        padding=row.padding if row.padding is not None else 40,
        template_style=row.template_style or "branded_card",
        niche_box_palette=row.niche_box_palette or BrandConfig().niche_box_palette,
        niche_box_color=row.niche_box_color or "#ff751f",
        description_box_alpha=row.description_box_alpha if row.description_box_alpha is not None else 0.79,
        show_logo=row.show_logo if row.show_logo is not None else True,
    )


async def load_brand_config(
    db: AsyncSession, brand_config_id: Optional[str] = None
) -> BrandConfig:
    """Load a brand preset from the DB; falls back to the default row, then defaults."""
    stmt = select(BrandConfigModel)
    if brand_config_id:
        stmt = stmt.where(BrandConfigModel.id == brand_config_id)
    else:
        stmt = stmt.where(BrandConfigModel.is_default == True).order_by(BrandConfigModel.created_at)  # noqa: E712
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        return BrandConfig()
    return _row_to_brand_config(row)


# ---- FastAPI dependencies ----

def get_brand_engine() -> PillowBrandEngine:
    return _get_brand_engine()


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    async with request.app.state.sessionmaker() as session:
        yield session


# ---- Authentication (multi-tenant) ----

LOCAL_USER_EMAIL = "local@localhost"


async def _get_or_create_local_user(db: AsyncSession) -> UserModel:
    """The single implicit owner used in local (desktop) mode — no login there."""
    row = (await db.execute(
        select(UserModel).where(UserModel.is_local == True)  # noqa: E712
    )).scalars().first()
    if row:
        return row
    user = UserModel(email=LOCAL_USER_EMAIL, is_local=True, is_active=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def get_current_user(
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: str = Header(default=""),
) -> UserModel:
    """Resolve the acting user. Local mode → the seeded local owner (no token).
    Cloud mode → decode the Bearer JWT; 401 if missing/invalid/inactive."""
    if settings.app_mode != "cloud":
        user = await _get_or_create_local_user(db)
        await _ensure_profile(db, user)
        current_user_id.set(user.id)
        return user

    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    claims = decode_access_token_claims(token)
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = await db.get(UserModel, claims["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Revocation: a token minted before the user's version was bumped (password
    # reset / logout-all) no longer authenticates.
    if int(claims.get("tv", 0)) != int(user.token_version or 0):
        raise HTTPException(status_code=401, detail="Session expired")
    await _ensure_profile(db, user)
    current_user_id.set(user.id)
    return user


async def _ensure_profile(db: AsyncSession, user: UserModel) -> None:
    """Give the user a brand profile if they somehow don't have one.

    Steady state is one attribute read and no query. It fires for a user the
    data migration never saw — the deploy window on a rolling restart, a restore
    from a pg_dump predating the migration, the lazily created local desktop
    owner — and for one whose active brand was just deleted.

    Cheap insurance for something expensive: since UX phase 2 the post list
    filters on the active profile, so a user without one sees an empty app.
    """
    if user.active_account_id:
        return
    from services.managed_account import ensure_primary_profile
    await ensure_primary_profile(db, user)


async def get_effective_settings(
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Settings:
    return await build_settings_for_user(db, user)


def require_token(user: Annotated[UserModel, Depends(get_current_user)]) -> None:
    """Thin auth gate kept under its historical name so the 40+ existing
    `dependencies=[Depends(require_token)]` sites need no churn. Authentication now
    means 'a resolvable user' — always true in local mode, JWT-gated in cloud."""
    return None


def require_admin(user: Annotated[UserModel, Depends(get_current_user)]) -> None:
    """Gate whole-database operations (backup/restore). The local desktop owner is
    admin; in cloud only a user flagged is_admin may pull everyone's data."""
    if not (user.is_local or user.is_admin):
        raise HTTPException(status_code=403, detail="Admin only")
    return None


def require_local(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """Gate endpoints that touch the server's own filesystem/desktop (write to
    ~/Downloads, spawn a file explorer). They're meaningless — and process-spawning
    — on a shared cloud host, so hide them there."""
    if settings.app_mode == "cloud":
        raise HTTPException(status_code=404, detail="Not found")
    return None


def require_verified(
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> None:
    """Gate publishing on a verified email — only when enforcement is enabled
    (require_verified_email) and the user is a real cloud account. Off by default
    until a real sending domain is configured; the desktop/local user is exempt."""
    if settings.require_verified_email and not user.is_local and not user.email_verified:
        raise HTTPException(status_code=403, detail="Please verify your email before publishing")
    return None


def require_business(user: Annotated[UserModel, Depends(get_current_user)]) -> None:
    """Gate Business-only endpoints (sources → leads → approval, added in later
    phases) on account_type == 'business'. The local desktop owner is exempt like
    the other gates; a cloud 'creator' account gets 403 — it never sees these."""
    if not user.is_local and (user.account_type or "creator") != "business":
        raise HTTPException(status_code=403, detail="Business account required")
    return None


def require_agency(user: Annotated[UserModel, Depends(get_current_user)]) -> None:
    """Gate the Team screen on account_type == 'agency'.

    Shaped exactly like require_business, including the desktop exemption: the
    local owner is the only account on their machine and gates there protect
    nobody. A 'creator' or 'business' cloud account gets 403 — inviting people
    to a team is the one thing the agency plan is for.
    """
    if not user.is_local and (user.account_type or "creator") != "agency":
        raise HTTPException(status_code=403, detail="Agency account required")
    return None


async def owned_post(db: AsyncSession, post_id: str, user: UserModel, *, options=()) -> PostModel:
    """Fetch a post the user is allowed to touch, else 404 (not 403 — don't reveal
    that another tenant's post exists). The local desktop user owns everything, so
    the ownership filter only applies to real cloud accounts."""
    stmt = select(PostModel).where(PostModel.id == post_id)
    if not user.is_local:
        stmt = stmt.where(PostModel.user_id == user.id)
    if options:
        stmt = stmt.options(*options)
    post = (await db.execute(stmt)).scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


def get_openrouter(
    settings: Annotated[Settings, Depends(get_effective_settings)],
) -> OpenRouterClient:
    return _get_openrouter(
        settings.openrouter_api_key,
        settings.openrouter_referer,
        settings.openrouter_app_title,
        settings.ssl_verify,
    )


def get_stock(settings: Annotated[Settings, Depends(get_effective_settings)]) -> StockClient:
    return _get_stock_client(
        settings.unsplash_access_key, settings.pexels_api_key, settings.ssl_verify,
    )


@lru_cache
def _get_ai_provider(provider: str, api_key: str, ssl_verify: bool,
                     referer: str, app_title: str):
    """Cached per (provider, key) — the provider MUST be part of the key, otherwise
    one tenant would be handed a client built for another's vendor."""
    from services.ai.factory import make_text_provider
    return make_text_provider(provider, api_key, ssl_verify=ssl_verify,
                              referer=referer, app_title=app_title)


def _ai_provider_or_none(provider: Optional[str], api_key: str, settings: Settings):
    """Build a provider, or None when the tenant has not configured one yet.
    The route turns None into a clear "choose a model in Account" error."""
    from services.ai.base import AIError
    if not provider or not api_key:
        return None
    try:
        return _get_ai_provider(provider, api_key, settings.ssl_verify,
                                settings.openrouter_referer, settings.openrouter_app_title)
    except AIError:
        return None


def get_text_provider(
    user: Annotated[UserModel, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_effective_settings)],
):
    provider, _model, api_key = resolve_ai_choice(user, settings, "text")
    return _ai_provider_or_none(provider, api_key, settings)


def get_image_provider(
    user: Annotated[UserModel, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_effective_settings)],
):
    provider, _model, api_key = resolve_ai_choice(user, settings, "image")
    return _ai_provider_or_none(provider, api_key, settings)


def get_demo_text_provider(settings: Annotated[Settings, Depends(get_settings)]):
    """Text provider for the public no-auth demo — built from the PLATFORM's own
    credentials, because an anonymous visitor has none. None when the platform
    has no key, so the route can 503 cleanly instead of failing partway.

    `resolve_ai_choice(None, …)` is what "the platform's own" means everywhere
    else: passing None for the user is exactly the branch that reads
    `default_text_provider` and finds the key through `key_field_for()`, and it
    is already how the free-generation path picks its credentials.

    It used to name OpenRouter here and read `openrouter_api_key` directly. That
    made the vendor a property of this one function rather than of the
    deployment: an owner who pointed the platform at another vendor got a
    working free tier and a landing page that still called OpenRouter, with a key
    field nothing else was filling — a 503 on the first screen of the product,
    on a deployment that looked correctly configured everywhere you would think
    to look.

    Base `get_settings` on purpose, never the per-user resolver: this must not be
    reachable with somebody else's key.
    """
    provider, _model, api_key = resolve_ai_choice(None, settings, "text")
    return _ai_provider_or_none(provider, api_key, settings)


def assemble_content_engine(text_provider, image_provider, stock: StockClient,
                            brand_engine: PillowBrandEngine,
                            user: Optional[UserModel]) -> ContentEngine:
    """Put an engine together from parts that are already resolved.

    Separate from the dependency below because the free-generation path (UX
    phase 6.2) needs an engine built from the APPLICATION's credentials rather
    than the caller's, and a FastAPI dependency can only ever hand back the
    caller's. Same assembly either way; only whose keys went into the parts
    differs.
    """
    # Text and images are independent: a tenant may use OpenRouter for copy and
    # Google for images, so these are two different objects.
    caption_gen = CaptionGenerator(text_provider)
    # Bound to THIS user: an upload id from another tenant must not resolve.
    # Their own photos stay theirs no matter whose key writes the caption.
    #
    # `user=None` is the landing (UX phase 7.1), where there is no account and
    # therefore no staging area. Refusing by name beats passing None through to
    # `staging.read`, which would build a path out of the string "None" and go
    # looking for somebody else's directory.
    def read_upload(upload_id: str) -> bytes:
        if user is None:
            raise ImageFetchError("Uploads need an account.")
        return staging.read(str(user.id), upload_id)

    image_router = ImageRouter(image_provider=image_provider, stock_client=stock,
                               upload_reader=read_upload)
    exporter = TemplateExporter()
    return ContentEngine(caption_gen, image_router, brand_engine, exporter)


def build_content_engine(settings: Settings, user: Optional[UserModel], *,
                         actor: Optional[UserModel],
                         brand_engine: Optional[PillowBrandEngine] = None) -> ContentEngine:
    """An engine that generates on `settings`, choosing models as `actor` would.

    `actor=None` means the platform's own defaults — `resolve_ai_choice` already
    reads it that way, which is why the free path needs no second resolver.
    `user` is still the caller: their uploads, their brand, our bill.
    """
    text_provider, text_key = _resolve_provider(actor, settings, "text")
    image_provider, image_key = _resolve_provider(actor, settings, "image")
    stock = _get_stock_client(settings.unsplash_access_key, settings.pexels_api_key,
                              settings.ssl_verify)
    return assemble_content_engine(
        _ai_provider_or_none(text_provider, text_key, settings),
        _ai_provider_or_none(image_provider, image_key, settings),
        stock, brand_engine or _get_brand_engine(), user)


def _resolve_provider(actor: Optional[UserModel], settings: Settings,
                      kind: str) -> tuple[Optional[str], str]:
    provider, _model, api_key = resolve_ai_choice(actor, settings, kind)
    return provider, api_key


def get_content_engine(
    text_provider: Annotated[object, Depends(get_text_provider)],
    image_provider: Annotated[object, Depends(get_image_provider)],
    stock: Annotated[StockClient, Depends(get_stock)],
    brand_engine: Annotated[PillowBrandEngine, Depends(get_brand_engine)],
    user: Annotated[UserModel, Depends(get_current_user)],
) -> ContentEngine:
    return assemble_content_engine(text_provider, image_provider, stock,
                                   brand_engine, user)
