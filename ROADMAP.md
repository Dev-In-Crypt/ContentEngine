# Content Engine — Roadmap

> Single living source for where the product is and where it's going.
> Last updated: **2026-07-26** · Baseline commit: `de536dd`

**Vision.** A multi-tenant, bring-your-own-keys SaaS that lets creators and companies
generate and publish social content (Instagram + X today, more later) under their own
accounts and their own API keys — no per-post cost to us, no Meta App Review to block us.

Two products share one engine, split only by `account_type`:
- **Creators** — compose posts/carousels/reels from a topic, brand them, publish.
- **Business** — watch company sources → leads → verified drafts → approval → publish.

---

## How to read this

**Priority** — `P0` do next / unblocks revenue or launch · `P1` important · `P2` nice-to-have.
**Effort** — `S` ≤1 session · `M` a few sessions · `L` multi-day.
**Status** — `shipped` · `wip` · `next` · `deferred` · `idea`.
**Gate** — who unblocks it:
- 🟢 **Eng** — we can just build it.
- 🟡 **Owner** — blocked on you providing a key/cred/credential or a product decision.
- 🔴 **External** — blocked on a third party (Meta App Review, a paid API tier, a lawyer).

> The Gate column is the most important one here: a large share of "remaining" work is
> **not** engineering — it waits on an owner action or a paid third-party tier. Don't read
> a long list as "half the product is unbuilt"; read the 🟡/🔴 rows as "waiting on a decision."

---

## Current baseline — what's already shipped & live

Running on Hetzner (`https://167.233.156.202.sslip.io`), 882 tests green, ruff clean.

- **Platform/tenancy** — argon2 + JWT auth, Fernet per-user key vault, `user_id` data
  isolation, email verify/reset flow (code-ready), JWT revocation (`token_version`),
  rate limits, non-root container, Alembic auto-migrate, daily pg + uploads backups,
  orphan-uploads cleanup, CI, Dependabot, ToS/Privacy templates, self-host compose stack.
- **AI** — multi-provider (OpenRouter + native OpenAI/Anthropic/Google), per-user model
  choice, usage/cost metering (`LLMUsage`).
- **Creators** — single/carousel/infographic composer, AI/stock/upload/Canva images,
  brand engine (logo, voice, profile, pillars), per-slide regen/upload/overlay editing,
  presets, plan-a-week, ZIP export, own-photo reorder, plain-photo & text-only styles.
- **Reels** — ElevenLabs voiceover + burned subtitles (R1), Pexels b-roll + AI judge (R2),
  music + ducking + cover frame + xfade (R3).
- **Publishing** — **X live-confirmed** (OAuth 1.0a, threads, long-form gate, text-only,
  hashtag-once); **Instagram code-complete but not yet live-verified**; scheduler with
  idempotency + startup reconcile; read-only "Test connection" preflight.
- **Business** — sources (github_releases/rss/generic) → **rules-only** poller → leads
  (worthy/weak, sensitive/bad-news flag) → draft/digest → claim-check + brand rules →
  approval workflow + audit journal → frequency caps → source-analytics funnel (with
  engagement) → managed accounts (agency MVP).

---

## Horizons at a glance

**Now (in flight / do next)** — IG live-publish verification · Resend email domain ·
finalize legal · Sentry on. *All four are owner/external gates; no engineering blocker remains.*
**Next** — LinkedIn publisher · X insights · billing decision · agency roles.
**Later** — more networks · CDN/media hosting · scale/perf · deeper analytics.
**Someday** — OAuth account connect · white-label · mobile app.

---

## A. Publishing & networks

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **Instagram live-publish verification** | P0 | S | next | 🟡 Owner | Code done (Graph v25 + imgbb). Needs a real IG Business token + user id + imgbb key, then one live post to confirm the happy path. |
| **X insights** | P1 | M | deferred | 🟢/🔴 | X has no `get_insights` today; needs the X metrics endpoints (paid tier dependent). Panel is hidden until then. |
| **LinkedIn publisher** | P1 | L | deferred | 🟢/🔴 | Generates but can't publish (`factory.py` only wires instagram+x). Needs `linkedin.py` adapter + creds + rail enable. |
| **Scheduler hardening** | P1 | M | shipped | 🟢 | Transient-only retry with 5/15/60-min backoff + email once the attempts are spent; a failed publish is visible in-app. Per-network quota awareness (X 1500/mo) still open. |
| **Credential health + token expiry** | P1 | S | shipped | 🟢 | Daily `verify_credentials()` sweep, email on the working→broken transition, Instagram expiry estimate + "Renew". Meta has no read-only introspection, so the date is an estimate unless renewed. |
| **TikTok / Facebook / YouTube** | P2 | L | idea | 🔴 | Currently "coming soon" rail buttons, not in the `Platform` enum. Each needs an API + review. |
| **Cross-posting** | P2 | M | idea | 🟢 | One draft → adapt & publish to multiple networks in one action. |

