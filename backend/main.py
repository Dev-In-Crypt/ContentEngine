import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import create_engine, inspect, select, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from starlette.datastructures import MutableHeaders

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from config import Settings, get_settings
from models.database import BrandConfig as BrandConfigModel, User as UserModel
from models.schemas import NICHE_BOX_PALETTE
from services.http_utils import setup_logging, setup_tls
from api.deps import LOCAL_USER_EMAIL
from api.ratelimit import limiter
from api.routes import (
    accounts, brand, insights, business, demo, media, onboarding, posts, models, publish_jobs,
    stock, team, admin,
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
        user = UserModel(email=LOCAL_USER_EMAIL, is_local=True, is_active=True,
                         email_verified=True)
        session.add(user)
        await session.commit()
        # The desktop owner gets a brand profile like everyone else (UX phase 2).
        from services.managed_account import ensure_primary_profile
        await ensure_primary_profile(session, user)


def sentry_options(settings: Settings) -> Optional[dict]:
    """What error monitoring is told, or None when it stays off.

    Split out of the lifespan so it can be asserted: everything here is either
    invisible until the day something breaks, or a decision about what leaves
    this machine, and neither survives being checked by reading the line.

    `environment` and `release` are what make an error answerable — which
    deployment, and which build. A missing release is left absent rather than
    guessed: sending a stale one points whoever is on call at a diff that never
    shipped, which is worse than sending nothing.

    `send_default_pii=False` is the SDK default, and is stated anyway. This app
    holds unpublished captions, brand profiles and encrypted third-party keys;
    "the vendor's default happens to be safe today" is not the same promise as
    "we decided this", and only one of the two has a test in front of it.
    """
    if not settings.sentry_dsn:
        return None
    options: dict = {
        "dsn": settings.sentry_dsn,
        "environment": settings.app_mode,
        "traces_sample_rate": 0.1,
        "send_default_pii": False,
    }
    release = os.getenv("APP_RELEASE", "").strip()
    if release:
        options["release"] = release
    return options


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)
    # Optional error monitoring.
    options = sentry_options(settings)
    if options:
        try:
            import sentry_sdk
            sentry_sdk.init(**options)
            log.info("Sentry initialized (environment=%s release=%s)",
                     options.get("environment"), options.get("release", "unstamped"))
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


#: The Content-Security-Policy, as directives rather than as a string, so the
#: header and the tests read the same source.
#:
#: Every fetch directive is this origin because the app genuinely has no other:
#: Tailwind and the four Barlow weights are vendored under /static/vendor, the
#: favicon is a data: URI, and `const API = window.location.origin`. That makes
#: the usual allow-list argument disappear entirely.
#:
#: `script-src 'self'` — no `'unsafe-inline'`, no hashes, no `'unsafe-eval'`.
#: Every script the pages run is fetched from this origin (/static/theme.js,
#: /static/app.js, the vendored Tailwind), so `'self'` authorises all of it and
#: there is no hash to regenerate on each edit. An injected <script> does not
#: run and an injected `onerror=` does not compile, which is the attack the
#: `esc()` helper in app.js used to stand against alone — with a session token
#: in localStorage behind it.
#:
#: Getting here took the 196 inline handlers out of the markup first; a static
#: test asserts the count is exactly zero, in both files, so this line cannot
#: quietly stop being true.
#:
#: `style-src 'unsafe-inline'` is PERMANENT, not pending, and the difference
#: matters to whoever reads this next. Tailwind Play computes its stylesheet at
#: runtime from the classes present in the DOM and injects it through a <style>
#: element, so that text cannot be hashed; the 96 style= attributes are the
#: smaller half of the problem. Removing it means precompiling Tailwind — the
#: build step this project has deliberately declined — and that is the whole
#: condition, so nobody has to guess whether it is on somebody's list.
#:
#: The residual risk is small and worth stating rather than implying: an
#: injected <style> or style= is permitted, but the classic exfiltration through
#: `background:url(https://attacker/…)` is already refused by `img-src 'self'`,
#: and the dominant XSS payload is script, which is now fully locked.
#:
#: Never add a nonce or a hash to `style-src`: the spec then IGNORES
#: 'unsafe-inline' for that directive, which blocks Tailwind's own injection and
#: every style= attribute at once, and the app renders as unstyled HTML.
#:
#: One tripwire, so it reads as deliberate rather than as a bug. `img-src` is
#: this origin, and `api/routes/stock.py` returns REMOTE Unsplash/Pexels
#: thumbnail URLs. The SPA never calls /api/stock/search today. The day somebody
#: wires up a stock picker, those thumbnails will be blank — that is the policy
#: working, forcing a proxy decision instead of a hotlink.
CSP_DIRECTIVES: dict[str, str] = {
    "default-src": "'self'",
    "base-uri": "'none'",
    "object-src": "'none'",
    "frame-src": "'none'",
    "frame-ancestors": "'none'",
    "form-action": "'none'",
    "worker-src": "'none'",
    "manifest-src": "'none'",
    "img-src": "'self' data: blob:",
    "media-src": "'self' blob:",
    "font-src": "'self'",
    "connect-src": "'self'",
    "style-src": "'self' 'unsafe-inline'",
    "script-src": "'self'",
}

