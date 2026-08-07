"""The Business screens, driven by a real browser.

Business is the half of the product a person is meant to *review* rather than
compose: leads arrive on their own, a draft is written from one, and a human
approves it before anything goes out. The approval gate is the whole point, so
the tests that matter most here are the ones about what is and isn't clickable
— a disabled Approve on a draft that breaks a brand rule is a product promise,
not a style choice.

Where a screen can be exercised for real, it is: brand rules and publishing
limits round-trip through the API and the database, and a source is genuinely
added — pointed at the test server's own `/terms` page, so the hourly poller
has something to fetch without this suite reaching the internet. Leads and
drafts are the exception: nothing can produce either without a model, so their
two list endpoints are faked. `LeadOut` fakes are built from the server's
schema; the drafts endpoint returns a hand-assembled dict with no model of its
own, so that one fake mirrors the route by hand and is the most likely thing
here to drift.
"""
import json
from datetime import datetime, timezone

import pytest
from playwright.sync_api import expect

from models.schemas import LeadOut

from tests.e2e.nav import open_create, open_rules, open_section, open_settings

pytestmark = pytest.mark.e2e


def _lead(**over) -> dict:
    fields = dict(
        id="lead-1",
        what_happened="v4.2 ships incremental builds",
        source_url="https://example.com/releases/4.2",
        quote="Builds are now 3× faster on large repos.",
        published_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        why_interesting="A measurable speedup users asked for",
        strength="worthy",
        reason="Named version, concrete number",
        sensitive=False,
        status="new",
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    fields.update(over)
    return LeadOut(**fields).model_dump(mode="json")


def _draft(**over) -> dict:
    """Mirrors the dict `GET /api/business/drafts` assembles by hand."""
    fields = dict(
        id="draft-1", topic="v4.2 ships incremental builds",
        hook="Your CI just got its afternoon back.",
        caption="v4.2 makes builds incremental. Large repos see 3× faster runs.",
        thread_parts=[], hashtags=["#devtools"], source_kind="lead",
        source_url="https://example.com/releases/4.2",
        platform="instagram", status="draft", schedule_error=None,
        checked_claims=[], brand_flags={},
        created_at="2026-07-20T00:00:00+00:00",
    )
    fields.update(over)
    return fields


def _serve(page, pattern: str, payload):
    page.route(pattern, lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(payload)))


# ── The Business shell ───────────────────────────────────────────────────────

def test_the_navigation_is_four_buttons(page, signed_in):
    """The point of phase 3, stated as a test. Fourteen top-level buttons became
    four; everything else is a tab, a mode or a view inside one of them. A
    fifth button appearing here is the regression this guards."""
    signed_in()
    for section in ("create", "calendar", "queue", "results"):
        expect(page.locator(f'[data-section="{section}"]')).to_be_visible()
    assert page.locator("#section-nav .sec-btn:visible").count() == 4


def test_a_business_account_gets_the_same_shell(page, signed_in):
    """One engine, two products — but no longer two navigations. Business gets
    the same buttons, minus the Calendar it has no use for; what differs is
    what is inside them. Sources and Rules are Settings tabs now, so their old
    top-level buttons must not exist for anybody."""
    signed_in(account_type="business")
    for section in ("create", "queue", "results"):
        expect(page.locator(f'[data-section="{section}"]')).to_be_visible()
    expect(page.locator('[data-section="calendar"]')).to_be_hidden()
    for gone in ("biz-sources", "biz-leads", "biz-drafts", "biz-rules",
                 "biz-analytics", "biz-journal", "feed", "analytics"):
        expect(page.locator(f'[data-section="{gone}"]')).to_have_count(0)


def test_create_opens_on_leads_for_a_business_account(page, signed_in):
    """Leads moved into Create in 3.8, and for Business it IS Create: a post
    starts from a lead, never from a blank topic box. Landing a Business
    account on the creator wizard offers a screen whose Generate button their
    product does not have."""
    signed_in(account_type="business")
    _serve(page, "**/api/business/leads*", [_lead()])
    open_section(page, "create")
    expect(page.locator("#create-leads-panel")).to_be_visible()
    expect(page.locator("#create-post-panel")).to_be_hidden()
    expect(page.locator("#biz-leads-list")).to_contain_text("v4.2 ships")


