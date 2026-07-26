import uuid
from sqlalchemy import (
    Boolean, Column, Float, ForeignKey, Index, Integer, String, Text, DateTime,
    JSON, UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func


class Base(AsyncAttrs, DeclarativeBase):
    pass


class User(Base):
    """An account. In cloud mode users register and log in; in local (desktop)
    mode a single implicit user (is_local=True) is seeded at startup and owns
    everything, so the desktop keeps working without a login screen."""
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(Text)          # argon2; null for the implicit local user
    is_active = Column(Boolean, default=True)
    is_local = Column(Boolean, default=False)   # the seeded desktop owner
    is_admin = Column(Boolean, default=False)   # may download/restore the whole DB
    email_verified = Column(Boolean, default=False)
    # Bumped on password reset / logout-all; access tokens carry the version they
    # were minted with, so a mismatch invalidates every token issued before.
    token_version = Column(Integer, default=0, nullable=False)
    # Brand voice preference (not a secret): which voice preset to generate in, and
    # the free-text voice when the preset is "custom". Steers style only; the output
    # format/rules stay fixed (see services/brand_voice).
    brand_voice_preset = Column(String(30))
    brand_voice_custom = Column(Text)
    # Brand profile (not a secret): set once at onboarding, used as defaults for
    # every post so the composer isn't an empty form, and to steer generation into
    # the tenant's own niche. All optional; the composer can still override per post.
    niche = Column(String(120))
    target_audience = Column(String(120))
    brand_name = Column(String(120))
    # Slide colours (per-tenant). Null → the platform default brand preset is used.
    slide_accent_color = Column(String(7))     # niche box fill, "#rrggbb"
    slide_text_box_color = Column(String(7))   # description box fill
    logo_path = Column(Text)                   # the tenant's own brand logo, drawn on slides
    post_presets = Column(JSON)                # saved composer settings, [{name, ...}]
    # Which AI provider + model this tenant generates with. Text and images are
    # independent (e.g. OpenRouter for copy, Google for images). The API key for
    # each provider lives encrypted on UserCredentials. No platform default in
    # cloud: unset means generation is blocked until the user chooses.
    text_provider = Column(String(30))
    text_model = Column(String(120))
    image_provider = Column(String(30))
    image_model = Column(String(120))
    # X Premium lifts the 280-char cap, which unlocks the "long post" mode. This is
    # our own flag, not a check against X — publishing a long post without real
    # Premium will still be rejected by X itself.
    x_premium = Column(Boolean, default=False)
    # Which product this account signed up for: "creator" (individuals, SMM,
    # influencers — today's app) or "business" (companies — sources → leads →
    # approval workflow). One engine underneath; this only splits the experience
    # (landing, onboarding, which sections show). Business screens gate on this.
    account_type = Column(String(20), default="creator")
    # Agency multi-account (Phase 7): which managed brand is active in the composer.
    # NULL = "Personal" (this user's own settings above). Never a security boundary —
    # posts are always owned by user_id; this only scopes the view + brand identity.
    # Plain column (no FK) to avoid a users<->managed_accounts create_all cycle; the
    # app clears it when the account is deleted.
    active_account_id = Column(String(36))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    credentials = relationship("UserCredentials", back_populates="user",
                               uselist=False, cascade="all, delete-orphan")


class UserCredentials(Base):
    """Per-user API keys, encrypted at rest (Fernet). One row per user. The app
    is custodian of these secrets — they are never logged and never returned to
    the client in plaintext (see api/routes/settings.py)."""
    __tablename__ = "user_credentials"

    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"),
                     primary_key=True)
    # One key per provider — it serves both text and images when the user picks
    # the same provider for both.
    openrouter_api_key_enc = Column(Text)
    openai_api_key_enc = Column(Text)
    anthropic_api_key_enc = Column(Text)
    google_api_key_enc = Column(Text)
    instagram_access_token_enc = Column(Text)
    instagram_user_id_enc = Column(Text)
    imgbb_api_key_enc = Column(Text)
    x_api_key_enc = Column(Text)
    x_api_secret_enc = Column(Text)
    x_access_token_enc = Column(Text)
    x_access_token_secret_enc = Column(Text)
    unsplash_access_key_enc = Column(Text)
    pexels_api_key_enc = Column(Text)
    elevenlabs_api_key_enc = Column(Text)   # TTS for voiceover Reels (R1)
    # Per-network health + token expiry, e.g.
    # {"instagram": {"ok": true, "checked_at": ..., "expires_at": ..., "expires_estimated": true}}
    # JSON rather than a column per network so adding LinkedIn needs no migration.
    connection_health = Column(JSON)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())

    user = relationship("User", back_populates="credentials")