#: Files served with `Cache-Control: no-cache` — revalidate, not "do not store".
#: StaticFiles sends an ETag and no freshness, so a browser falls back to
#: heuristic caching and can keep serving a stale bundle for days after a deploy.
#: legal.css joins them for the same reason: it is the whole appearance of
#: /terms and /privacy, and a stale copy repaints those pages in last month's
#: palette while the rest of the site has moved.
REVALIDATE_PATHS = frozenset({"/static/app.js", "/static/theme.js",
                              "/static/legal.css"})

#: …and every HTML document, which the path set above cannot express.
#:
#: The rule started as "the bundles must revalidate against a freshly fetched
#: index.html" — and index.html has no Cache-Control either, so the browser
#: applies the same heuristic to the document that carries all of the markup and
#: names both bundles. A markup fix was deployed, verified with curl, and kept
#: rendering in its old form in a real browser; that is what this closes.
#:
#: By content type rather than by path, because the shell is served from `/`,
#: from /static/index.html and from every SPA fallback route an emailed link
#: lands on (/verify, /reset, /team/accept). A hand-kept list of those goes stale
#: the first time somebody adds a route, and goes stale silently.
#:
#: Scoped to documents, not applied to everything: the generated slides and the
#: reel MP4s are immutable once written and are the heaviest bytes here, and a
#: blanket rule would spend a revalidation round trip per image on every feed.
REVALIDATE_CONTENT_TYPE = "text/html"

_DOCS_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})


def security_policy() -> str:
    return "; ".join(f"{name} {value}" for name, value in CSP_DIRECTIVES.items())


def docs_exempt_paths(app_mode: str) -> frozenset[str]:
    """Where the policy must NOT go: Swagger, when Swagger exists.

    It is served only off cloud (_docs_urls above) and it loads its bundle from
    a CDN with an inline bootstrap — a strict policy over it breaks the API docs
    on every developer machine and in the desktop build, somewhere CI never
    looks. In cloud those three paths are unregistered and fall through to the
    SPA, so exempting them there would hand out the app shell with no policy.
    """
    return frozenset() if app_mode == "cloud" else _DOCS_PATHS


class SecurityHeadersMiddleware:
    """Attach the policy to every response.

    Pure ASGI on purpose. `BaseHTTPMiddleware` wraps the response in a task
    group, and this app streams `text/event-stream` from generation and the
    landing demo — a place where that wrapper has a long history of interfering
    with cancellation and response lifetime. Mutating the headers on the
    `http.response.start` message touches nothing else.
    """

    def __init__(self, app, *, policy: str, exempt: frozenset[str],
                 revalidate: frozenset[str]):
        self.app = app
        self.policy = policy
        self.exempt = exempt
        self.revalidate = revalidate

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if path not in self.exempt:
                    headers["content-security-policy"] = self.policy
                if (path in self.revalidate
                        or headers.get("content-type", "").startswith(
                            REVALIDATE_CONTENT_TYPE)):
                    headers["cache-control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_headers)


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

# Security headers. Set by the application rather than by the reverse proxy:
# render.yaml runs the Dockerfile behind Render's own TLS and the desktop build
# is a bare uvicorn, so a policy in the Caddyfile would cover one deployment of
# three. It is also the only way the browser tests see real enforcement.
app.add_middleware(
    SecurityHeadersMiddleware,
    policy=security_policy(),
    exempt=docs_exempt_paths(settings.app_mode),
    revalidate=REVALIDATE_PATHS,
)

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
app.include_router(insights.router)
app.include_router(team.router)
app.include_router(onboarding.router)

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