## B. Onboarding & auth

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **Real email delivery** | P0 | S | next | 🟡 Owner | `RESEND_API_KEY` empty → emails are log-only, `require_verified_email` stays off. Needs a verified Resend sending domain. |
| **Turn on email verification** | P1 | S | next | 🟡 Owner | Flip `require_verified_email=true` once email delivery is real. |
| **OAuth "Connect account" flow** | P2 | L | idea | 🔴 | Alternative to raw keys (much easier onboarding) but triggers Meta App Review — deliberately deferred. |
| **Onboarding wizard** | P2 | M | shipped | 🟢 | Stepped first-run per product (brand → AI key → publishing → done; Business: AI key → first source → done), each step tested against the live API before advancing. Re-openable from Connections. |

## C. Monetization

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **Billing model decision** | P1 | S | idea | 🟡 Owner | No billing today (free BYO-keys). Decide: stay free, flat sub, or metered. Blocks everything below. |
| **Plans & subscription (Stripe)** | P2 | L | idea | 🟡 Owner | Only after the model decision. |
| **Usage quotas / free-tier limits** | P2 | M | idea | 🟢 | `LLMUsage` metering already exists — enforce caps per plan. |

## D. Multi-tenant & agency

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **Workspace roles & invites** | P1 | L | deferred | 🟢 | Today the workspace owner is sole author+approver. Add `WorkspaceMember` + roles (author/approver/viewer) + invites. |
| **Per-managed-account social/AI keys** | P2 | M | deferred | 🟢 | Agency MVP uses the agency's own keys for every managed brand; split per-account later. |
| **Per-account presets / x_premium** | P2 | S | deferred | 🟢 | Managed-account overrides. |

## E. Reliability & ops

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **Turn on Sentry** | P0 | S | next | 🟡 Owner | Init is gated in lifespan; `SENTRY_DSN` is empty. Provide a DSN. |
| **Uptime + alerting** | P1 | S | idea | 🟡 Owner | `/health` exists; wire UptimeRobot/alerts. |
| **Staging environment** | P1 | M | idea | 🟢 | A non-prod box to smoke deploys before prod. |
| **Restore drills / DR runbook** | P1 | S | idea | 🟢 | Backups exist; document + rehearse restore. |
| **Off-box backup copy** | P1 | S | idea | 🟡 Owner | Ship daily dumps to object storage, not just the VPS volume. |

## F. Security & compliance

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **Finalize ToS / Privacy** | P0 | S | next | 🔴 Lawyer | Templates served at `/terms` `/privacy`; need real legal review before public launch. |
| **GDPR: data export & delete** | P1 | M | idea | 🟢 | Account deletion + "download my data" for a public SaaS holding others' tokens. |
| **Secret-custody review** | P1 | S | idea | 🟢 | We hold others' IG tokens + paid X keys — audit encryption, logging, access. |
| **Pen-test / dependency audit** | P2 | M | idea | 🟡 Owner | Before a wide launch. |
| **Abuse / anti-spam controls** | P2 | M | idea | 🟢 | Publish-rate abuse, content limits, per-tenant throttles. |

## G. Content quality & accuracy

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **Creator-side accuracy gate** | P1 | M | idea | 🟢 | Claim-check runs only for Business; creator posts ship AI figures unverified. Offer an opt-in check. |
| **Content-safety filter** | P2 | M | idea | 🟢 | Guard against unsafe/off-brand output before publish. |
| **Brand-rules expansion** | P2 | S | idea | 🟢 | More deterministic rules (banned claims, mandatory disclaimers). |

## H. Analytics & insights

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **X insights** | P1 | M | deferred | 🔴 | See A — needs X metrics API. Lights up the X analytics panel + Business engagement for X. |
| **Cross-network dashboard** | P2 | M | idea | 🟢 | Unified metrics across IG + X once both report. |
| **Scheduled reports / CSV export** | P2 | S | idea | 🟢 | Business funnel export exists; extend to metrics + email digests. |
| **Time-windowed analytics** | P2 | S | idea | 🟢 | Source-analytics is all-time; add 7/30/90-day windows. |