def test_a_creator_is_not_offered_the_leads_mode(page, signed_in):
    signed_in()
    open_section(page, "create")
    expect(page.locator('#create-modes [data-create-mode="leads"]')).to_be_hidden()
    expect(page.locator("#create-post-panel")).to_be_visible()


def test_a_creator_asking_for_the_leads_mode_gets_the_wizard(page, signed_in):
    """Same shape as the Results tab guard: hiding a mode button is not a guard,
    because setCreateMode is reachable from anywhere and S.createMode outlives
    an account switch. A creator must land on the wizard, not on an empty
    Business panel with no mode button to leave by."""
    signed_in()
    open_section(page, "create")
    page.evaluate("setCreateMode('leads')")
    expect(page.locator("#create-post-panel")).to_be_visible()
    expect(page.locator("#create-leads-panel")).to_be_hidden()


def test_an_agency_account_gets_the_creator_shell(page, signed_in):
    """account_type has accepted "agency" since UX phase 2.1, and the SPA maps it
    to the creator shell on purpose — its own navigation arrives with the Team
    screen. Nothing tested that, in any browser test, until 3.0.

    It matters because of how the gating CSS is built: there is exactly one rule,
    `body[data-account-type="business"] .creator-only { display:none }`. Creator
    is the DEFAULT, not a positive match. The moment renderUserChrome starts
    emitting a third value, the natural "symmetric" fix is one keystroke away
    from hiding the entire application for a whole class of account. This test
    passes today and costs nothing; it exists to fail on that day.
    """
    signed_in(account_type="agency")
    expect(page.locator('[data-section="create"]')).to_be_visible()
    expect(page.locator("#create-post-panel")).to_be_visible()
    expect(page.locator('[data-section="calendar"]')).to_be_visible()


# ── Sources ──────────────────────────────────────────────────────────────────

def test_a_url_without_a_scheme_never_reaches_the_server(page, signed_in):
    """The server validates this too. Sending it anyway costs a round-trip to
    be told what the browser already knew, and the field is the obvious place
    for the answer to appear."""
    signed_in(account_type="business")
    calls = []
    page.on("request",
            lambda r: calls.append(r.url) if r.method == "POST" and "sources" in r.url else None)

    open_settings(page, "sources")
    page.locator("#biz-source-url").fill("example.com/feed")
    page.locator("#biz-source-add").click()

    expect(page.locator("#biz-source-status")).to_contain_text("public http(s) URL")
    assert calls == []


def test_an_added_source_is_listed(page, signed_in, live_server):
    """A real add: the row lands in the database and the list re-reads it.

    The URL points at the test server's own /terms page — a source the poller
    can actually fetch without this suite touching the internet.
    """
    signed_in(account_type="business")
    open_settings(page, "sources")
    expect(page.locator("#biz-sources-list")).to_contain_text("No sources yet")

    page.locator("#biz-source-url").fill(f"{live_server}/terms")
    page.locator("#biz-source-add").click()

    expect(page.locator("#biz-source-status")).to_contain_text("Added.")
    expect(page.locator("#biz-sources-list")).to_contain_text(f"{live_server}/terms")
    expect(page.locator("#biz-source-url")).to_have_value("")   # cleared for the next one


def test_a_deleted_source_leaves_the_list(page, signed_in, live_server):
    signed_in(account_type="business")
    open_settings(page, "sources")
    page.locator("#biz-source-url").fill(f"{live_server}/terms")
    page.locator("#biz-source-add").click()
    expect(page.locator("#biz-sources-list")).to_contain_text("/terms")

    page.on("dialog", lambda d: d.accept())
    page.locator('#biz-sources-list [data-act="delete"]').click()
    expect(page.locator("#biz-sources-list")).to_contain_text("No sources yet")


# ── Leads ────────────────────────────────────────────────────────────────────

