from __future__ import annotations
import re
from datetime import date, datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class PostFormat(str, Enum):
    SINGLE = "single"
    CAROUSEL_3 = "carousel_3"
    CAROUSEL_5 = "carousel_5"
    CAROUSEL_10 = "carousel_10"
    INFOGRAPHIC = "infographic"
    REEL = "reel"


class ImageSource(str, Enum):
    STOCK = "stock"
    AI_GEN = "ai_gen"
    CANVA = "canva"
    UPLOAD = "upload"      # the user's own photo, staged before generation


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    LINKEDIN = "linkedin"
    X = "x"


class XPostMode(str, Enum):
    """How an X post is shaped. SHORT is one tweet; THREAD is a chain of tweets,
    each a complete thought continuing the previous; LONG is a single long-form
    post, which only X Premium accounts may publish."""
    SHORT = "short"
    THREAD = "thread"
    LONG = "long"


class XStyle(str, Enum):
    """The ANGLE of an X post (orthogonal to brand voice — voice is WHO speaks,
    style is how THIS post is framed). STANDARD keeps the neutral informative
    take; the rest steer the framing, ported from the user's shorts/thread bot."""
    STANDARD = "standard"
    EDUCATIONAL = "educational"
    HOT_TAKE = "hot_take"
    NEWS_BREAKDOWN = "news_breakdown"


#: Hard cap on tweets per thread. A thread of N tweets spends N of the account's
#: monthly X quota (1,500 on the free tier), so this is deliberately bounded.
MAX_THREAD_TWEETS = 15
#: Per-tweet character budget, hashtags included. Below X's 280 on purpose.
TWEET_CHAR_LIMIT = 250


class LengthTier(str, Enum):
    HOOK_ZONE = "hook_zone"      # ~125 chars
    SWEET_SPOT = "sweet_spot"    # 125-150 then continues
    DEEP_DIVE = "deep_dive"      # 300-900+ chars


class TemplateStyle(str, Enum):
    SQUARE = "square"               # legacy apply_brand path
    BRANDED_CARD = "branded_card"   # portrait 1080x1350 card


# Suggested quick swatches for the niche box. NOT a whitelist — slide colours are
# per-tenant, so any valid #rrggbb is accepted; these are just one-click shortcuts.
NICHE_BOX_PALETTE = ["#ffbf00", "#0076cb", "#5e17eb", "#00bf63", "#000000", "#ff751f"]
HEX_COLOR_RE = r"^#[0-9a-fA-F]{6}$"


class PostStatus(str, Enum):
    DRAFT = "draft"
    PREVIEW = "preview"
    PUBLISHED = "published"
    SCHEDULED = "scheduled"
    EXPORTED = "exported"
    FAILED = "failed"
    # Business approval workflow (Phase 5): draft → in_review → approved → published/rejected
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class OutputChoice(str, Enum):
    PUBLISH = "publish"
    EXPORT = "export"
    SCHEDULE = "schedule"


class StagedUpload(BaseModel):
    """A photo parked for an upcoming generation (POST /api/posts/uploads)."""
    id: str
    filename: str = ""
    bytes: int = 0


# --- Request Models ---

class SlideConfig(BaseModel):
    slide_number: int
    image_source: ImageSource
    search_query: Optional[str] = None
    gen_prompt: Optional[str] = None
    gen_model: Optional[str] = None
    canva_template_id: Optional[str] = None
    upload_id: Optional[str] = None         # id from POST /api/posts/uploads
    page_number: Optional[int] = None       # manual carousel page number; None → none rendered


