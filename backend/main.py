import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import create_engine, inspect, select, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from config import get_settings
from models.database import BrandConfig as BrandConfigModel, User as UserModel
from models.schemas import NICHE_BOX_PALETTE
from services.http_utils import setup_logging, setup_tls
from api.deps import LOCAL_USER_EMAIL
from api.ratelimit import limiter
from api.routes import (
    accounts, brand, business, demo, media, posts, models, publish_jobs, stock, admin,
    auth, settings as settings_routes,
)

STATIC_DIR = Path(__file__).parent / "static"
UPLOADS_DIR = Path(__file__).parent / "uploads"

settings = get_settings()
log = logging.getLogger(__name__)

_HERE = Path(__file__).parent


def _sync_db_url(url: str) -> str:
    """Sync-driver form for Alembic (which is synchronous)."""
    return (url.replace("+aiosqlite", "").replace("+asyncpg", "")
            .replace("postgres://", "postgresql://"))


def _run_migrations(database_url: str) -> None:
    """Bring the schema to head via Alembic. Auto-adopts a pre-existing DB (tables
    but no alembic_version) by stamping head first, so the very first deploy onto
    the already-populated prod DB doesn't try to recreate existing tables. A fresh
    DB just runs the baseline. Synchronous — call via asyncio.to_thread."""
    sync_url = _sync_db_url(database_url)
    cfg = Config(str(_HERE / "alembic.ini"))
    cfg.set_main_option("script_location", str(_HERE / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sync_url)

    engine = create_engine(sync_url)
    try:
        insp = inspect(engine)
        if not insp.has_table("alembic_version") and insp.has_table("users"):
            command.stamp(cfg, "head")   # existing schema → adopt it, don't rebuild
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")


def _async_db_url(url: str) -> str:
    """Normalize a database URL to an async driver for create_async_engine.

    Render/Heroku hand out `postgres://` or `postgresql://` (the psycopg2/sync
    driver), which create_async_engine rejects. Map both to asyncpg. sqlite and
    already-async URLs are left untouched.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


async def _apply_admin_emails(sessionmaker, emails_csv: str) -> None:
    """Grant is_admin to the configured emails (cloud has no local owner). Idempotent."""
    emails = [e.strip().lower() for e in emails_csv.split(",") if e.strip()]
    if not emails:
        return
    async with sessionmaker() as session:
        await session.execute(
            update(UserModel).where(UserModel.email.in_(emails))
            .values(is_admin=True, email_verified=True)
        )
        await session.commit()


async def _seed_brand_preset(sessionmaker) -> None:
    """Insert the neutral 'Default' brand preset if it does not exist yet."""
    async with sessionmaker() as session:
        result = await session.execute(
            select(BrandConfigModel).where(BrandConfigModel.name == "Default")
        )
        if result.scalar_one_or_none():
            return
        session.add(BrandConfigModel(
            id=str(uuid.uuid4()),
            name="Default",
            is_default=True,
            primary_color="#0076cb",
            secondary_color="#1A4D8A",
            accent_color="#ff751f",
            logo_position="top_right",
            logo_scale=0.15,
            padding=40,
            template_style="branded_card",
            niche_box_color="#ff751f",
            niche_box_palette=NICHE_BOX_PALETTE,
            description_box_alpha=0.79,
            show_logo=True,
        ))
        await session.commit()


async def _seed_local_user(sessionmaker) -> None:
    """Local (desktop) mode owns everything under one implicit user, so the
    desktop needs no login. Insert it once; get_current_user returns it."""
    async with sessionmaker() as session:
        result = await session.execute(
            select(UserModel).where(UserModel.is_local == True)  # noqa: E712
        )
        if result.scalar_one_or_none():
            return
        session.add(UserModel(email=LOCAL_USER_EMAIL, is_local=True, is_active=True,
                              email_verified=True))
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)
    # Optional error monitoring.
    if settings.sentry_dsn:
        try:
            import sentry_sdk
            sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1)
            log.info("Sentry initialized")
        except Exception as exc:  # pragma: no cover
            log.warning("Sentry init failed: %s", exc)
    # Must precede every outbound connection, the database included.
    setup_tls()

    # In cloud mode the app is publicly reachable and multi-tenant: SECRET_KEY
    # signs every session JWT AND derives the key that encrypts users' stored API
    # keys. With the default value both are trivially forgeable/decryptable, so a
    # real one is mandatory. Refuse to start otherwise.
    if settings.app_mode == "cloud" and settings.secret_key == "change-me-in-production":
        raise RuntimeError(
            "SECRET_KEY must be set in cloud mode: it signs auth tokens and "
            "encrypts stored user credentials. Set a strong, stable SECRET_KEY "
            "in the environment (rotating it later logs everyone out and orphans "
            "all stored keys)."
        )

    # Schema: Alembic to head (auto-stamps a pre-existing DB). Runs in a thread
    # since Alembic is synchronous.
    await asyncio.to_thread(_run_migrations, settings.database_url)
    engine = create_async_engine(_async_db_url(settings.database_url), echo=False)
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    await _seed_brand_preset(app.state.sessionmaker)
    if settings.app_mode != "cloud":
        await _seed_local_user(app.state.sessionmaker)
    await _apply_admin_emails(app.state.sessionmaker, settings.admin_emails)
    UPLOADS_DIR.mkdir(exist_ok=True)

    # Scheduled publishing (APScheduler). In local mode this only fires while
    # the app is open; in cloud mode it runs 24/7. Failures here must not block
    # the app from starting.
    try:
        from services.scheduler import init_scheduler, reconcile_scheduled
        # Business source polling runs cloud-only (offline desktops have no sources).
        init_scheduler(settings.database_url, app.state.sessionmaker,
                       poll_sources=(settings.app_mode == "cloud"))
        # Recover posts left 'scheduled' with no live job (server was down at fire time).
        await reconcile_scheduled(app.state.sessionmaker)
    except Exception as exc:  # pragma: no cover
        import logging
        logging.getLogger(__name__).warning("Scheduler init failed: %s", exc)

    yield

    try:
        from services.scheduler import shutdown_scheduler
        shutdown_scheduler()
    except Exception:
        pass
    await engine.dispose()


def _docs_urls(app_mode: str) -> dict:
    """Hide Swagger/ReDoc/OpenAPI on a public deployment; keep them for local dev."""
    if app_mode == "cloud":
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}


app = FastAPI(
    title="Instagram Content Engine",
    description="AI-powered Instagram post generation and publishing system",
    version="1.0.0",
    lifespan=lifespan,
    **_docs_urls(settings.app_mode),
)

# Rate limiting (per-IP). 429 on exceed via slowapi's handler.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(settings_routes.router)
app.include_router(posts.router)
app.include_router(models.router)
app.include_router(stock.router)
app.include_router(admin.router)
app.include_router(demo.router)
app.include_router(business.router)
app.include_router(accounts.router)
app.include_router(media.router)
app.include_router(publish_jobs.router)
app.include_router(brand.router)

# Serve built frontend assets (images, fonts, etc.) at /static/*
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# Standalone legal pages (linked from the landing footer + the sign-up screen).
# Served explicitly so they don't fall through to the SPA.
@app.get("/terms", include_in_schema=False)
async def terms_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "terms.html")


@app.get("/privacy", include_in_schema=False)
async def privacy_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "privacy.html")


# Catch-all: serve the single-page app for any non-API route. An unknown /api/*
# path must 404 rather than fall through to the SPA with a misleading 200.
@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str) -> FileResponse:
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(STATIC_DIR / "index.html")