def test_a_lead_shows_its_strength_and_its_source(page, signed_in):
    signed_in(account_type="business")
    _serve(page, "**/api/business/leads*", [_lead()])
    open_create(page, "leads")

    row = page.locator("#biz-leads-list .ce-card").first
    expect(row).to_contain_text("v4.2 ships incremental builds")
    expect(row).to_contain_text("Named version, concrete number")
    expect(row.locator("a")).to_have_attribute(
        "href", "https://example.com/releases/4.2")


def test_a_sensitive_lead_is_flagged_before_it_is_drafted(page, signed_in):
    """Bad news drafted in a cheerful brand voice is the failure mode this
    product has to avoid; the warning is the only thing standing in front of
    it, and it has to be on the row, not in a tooltip."""
    signed_in(account_type="business")
    _serve(page, "**/api/business/leads*", [_lead(sensitive=True)])
    open_create(page, "leads")
    expect(page.locator("#biz-leads-list")).to_contain_text("Sensitive")


def test_the_digest_button_appears_only_once_two_leads_are_picked(page, signed_in):
    """A digest of one is just a post, and the endpoint rejects it — better to
    not offer the button than to explain the rejection afterwards."""
    signed_in(account_type="business")
    _serve(page, "**/api/business/leads*",
           [_lead(id="lead-1"), _lead(id="lead-2", what_happened="v4.3 adds caching")])
    open_create(page, "leads")

    checks = page.locator(".biz-lead-check")
    expect(page.locator("#biz-digest-btn")).to_be_hidden()
    checks.nth(0).check()
    expect(page.locator("#biz-digest-btn")).to_be_hidden()
    checks.nth(1).check()
    expect(page.locator("#biz-digest-btn")).to_be_visible()
    expect(page.locator("#biz-digest-btn")).to_contain_text("2 selected")

    checks.nth(1).uncheck()
    expect(page.locator("#biz-digest-btn")).to_be_hidden()


def test_choosing_x_reveals_the_post_shape_and_instagram_hides_it(page, signed_in):
    signed_in(account_type="business")
    _serve(page, "**/api/business/leads*", [_lead()])
    open_create(page, "leads")

    row = page.locator("#biz-leads-list .ce-card").first
    expect(row.locator('[data-role="xmode"]')).to_be_hidden()
    row.locator('[data-role="platform"]').select_option("x")
    expect(row.locator('[data-role="xmode"]')).to_be_visible()
    row.locator('[data-role="platform"]').select_option("instagram")
    expect(row.locator('[data-role="xmode"]')).to_be_hidden()


# ── Drafts and the approval gate ─────────────────────────────────────────────

def test_a_fresh_draft_can_only_be_submitted(page, signed_in):
    signed_in(account_type="business")
    _serve(page, "**/api/business/drafts", [_draft()])
    open_section(page, "queue")

    row = page.locator("#biz-drafts-list .ce-card").first
    expect(row).to_contain_text("Draft")
    expect(row.locator('[data-act="submit"]')).to_be_visible()
    expect(row.locator('[data-act="approve"]')).to_have_count(0)
    expect(row.locator('[data-role="caption"]')).to_have_value(
        "v4.2 makes builds incremental. Large repos see 3× faster runs.")


def test_a_draft_in_review_offers_approve_and_reject(page, signed_in):
    signed_in(account_type="business")
    _serve(page, "**/api/business/drafts", [_draft(status="in_review")])
    open_section(page, "queue")

    row = page.locator("#biz-drafts-list .ce-card").first
    expect(row).to_contain_text("In review")
    expect(row.locator('[data-act="approve"]')).to_be_enabled()
    expect(row.locator('[data-act="reject"]')).to_be_visible()


def test_a_brand_rule_breach_blocks_approval(page, signed_in):
    """Forbidden phrases block approval — that is what the rules screen
    promises. A clickable Approve here would make the rules decorative."""
    signed_in(account_type="business")
    _serve(page, "**/api/business/drafts", [_draft(
        status="in_review",
        caption="Guaranteed returns on every build.",
        brand_flags={"forbidden": ["guaranteed returns"], "missing_disclaimers": []},
    )])
    open_section(page, "queue")

    row = page.locator("#biz-drafts-list .ce-card").first
    expect(row.locator('[data-act="approve"]')).to_be_disabled()
    expect(row).to_contain_text("Verify before approving")
    expect(row).to_contain_text("guaranteed returns")