class GenerateRequest(BaseModel):
    topic: str = Field(..., min_length=3, max_length=500)
    format: PostFormat
    text_model: Optional[str] = None        # None → use DEFAULT_TEXT_MODEL from .env
    image_model: Optional[str] = None       # None → use DEFAULT_IMAGE_MODEL from .env
    default_image_source: ImageSource = ImageSource.STOCK
    # X only: a pure-text post with no image/slides at all. Instagram's API requires
    # media, so the route rejects text_only for any non-X platform.
    text_only: bool = False
    # Own photos, in slide order — required when default_image_source is "upload".
    upload_ids: list[str] = Field(default_factory=list)
    # Batch planning: the calendar date this draft belongs to. Sets scheduled_at
    # WITHOUT scheduling a publish — the post stays a preview for the user to review.
    plan_date: Optional[datetime] = None
    slides: Optional[list[SlideConfig]] = None
    tone: str = "professional"
    niche: Optional[str] = None
    target_audience: Optional[str] = None
    apply_branding: bool = True
    brand_engine: str = "pillow"
    additional_instructions: Optional[str] = None
    platform: Platform = Platform.INSTAGRAM
    length_tier: LengthTier = LengthTier.SWEET_SPOT
    template_style: TemplateStyle = TemplateStyle.BRANDED_CARD
    niche_box_color: Optional[str] = None   # None → brand default; else must be in palette
    show_logo: bool = True
    brand_config_id: Optional[str] = None   # None → default brand preset
    brand_voice_preset: Optional[str] = None  # per-post override of the saved brand voice
    # X only. thread_min/max bound how many tweets a thread may have; the model
    # picks a number inside the range that fits the topic, rather than padding or
    # squeezing the argument to hit an exact count.
    x_mode: XPostMode = XPostMode.SHORT
    # X only: the framing angle of the post (standard / educational / hot take /
    # news breakdown). Composes with the brand voice.
    x_style: XStyle = XStyle.STANDARD
    thread_min: int = Field(3, ge=2, le=MAX_THREAD_TWEETS)
    thread_max: int = Field(7, ge=2, le=MAX_THREAD_TWEETS)

    @model_validator(mode="after")
    def _validate_thread_range(self):
        if self.thread_min > self.thread_max:
            raise ValueError("thread_min cannot be greater than thread_max")
        return self

    @field_validator("niche_box_color")
    @classmethod
    def _validate_niche_box_color(cls, v: Optional[str]) -> Optional[str]:
        """Any valid #rrggbb — slide colours are per-tenant, the palette is only
        a set of suggested swatches."""
        if v is None:
            return v
        v = v.strip().lower()
        if not re.fullmatch(HEX_COLOR_RE, v):
            raise ValueError("niche_box_color must be a hex colour like #ff751f")
        return v


class ScheduleRequest(BaseModel):
    publish_at: datetime


# --- Batch plan (a week of topics, reviewed before any post is generated) ---

class PlanRequest(BaseModel):
    theme: Optional[str] = Field(None, max_length=200)
    count: int = Field(7, ge=2, le=14)          # a carousel of a week or two, bounded
    start_date: date
    cadence_days: int = Field(1, ge=1, le=7)    # 1 = daily, 2 = every other day, …
    platform: Platform = Platform.INSTAGRAM


class PlanItem(BaseModel):
    topic: str
    pillar: str
    pillar_label: str
    angle: str = ""
    date: date


class PlanResponse(BaseModel):
    items: list[PlanItem] = []


class CaptionUpdate(BaseModel):
    caption: Optional[str] = None
    hashtags: Optional[list[str]] = None
    cta: Optional[str] = None
    seo_keywords: Optional[list[str]] = None
    thread_parts: Optional[list[str]] = None   # edited X thread, in order


class ReplaceSlideRequest(BaseModel):
    """Body for re-fetching a single slide's image without re-running the whole post."""
    search_query: Optional[str] = None             # override stored query
    image_source: Optional[ImageSource] = None     # switch stock ↔ ai_gen
    gen_prompt: Optional[str] = None               # for ai_gen
    image_model: Optional[str] = None              # for ai_gen
    stock_source: Optional[str] = None             # "unsplash" | "pexels" | "auto"