## I. Content capabilities

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **Real Pexels b-roll for reels** | P1 | S | next | 🟡 Owner | Reels b-roll needs a real Pexels key; falls back to Ken Burns without it. |
| **More post formats / templates** | P2 | M | idea | 🟢 | Story format, quote cards, more carousel layouts. |
| **AI model catalog expansion** | P2 | S | idea | 🟢 | Keep provider/model catalog current. |
| **Content calendar / bulk plan** | P2 | M | idea | 🟢 | Plan-a-week exists; extend to a full editable calendar of scheduled posts. |

## J. UX & product polish

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **i18n / finish RU/EN** | P1 | M | idea | 🟢 | Leftover hard-coded strings; decide on a proper i18n layer vs English-only. |
| **Accessibility pass (full)** | P2 | M | idea | 🟢 | Modal focus-trap/aria shipped; do a complete a11y sweep. |
| **Mobile polish** | P2 | S | idea | 🟢 | Header/banner wrap fixed; continue per-screen mobile review. |
| **Empty/loading/error states** | P2 | S | idea | 🟢 | Consistent coverage across all screens. |

## K. Desktop app

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **Feature parity (local mode)** | P2 | S | ongoing | 🟢 | `.pyw`/`.exe` local mode (no login, `.env` keys). Keep it in step; Business module stays hidden offline. |
| **Signed builds / auto-update** | P2 | M | idea | 🟡 Owner | Code-signing cert + an update channel. |

## L. Scale & performance

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **Media hosting / CDN** | P1 | M | idea | 🟡 Owner | Slide images live on the VPS disk; move to object storage + CDN as tenants grow. |
| **Background worker / queue** | P2 | M | idea | 🟢 | Publishing/reel-render is inline; a queue decouples long jobs from requests. |
| **DB scaling & indexing review** | P2 | S | idea | 🟢 | Index/query review as data grows. |
| **Caching** | P2 | S | idea | 🟢 | Cache hot reads (catalogs, settings). |

## M. Dev process & quality

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **Frontend test harness** | P1 | M | idea | 🟢 | The SPA has no JS test harness — verified manually in-browser. Add one (Playwright/vitest). |
| **Feature flags** | P2 | S | idea | 🟢 | Ship risky features behind flags. |
| **Coverage / mutation gates in CI** | P2 | S | idea | 🟢 | Enforce the TDD + mutation discipline in CI, not just locally. |

## N. Documentation

| Item | P | Effort | Status | Gate | Notes |
|---|---|---|---|---|---|
| **User docs / help** | P2 | M | idea | 🟢 | End-user guide (keys, publishing, Business flow) beyond the dev-facing `*.md`. |
| **Public API docs** | P2 | S | idea | 🟢 | `/docs` is cloud-gated; decide on a public/partner API story. |

---

## Owner-gated summary (what's waiting on you)

These unblock the most with the least engineering:

1. **Instagram creds** (Business token + user id + imgbb) → verify IG live-publish. *(P0)*
2. **Resend sending domain** → real email + turn on verification. *(P0)*
3. **Legal review** of ToS/Privacy → public-launch gate. *(P0, external)*
4. **`SENTRY_DSN`** → error visibility in prod. *(P0)*
5. **Pexels key** → real reel b-roll. *(P1)*
6. **Billing decision** (free / sub / metered) → unblocks the whole monetization column. *(P1)*
7. **Off-box backup + custom domain + uptime alerts** → operational maturity. *(P1)*

---

## Non-goals (deliberately out of scope for now)

- OAuth-based account connection (keeps us out of Meta App Review — raw keys instead).
- Owning image hosting (imgbb / user keys for now).
- A billing system before the model is decided.
- Trend Finder & Hashtag Intelligence (removed in Part XIX — isolation/XSS risk).

---

## Risks & assumptions

- **We custody others' credentials** — IG tokens + paid X keys. Encryption + no-logging is
  a hard requirement, not a nice-to-have.
- **X economics are per-user** — each tenant needs their own paid X tier; we don't front it.
- **Raw-key model is a UX tax** — easy for us (no App Review), heavy for non-technical users.
- **Single VPS** — fine now; media-on-disk and inline jobs are the first things to bite at scale.

---

## Success metrics (to define)

Suggested starting KPIs once public: activated tenants (added ≥1 key), first-post
conversion, posts published/week, publish success rate, weekly retention, infra cost/tenant.

---

*Decision log & detailed phase history live in the project memory and the
`content-engine-business-implementation.md` / `instagram-content-engine-spec.md` specs.
This roadmap is the forward-looking view; those are the record of what shipped.*