def test_a_missing_disclaimer_blocks_approval_too(page, signed_in):
    signed_in(account_type="business")
    _serve(page, "**/api/business/drafts", [_draft(
        status="in_review",
        brand_flags={"forbidden": [], "missing_disclaimers": ["Not financial advice."]},
    )])
    open_section(page, "queue")
    expect(page.locator('#biz-drafts-list [data-act="approve"]')).to_be_disabled()


def test_a_published_draft_is_read_only_and_says_so(page, signed_in):
    """Every status that wasn't in the badge map fell through to a grey
    "Draft" — the most misleading label available for a post that already went
    out, and for one that failed on the way."""
    signed_in(account_type="business")
    _serve(page, "**/api/business/drafts", [_draft(status="published")])
    open_section(page, "queue")

    row = page.locator("#biz-drafts-list .ce-card").first
    expect(row).to_contain_text("Published")
    expect(row.locator('[data-role="caption"]')).to_have_count(0)   # not editable
    expect(row.locator('[data-act="save"]')).to_have_count(0)


def test_a_failed_publish_shows_the_reason_on_the_draft(page, signed_in):
    signed_in(account_type="business")
    _serve(page, "**/api/business/drafts",
           [_draft(status="failed", schedule_error="Instagram rejected the media.")])
    open_section(page, "queue")

    row = page.locator("#biz-drafts-list .ce-card").first
    expect(row).to_contain_text("Failed")
    expect(row).to_contain_text("Instagram rejected the media.")


# ── Brand rules and publishing limits (real round-trips) ─────────────────────

def test_brand_rules_survive_leaving_the_screen(page, signed_in):
    signed_in(account_type="business")
    open_rules(page)
    page.locator("#rules-forbidden").fill("guaranteed returns\nbest in the world")
    page.locator("#rules-disclaimers").fill("Not financial advice.")
    page.get_by_role("button", name="Save rules").click()
    expect(page.locator("#rules-status")).to_contain_text("Saved.")

    open_settings(page, "sources")
    open_rules(page)
    expect(page.locator("#rules-forbidden")).to_have_value(
        "guaranteed returns\nbest in the world")
    expect(page.locator("#rules-disclaimers")).to_have_value("Not financial advice.")


def test_blank_lines_in_the_rules_are_not_saved_as_rules(page, signed_in):
    """An empty forbidden phrase matches every caption, so a stray blank line
    would block every approval in the workspace."""
    signed_in(account_type="business")
    open_rules(page)
    page.locator("#rules-forbidden").fill("guaranteed returns\n\n   \nbest in the world")
    page.get_by_role("button", name="Save rules").click()
    expect(page.locator("#rules-status")).to_contain_text("Saved.")

    open_settings(page, "sources")
    open_rules(page)
    expect(page.locator("#rules-forbidden")).to_have_value(
        "guaranteed returns\nbest in the world")


def test_publishing_limits_persist(page, signed_in):
    signed_in(account_type="business")
    open_rules(page)
    page.locator("#limit-day").fill("2")
    page.locator("#limit-week").fill("10")
    page.get_by_role("button", name="Save limits").click()
    expect(page.locator("#limits-status")).to_contain_text("Saved.")

    open_settings(page, "sources")
    open_rules(page)
    expect(page.locator("#limit-day")).to_have_value("2")
    expect(page.locator("#limit-week")).to_have_value("10")


def test_a_limit_out_of_range_is_reported_not_swallowed(page, signed_in):
    """The server caps these at 100. Without the message the save looks like it
    worked and the cap silently isn't there."""
    signed_in(account_type="business")
    open_rules(page)
    page.locator("#limit-day").fill("999")
    page.get_by_role("button", name="Save limits").click()
    expect(page.locator("#limits-status")).to_contain_text("must be 1–100")