class OverlayUpdateRequest(BaseModel):
    """Body for editing overlay text in-place on an existing slide (re-renders
    over the stored raw image, no fresh image fetch)."""
    overlay_text: Optional[str] = None        # description box (white, lower)
    niche_text: Optional[str] = None          # niche box (colored, upper) — slide 1 only typically


class RegenFieldRequest(BaseModel):
    field: str = Field(..., pattern="^(caption|hook|cta|hashtags|seo_keywords)$")
    count: int = Field(4, ge=1, le=8)


class RegenFieldResponse(BaseModel):
    field: str
    variants: list                              # list[str] or list[list[str]]


class PostInsightSchema(BaseModel):
    snapshot_at: datetime
    reach: Optional[int] = None
    impressions: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    saved: Optional[int] = None
    shares: Optional[int] = None
    total_interactions: Optional[int] = None
    plays: Optional[int] = None
    video_views: Optional[int] = None

    model_config = {"from_attributes": True}


# --- Response Models ---

class SlidePreview(BaseModel):
    slide_number: int
    image_url: str
    image_source: ImageSource
    width: int
    height: int
    attribution: Optional[dict] = None     # stock credits, see services.stock
    # overlay editing
    overlay_text: Optional[str] = None
    niche_text: Optional[str] = None
    original_overlay_text: Optional[str] = None   # LLM-generated, for Reset
    original_niche_text: Optional[str] = None
    has_raw_image: bool = False                   # True if PUT /overlay is supported


class ClaimFlag(BaseModel):
    """A sentence the author should verify before posting (a number, a stat, or a
    named source). Not a verdict on truth — a prompt to check it."""
    text: str
    reason: str


class VerifiedClaim(BaseModel):
    """A Business claim after LLM verification against the source (Phase 4)."""
    claim: str
    status: str                       # confirmed | unconfirmed
    evidence: str = ""                # verbatim source support, only when confirmed


class PostPreview(BaseModel):
    id: str
    topic: str
    format: PostFormat
    status: PostStatus
    caption: str
    thread_parts: list[str] = []      # non-empty only for an X thread
    hashtags: list[str]
    seo_keywords: list[str] = []
    cta: Optional[str]
    hook: Optional[str]
    platform: Platform = Platform.INSTAGRAM
    slides: list[SlidePreview]
    text_model_used: str
    image_model_used: Optional[str]
    created_at: datetime
    sources: list[dict] = []           # [{title,url}] from web-grounded LLM (:online)
    claims: list[ClaimFlag] = []       # sentences to verify (computed from the caption)
    # Business (Phase 4): LLM-verified claims + brand-rule flags, read from Post.claim_check.
    checked_claims: list[VerifiedClaim] = []
    brand_flags: dict = {}             # {"forbidden":[...],"missing_disclaimers":[...]}
    # Outcome of the opt-in creator fact check: {"status","sources_used","checked_at"}.
    # Empty until it is run. Without the status the UI cannot tell "we checked and
    # found nothing to flag" from "there was nothing to check against" — and those
    # mean opposite things to an author about to publish.
    fact_check: dict = {}
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    schedule_error: Optional[str] = None
    instagram_media_id: Optional[str] = None

    model_config = {"from_attributes": True}


class PostSummary(BaseModel):
    id: str
    topic: str
    format: PostFormat
    status: PostStatus
    thumb_url: Optional[str] = None      # first slide image, for grid/calendar
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    created_at: datetime
    # Why the last publish failed. Carried in the LIST so a failures view can show
    # the reason without fetching every post individually.
    schedule_error: Optional[str] = None

    model_config = {"from_attributes": True}


class PublishResult(BaseModel):
    success: bool
    instagram_media_id: Optional[str] = None   # platform post id
    published_url: Optional[str] = None         # permalink to the published post
    error: Optional[str] = None


