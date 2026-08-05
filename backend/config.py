from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).parent          # backend/
_ROOT = _HERE.parent                   # project root/

# Search order: root .env first, then backend/.env (backend wins on conflict).
# This way the app works whether someone puts .env in the root or in backend/.
_ENV_FILES = [
    str(_ROOT / ".env"),   # project root  (lower priority)
    str(_HERE / ".env"),   # backend/      (higher priority)
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILES, extra="ignore")

    # Core
    database_url: str = "sqlite+aiosqlite:///./insta.db"
    secret_key: str = "change-me-in-production"  # signs JWTs AND derives the vault key
    # Optional dedicated key for encrypting user credentials at rest. If empty, the
    # vault key is derived from secret_key. MUST stay stable — rotating it orphans
    # every stored user secret. See services/secrets.py.
    encryption_key: str = ""
    api_token: str = ""  # legacy single-tenant Bearer; superseded by JWT auth (cloud)
    log_level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR

    # Certificates are ALWAYS verified against the OS trust store — see
    # services/http_utils.setup_tls, installed at every process entry point — so an
    # antivirus or corporate proxy that inspects HTTPS needs no configuration here.
    #
    # This flag only decides whether the clients that accept an ssl_verify argument
    # (OpenRouter, Unsplash, Pexels) verify at all. Setting it False exposes those
    # API keys to man-in-the-middle interception, and does NOT affect Instagram,
    # imgbb or the Telegram bot, which always verify.
    # It is a last resort, not the fix for TLS-inspecting security software.
    ssl_verify: bool = True

    # Outbound fetches to a host somebody else chose (a Business source, a site
    # we read a brand off) are refused when the name resolves into private
    # space — see services/url_guard.py. Turning this on lets a self-hoster
    # watch an internal wiki; on a public deployment it re-opens the server as
    # a window into its own network, so it stays off.
    allow_private_urls: bool = False

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_referer: str = "https://localhost"
    openrouter_app_title: str = "InstaContentEngine"

    # Other AI providers. Cloud tenants set these per account (encrypted); these
    # .env values are the local/desktop fallback.
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""

    # LOCAL/DESKTOP ONLY. Cloud tenants choose provider + model in
    # Account → AI models; there is deliberately no platform default there, so a
    # user is never billed for a model someone else picked.
    default_text_provider: str = "openrouter"
    default_image_provider: str = "openrouter"
    default_text_model: str = "anthropic/claude-sonnet-5"
    default_image_model: str = "google/gemini-3.1-flash-image"

    # Public no-auth demo (Business landing). Runs on the app's OWN OpenRouter key
    # (openrouter_api_key), not a user's — anonymous visitors have none. A hard
    # per-IP rate limit protects the spend; an empty openrouter_api_key makes the
    # demo 503 rather than fail mid-run. Empty demo_text_model → default_text_model.
    demo_text_model: str = ""

    # Stock Photos
    unsplash_access_key: str = ""
    pexels_api_key: str = ""

    # ElevenLabs TTS — voiceover Reels (R1). The key is per-user in cloud (overlaid
    # by build_settings_for_user); the voice id is a non-secret default (Rachel),
    # overridable per request or via .env.
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"

    # Kling AI video generation. A single bearer key (the current API key
    # format), per-user in cloud like every key above it — not part of the
    # PROVIDERS/resolve_ai_choice text-image system, since it has no text or
    # image models and is priced per second rather than per token.
    kling_api_key: str = ""
    # B-roll frame judge (Reels R2) — a cheap vision model on the user's OpenRouter
    # key; fail-open, so an empty/wrong model just disables the filter.
    broll_judge_model: str = "google/gemini-2.0-flash-001"

    # Canva
    canva_client_id: str = ""
    canva_client_secret: str = ""
    canva_redirect_uri: str = "http://localhost:3000/auth/canva/callback"

    # Instagram / Meta
    instagram_access_token: str = ""
    instagram_user_id: str = ""
    meta_app_id: str = ""
    meta_app_secret: str = ""

    # X / Twitter — OAuth 1.0a user context (single brand account).
    # From the X developer portal (needs a paid Basic tier to post).
    x_api_key: str = ""            # consumer key
    x_api_secret: str = ""         # consumer secret
    x_access_token: str = ""
    x_access_token_secret: str = ""

    # Telegram
    telegram_bot_token: str = ""

    # Storage
    storage_type: str = "local"  # "local" or "s3"
    storage_path: str = "./uploads"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_s3_bucket: str = "insta-engine-assets"
    aws_region: str = "us-east-1"

    # App
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    frontend_url: str = "http://localhost:3000"

    # Image hosting (public URLs for Instagram publishing)
    imgbb_api_key: str = ""

    # Business sources. Empty → GitHub is read anonymously, which is capped at 60
    # requests/hour PER IP — and on a server that IP is shared by every tenant, so
    # the poller starts failing once a handful of sources exist. Any classic PAT
    # (no scopes needed for public repos) raises the cap to 5,000/hour.
    github_token: str = ""

    # Deployment mode: "local" (desktop, scheduler runs only while app open) or
    # "cloud" (24/7 backend, scheduled posts publish even when user's PC is off)
    app_mode: str = "local"
    public_base_url: str = ""   # e.g. https://myengine.up.railway.app (cloud mode)
    # Comma-separated emails granted admin (backup/restore) in cloud, where there
    # is no implicit local owner. e.g. ADMIN_EMAILS=me@example.com
    admin_emails: str = ""

    # === Email (Resend) — for verification + password reset ===
    resend_api_key: str = ""                       # empty → emails become no-ops (dev)
    email_from: str = "Content Engine <onboarding@resend.dev>"
    # Enforce a verified email before publishing. Keep FALSE until a real sending
    # domain is verified in Resend (shared sender has weak deliverability).
    require_verified_email: bool = False

    # Error monitoring (optional). Empty → Sentry not initialized.
    sentry_dsn: str = ""

    # Reel-render backend: builds a video from a post's own slides. "kenburns"
    # (local ffmpeg slideshow) is the only one — NOT to be confused with
    # services/video/genai's Kling prompt-to-video generation, an unrelated
    # system for the standalone Video tab.
    video_provider: str = "kenburns"


@lru_cache
def get_settings() -> Settings:
    return Settings()