class Post(Base):
    __tablename__ = "posts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    topic = Column(Text, nullable=False)
    format = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="draft")
    caption = Column(Text)
    # X thread: the individual tweets, in order. NULL for everything else. For a
    # thread `caption` holds the parts joined by blank lines, so export, pillar
    # classification and the feed keep working unchanged — but note that means
    # len(caption) is the whole thread, not one tweet.
    thread_parts = Column(JSON)
    hashtags = Column(JSON)          # stored as JSON array
    seo_keywords = Column(JSON)      # separate from hashtags
    sources = Column(JSON)           # [{title,url}] from web-grounded LLM (:online)
    cta = Column(Text)
    hook = Column(Text)
    alt_text = Column(Text)
    platform = Column(String(20), default="instagram")
    template_style = Column(String(20), default="branded_card")
    text_model = Column(String(100))
    image_model = Column(String(100))
    brand_engine = Column(String(20), default="pillow")
    instagram_media_id = Column(String(100))   # platform post id (name kept for back-compat)
    published_url = Column(Text)                # permalink to the published post
    scheduled_at = Column(DateTime(timezone=True))
    published_at = Column(DateTime(timezone=True))
    published_image_urls = Column(JSON)   # imgbb public URLs used for publishing
    schedule_error = Column(Text)          # last publish failure (status=failed)
    # Retries the scheduled-publish job has already spent. Reset on success and on
    # every (re)schedule, so a fresh slot always starts with the full budget.
    publish_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    pillar = Column(String(30))            # content pillar (educational/inspirational/...)
    video_path = Column(Text)              # generated Reel MP4 on disk
    # Business origin (Phase 3): a post drafted from a source lead. All nullable —
    # creator posts leave them empty. lead_id is SET NULL so a post survives its
    # lead being deleted; source_kind marks it "from Business" for badges/filters.
    lead_id = Column(String(36), ForeignKey("leads.id", ondelete="SET NULL"), index=True)
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    source_kind = Column(String(20))
    # Business claim check (Phase 4): LLM verdict per factual claim + brand-rule flags,
    # computed once at draft time (expensive), read back on preview. Creator posts NULL.
    # {"claims":[{text,status,evidence}], "brand":{"forbidden":[...],"missing_disclaimers":[...]}}
    claim_check = Column(JSON)
    # Business approval workflow (Phase 5): snapshot of the AI's first caption, kept so
    # the audit journal can show AI draft vs the human's edits. Creator posts NULL.
    ai_caption = Column(Text)
    # Agency multi-account (Phase 7): which managed brand this post belongs to. NULL =
    # the owner's Personal account. Additive to user_id (which stays the security gate).
    managed_account_id = Column(String(36), ForeignKey("managed_accounts.id", ondelete="SET NULL"),
                                index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    slides = relationship("Slide", back_populates="post", cascade="all, delete-orphan")
    insights = relationship("PostInsight", back_populates="post", cascade="all, delete-orphan")


class Slide(Base):
    __tablename__ = "slides"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    post_id = Column(String(36), ForeignKey("posts.id", ondelete="CASCADE"))
    slide_number = Column(Integer, nullable=False)
    page_number = Column(Integer)    # manual carousel page number
    image_source = Column(String(20), nullable=False)
    image_path = Column(Text)        # absolute path on disk
    image_url = Column(Text)
    search_query = Column(Text)
    gen_prompt = Column(Text)
    attribution = Column(JSON)        # {source, author_name, author_profile_url, source_link}
    render_params = Column(JSON)      # overlay text + brand config used so single-slide regenerate can reproduce the look
    raw_image_path = Column(Text)     # unbranded background JPEG path, used by PUT /overlay
    original_overlay_text = Column(Text)   # LLM-generated overlay text, for Reset
    original_niche_text = Column(Text)     # LLM-generated niche text (slide 1), for Reset
    gen_model = Column(String(100))
    canva_template_id = Column(String(100))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    post = relationship("Post", back_populates="slides")


class BrandConfig(Base):
    __tablename__ = "brand_configs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    is_default = Column(Boolean, default=False)
    logo_path = Column(Text)
    primary_color = Column(String(7))
    secondary_color = Column(String(7))
    accent_color = Column(String(7))
    heading_font_path = Column(Text)
    body_font_path = Column(Text)
    logo_position = Column(String(20), default="bottom_right")
    logo_scale = Column(Float, default=0.15)
    padding = Column(Integer, default=40)
    template_style = Column(String(20), default="branded_card")
    niche_box_color = Column(String(7), default="#ff751f")
    niche_box_palette = Column(JSON)
    description_box_alpha = Column(Float, default=0.79)
    show_logo = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CanvaToken(Base):
    __tablename__ = "canva_tokens"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class InstagramToken(Base):
    __tablename__ = "instagram_tokens"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    access_token = Column(Text, nullable=False)
    ig_user_id = Column(String(100), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class LLMUsage(Base):
    """One row per OpenRouter call — for the cost dashboard / badge."""
    __tablename__ = "llm_usage"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    model = Column(String(120))
    prompt_tokens = Column(Integer)
    completion_tokens = Column(Integer)
    total_tokens = Column(Integer)
    cost = Column(Float, default=0.0)      # USD, from OpenRouter usage.cost
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PostInsight(Base):
    """A point-in-time snapshot of a published post's Instagram metrics."""
    __tablename__ = "post_insights"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    post_id = Column(String(36), ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    snapshot_at = Column(DateTime(timezone=True), server_default=func.now())
    reach = Column(Integer)
    impressions = Column(Integer)
    likes = Column(Integer)
    comments = Column(Integer)
    saved = Column(Integer)
    shares = Column(Integer)
    total_interactions = Column(Integer)
    plays = Column(Integer)          # video / Reels only
    video_views = Column(Integer)    # video / Reels only
    raw = Column(JSON)               # full Graph API response

    post = relationship("Post", back_populates="insights")


# ===== Business module (Phase 2): sources → leads =====

class Workspace(Base):
    """A business space. One per business user for now (unique owner); the FK is
    the seam for future multi-brand/agency ownership without another rewrite."""
    __tablename__ = "workspaces"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"),
                           unique=True, index=True)
    name = Column(String(120))
    # Publishing frequency caps (Phase 6). NULL = no limit. Enforced at publish time.
    max_per_day = Column(Integer)
    max_per_week = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Source(Base):
    """A public link a workspace watches (release notes, a feed, a changelog page)."""
    __tablename__ = "sources"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    url = Column(Text, nullable=False)
    kind = Column(String(30), nullable=False)          # github_releases | rss | generic_page
    status = Column(String(20), nullable=False, default="ok")   # ok | unreachable | format_changed
    active = Column(Boolean, default=True)
    config = Column(JSON)
    last_checked_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SourceSnapshot(Base):
    """One row per item ever seen at a source — the key for change/dedup detection.
    Unique on (source_id, external_id) so a re-poll of the same item is a no-op."""
    __tablename__ = "source_snapshots"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id = Column(String(36), ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    external_id = Column(String(255), nullable=False)   # stable per-item key from the fetcher
    fingerprint = Column(String(64))                    # hash of title+body to catch edits
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_snapshot_source_external"),
    )


class Lead(Base):
    """A newsworthy thing that happened at a source. Raw fields + rules verdict are
    written by the (LLM-free) poller; why_interesting/missing stay NULL until the
    user drafts (Phase 3), which is the only place LLM spend happens."""
    __tablename__ = "leads"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    source_id = Column(String(36), ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    external_id = Column(String(255))
    what_happened = Column(Text)          # the source item's headline (free, from the poller)
    source_url = Column(Text)
    quote = Column(Text)                  # a short excerpt of the source body
    published_at = Column(DateTime(timezone=True))
    why_interesting = Column(Text)        # LLM, NULL until the user drafts this lead
    strength = Column(String(20))         # worthy | weak | duplicate
    reason = Column(Text)                 # one-line explanation from the rules
    sensitive = Column(Boolean, default=False)  # bad-news flag (Phase 6): warn before posting
    missing = Column(JSON)                # LLM, NULL until drafted: questions the source doesn't answer
    status = Column(String(20), nullable=False, default="new")  # new|dismissed|snoozed_kind|drafted|digested
    raw = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_leads_ws_status", "workspace_id", "status"),
        Index("ix_leads_ws_created", "workspace_id", "created_at"),
    )


class BrandRules(Base):
    """Per-workspace 'what the brand allows' (Phase 4). Not a legal check — the user's
    own forbidden phrases + required disclaimers, applied to every Business draft."""
    __tablename__ = "brand_rules"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"),
                          unique=True, index=True)
    forbidden = Column(JSON)              # list of phrases that must not appear
    required_disclaimers = Column(JSON)   # list of phrases that must appear
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AuditEntry(Base):
    """The human-in-the-loop record (Phase 5): one row per approval, snapshotting the
    AI draft vs the human's edits, who signed off and when. For an agency this is the
    report to the client."""
    __tablename__ = "audit_entries"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    post_id = Column(String(36), ForeignKey("posts.id", ondelete="SET NULL"), index=True)
    lead_id = Column(String(36))
    source_url = Column(Text)
    ai_draft = Column(Text)               # the caption as first generated
    human_edits = Column(Text)            # the caption at approval time
    approved_by = Column(String(36))      # user id
    approved_at = Column(DateTime(timezone=True))
    published_url = Column(Text)          # filled when/if the post is later published
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_audit_ws_created", "workspace_id", "created_at"),
    )


class ManagedAccount(Base):
    """A managed client brand (Phase 7, Creators-side agency MVP). One owner can have
    many. Holds only brand IDENTITY — deliberately the SAME column names as User so the
    existing resolvers (resolve_user_profile / resolve_user_brand_voice /
    apply_user_slide_style) work on it via duck typing. Keys stay on the owning User."""
    __tablename__ = "managed_accounts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name = Column(String(120))
    brand_voice_preset = Column(String(30))
    brand_voice_custom = Column(Text)
    niche = Column(String(120))
    target_audience = Column(String(120))
    brand_name = Column(String(120))
    slide_accent_color = Column(String(7))
    slide_text_box_color = Column(String(7))
    logo_path = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