class ReelRequest(BaseModel):
    """Options for building a Reel. No body (old clients) = the classic silent
    Ken Burns slideshow; voiceover adds TTS narration + burned-in subtitles;
    visuals="broll" swaps slides for stock video clips (needs voiceover)."""
    voiceover: bool = False
    voice_id: Optional[str] = Field(None, max_length=80)   # ElevenLabs voice
    visuals: str = "slides"
    music: bool = False    # tenant's uploaded track, ducked under the voice (R3)
    cover: bool = False    # slide 1 replaces the first 0.5s of video (R3)

    @field_validator("visuals")
    @classmethod
    def _visuals_known(cls, v: str) -> str:
        v = (v or "slides").strip().lower()
        if v not in {"slides", "broll"}:
            raise ValueError("visuals must be 'slides' or 'broll'")
        return v


class ExportResult(BaseModel):
    download_url: str
    filename: str
    size_bytes: int


class ModelInfo(BaseModel):
    id: str
    name: str
    provider: str


class StockPhoto(BaseModel):
    id: str
    url: str
    thumb_url: str
    alt: Optional[str] = None
    source: str  # "unsplash" or "pexels"


class CanvaDesign(BaseModel):
    id: str
    title: str
    thumbnail_url: Optional[str] = None


class BrandConfigSchema(BaseModel):
    id: Optional[str] = None
    name: str = "Default"
    is_default: bool = False
    logo_path: Optional[str] = None
    primary_color: str = "#2E75B6"
    secondary_color: str = "#1A4D8A"
    accent_color: str = "#F0A500"
    heading_font_path: Optional[str] = None
    body_font_path: Optional[str] = None
    logo_position: str = "bottom_right"
    logo_scale: float = 0.15
    padding: int = 40
    template_style: str = "branded_card"
    niche_box_color: str = "#ff751f"
    niche_box_palette: list[str] = NICHE_BOX_PALETTE
    description_box_alpha: float = 0.79
    show_logo: bool = True

    model_config = {"from_attributes": True}


# --- Brand voice (generation style preference) ---

class BrandVoiceUpdate(BaseModel):
    preset: Optional[str] = None                       # a preset key or "custom"
    custom: Optional[str] = Field(None, max_length=800)  # used when preset == "custom"


class BrandVoiceResponse(BaseModel):
    preset: str                    # current saved preset (default "balanced")
    custom: str = ""               # current custom text (empty unless preset == "custom")
    presets: list[dict] = []       # [{key,label,description}] for the settings UI


# --- Brand profile (niche/audience/brand set once, used as generation defaults) ---

class ProfileUpdate(BaseModel):
    niche: Optional[str] = Field(None, max_length=120)
    target_audience: Optional[str] = Field(None, max_length=120)
    brand_name: Optional[str] = Field(None, max_length=120)


class ProfileResponse(BaseModel):
    niche: str = ""
    target_audience: str = ""
    brand_name: str = ""


# --- Slide colours (per-tenant branding of the generated slides) ---

class SlideStyleUpdate(BaseModel):
    accent_color: Optional[str] = None      # niche box fill; "" clears to default
    text_box_color: Optional[str] = None    # description box fill

    @field_validator("accent_color", "text_box_color")
    @classmethod
    def _validate_hex(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        v = v.strip().lower()
        if not re.fullmatch(HEX_COLOR_RE, v):
            raise ValueError("colour must be a hex value like #ff751f")
        return v


class SlideStyleResponse(BaseModel):
    accent_color: str = ""          # empty → the platform default is used
    text_box_color: str = ""
    default_accent_color: str = ""  # what generation falls back to when unset
    palette: list[str] = []         # suggested swatches for the UI


# --- X-specific account settings ---

class PostPreset(BaseModel):
    """A saved bundle of composer settings — the "how" of a post, not the "what"
    (topic/niche live elsewhere). Applied in one click from the composer."""
    name: str = Field(..., min_length=1, max_length=40)
    format: PostFormat = PostFormat.SINGLE
    tone: str = Field("professional", max_length=40)
    length_tier: LengthTier = LengthTier.SWEET_SPOT
    default_image_source: ImageSource = ImageSource.STOCK
    platform: Platform = Platform.INSTAGRAM
    template_style: TemplateStyle = TemplateStyle.BRANDED_CARD
    niche_box_color: Optional[str] = None
    apply_branding: bool = True
    show_logo: bool = True

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("preset name cannot be blank")
        return v

    @field_validator("niche_box_color")
    @classmethod
    def _validate_color(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        v = v.strip().lower()
        if not re.fullmatch(HEX_COLOR_RE, v):
            raise ValueError("niche_box_color must be a hex value like #ff751f")
        return v

    @field_validator("default_image_source")
    @classmethod
    def _no_upload(cls, v: ImageSource) -> ImageSource:
        # A preset stores settings, not files — "my photos" can't be a default.
        if v == ImageSource.UPLOAD:
            raise ValueError("a preset cannot use uploaded photos as its image source")
        return v


class PresetsUpdate(BaseModel):
    presets: list[PostPreset] = Field(default_factory=list)

    @field_validator("presets")
    @classmethod
    def _bounded_and_unique(cls, v: list[PostPreset]) -> list[PostPreset]:
        if len(v) > 20:
            raise ValueError("at most 20 presets")
        # De-dupe by name, last one wins (a re-save replaces the old).
        by_name: dict[str, PostPreset] = {}
        for p in v:
            by_name[p.name.lower()] = p
        return list(by_name.values())


class PresetsResponse(BaseModel):
    presets: list[PostPreset] = []


class LogoSettingsResponse(BaseModel):
    """The tenant's brand logo — whether one is set, and where to preview it."""
    set: bool = False
    url: Optional[str] = None


class MusicSettingsResponse(BaseModel):
    """The tenant's Reel background-music track — whether one is uploaded."""
    set: bool = False


class XSettingsUpdate(BaseModel):
    # Our own record of the tenant's X plan, not a check against X. Enabling it
    # unlocks the long-post mode; if the account isn't actually Premium, X
    # rejects the tweet and we surface its error.
    x_premium: bool


class XSettingsResponse(BaseModel):
    x_premium: bool = False
    tweet_char_limit: int = TWEET_CHAR_LIMIT
    max_thread_tweets: int = MAX_THREAD_TWEETS


# --- AI provider + model selection (per tenant, they pay for it) ---

class AISettingsUpdate(BaseModel):
    text_provider: Optional[str] = None
    text_model: Optional[str] = Field(None, max_length=120)
    image_provider: Optional[str] = None
    image_model: Optional[str] = Field(None, max_length=120)


class AISettingsResponse(BaseModel):
    text_provider: str = ""
    text_model: str = ""
    image_provider: str = ""
    image_model: str = ""
    #: {provider_key: {"set": bool, "masked": str|None}} — never raw keys.
    keys: dict = {}


class AITestRequest(BaseModel):
    kind: str = "text"              # "text" | "image"


class AITestResponse(BaseModel):
    ok: bool
    message: str


class PublishTestRequest(BaseModel):
    platform: str                   # "x" | "instagram"


class PublishTestResponse(BaseModel):
    ok: bool
    message: str
    handle: Optional[str] = None    # @username / account handle on success


# ===== Business module (Phase 2-3) =====

class SourceStatus(str, Enum):
    OK = "ok"
    UNREACHABLE = "unreachable"
    FORMAT_CHANGED = "format_changed"


class LeadStrength(str, Enum):
    WORTHY = "worthy"
    WEAK = "weak"
    DUPLICATE = "duplicate"


class LeadStatus(str, Enum):
    NEW = "new"
    DISMISSED = "dismissed"
    SNOOZED_KIND = "snoozed_kind"
    DRAFTED = "drafted"
    DIGESTED = "digested"


class SourceCreate(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        s = (v or "").strip()
        if not (s.startswith("http://") or s.startswith("https://")):
            raise ValueError("Enter a public http(s) URL")
        if len(s) > 500:
            raise ValueError("URL is too long")
        return s


class SourceOut(BaseModel):
    id: str
    url: str
    kind: str
    status: str
    last_checked_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class LeadOut(BaseModel):
    id: str
    what_happened: Optional[str] = None
    source_url: Optional[str] = None
    quote: Optional[str] = None
    published_at: Optional[datetime] = None
    why_interesting: Optional[str] = None
    strength: Optional[str] = None
    reason: Optional[str] = None
    missing: Optional[list[str]] = None
    sensitive: bool = False
    status: str
    created_at: Optional[datetime] = None


class DigestRequest(BaseModel):
    lead_ids: list[str] = Field(..., min_length=1, max_length=20)


class BrandRulesUpdate(BaseModel):
    forbidden: list[str] = []
    required_disclaimers: list[str] = []

    @field_validator("forbidden", "required_disclaimers")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in (v or []) if s and s.strip()][:50]


class BrandRulesOut(BaseModel):
    forbidden: list[str] = []
    required_disclaimers: list[str] = []


class DraftEditRequest(BaseModel):
    caption: str = Field(..., max_length=8000)


class LimitsUpdate(BaseModel):
    max_per_day: Optional[int] = Field(None, ge=1, le=100)
    max_per_week: Optional[int] = Field(None, ge=1, le=100)


class LimitsOut(BaseModel):
    max_per_day: Optional[int] = None
    max_per_week: Optional[int] = None


class SourceAnalyticsOut(BaseModel):
    """One source's pipeline funnel (Phase 8): leads → worthy → drafts →
    approved/published, plus engagement of the posts that were published and had
    their metrics refreshed. Engagement is Instagram-only (X has no in-app metrics)
    and needs a manual insights refresh, so it's sparse but real once posts go out."""
    source_id: str
    url: str
    kind: str
    status: str
    last_checked_at: Optional[datetime] = None
    last_lead_at: Optional[datetime] = None
    leads_total: int = 0
    worthy: int = 0
    weak: int = 0
    dismissed: int = 0
    drafts: int = 0
    in_review: int = 0
    approved: int = 0
    published: int = 0
    reach: int = 0               # sum of the latest reach snapshot per published post
    engagement: int = 0          # sum of latest likes+comments+saves+shares per post
    measured_posts: int = 0      # published posts that have an insights snapshot
    worthy_rate: float = 0.0     # worthy / leads_total
    approve_rate: float = 0.0    # approved / drafts


class SourceAnalyticsResponse(BaseModel):
    sources: list[SourceAnalyticsOut] = []
    totals: dict = {}
    digests: int = 0             # digest posts (span multiple leads → no single source)


# ===== Managed accounts (Phase 7: agency multi-account) =====

class AccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class AccountUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    brand_voice_preset: Optional[str] = Field(None, max_length=30)
    brand_voice_custom: Optional[str] = None
    niche: Optional[str] = Field(None, max_length=120)
    target_audience: Optional[str] = Field(None, max_length=120)
    brand_name: Optional[str] = Field(None, max_length=120)
    slide_accent_color: Optional[str] = None
    slide_text_box_color: Optional[str] = None

    @field_validator("slide_accent_color", "slide_text_box_color")
    @classmethod
    def _validate_hex(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        v = v.strip().lower()
        if not re.fullmatch(HEX_COLOR_RE, v):
            raise ValueError("colour must be a hex value like #ff751f")
        return v


class AccountOut(BaseModel):
    id: str
    name: Optional[str] = None
    brand_voice_preset: Optional[str] = None
    brand_voice_custom: Optional[str] = None
    niche: Optional[str] = None
    target_audience: Optional[str] = None
    brand_name: Optional[str] = None
    slide_accent_color: Optional[str] = None
    slide_text_box_color: Optional[str] = None
    has_logo: bool = False


class AccountListResponse(BaseModel):
    accounts: list[dict] = []            # [{id, name}]
    active_account_id: Optional[str] = None


class AccountSwitch(BaseModel):
    account_id: Optional[str] = None     # None → Personal
