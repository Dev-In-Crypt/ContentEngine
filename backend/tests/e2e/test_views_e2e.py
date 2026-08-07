"""Calendar, the feed grid, Results and the Journal, in a real browser.

These four screens had no browser coverage of any kind. They are also the ones
UX phase 3 moves, merges and re-parents — so without this file the phase could
break them and the suite would stay green. That is the whole reason it exists.

Posts are served from a stubbed /api/posts so a test can state exactly which
networks and statuses are on screen; the fakes are built from PostSummary, so a
schema change breaks them here rather than silently in the product.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from models.schemas import PostSummary
from tests.e2e.nav import open_calendar, open_results, open_section

pytestmark = pytest.mark.e2e


def _post(**over):
    """One row of /api/posts, valid by construction."""
    fields = dict(
        id="p1", topic="A topic", format="single", status="draft",
        platform="instagram", created_at=datetime.now(timezone.utc),
    )
    fields.update(over)
    return PostSummary(**fields).model_dump(mode="json")


def _serve_posts(page, *posts):
    page.route("**/api/posts*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(list(posts))))


def _on(day_offset=1, **over):
    when = (datetime.now(timezone.utc) + timedelta(days=day_offset)).isoformat()
    return _post(scheduled_at=when, status="scheduled", **over)


# ------------------------------------------------------------------ calendar

def test_the_calendar_places_a_scheduled_post_on_a_day(page, signed_in):
    signed_in()
    _serve_posts(page, _on(id="c1", topic="Sourdough day"))
    open_section(page, "calendar")
    expect(page.locator("#cal-grid")).to_contain_text("Sourdough")


def test_the_calendar_shows_every_network_at_once(page, signed_in):
    """Mutation guard for the filter this phase removed. It read `p.platform`,
    which /api/posts never sent, so on X it matched nothing and the calendar was
    empty. Put any network filter back and one of these two disappears."""
    signed_in()
    _serve_posts(page,
                 _on(id="c1", topic="Instagram one", platform="instagram"),
                 _on(id="c2", topic="X one", platform="x"))
    open_section(page, "calendar")
    expect(page.locator("#cal-grid")).to_contain_text("Instagram")
    expect(page.locator("#cal-grid")).to_contain_text("X one")


def test_a_calendar_entry_says_which_network_it_is_for(page, signed_in):
    signed_in()
    _serve_posts(page, _on(id="c2", topic="X one", platform="x"))
    open_section(page, "calendar")
    expect(page.locator('#cal-grid [title="X"]')).to_be_visible()


def test_clicking_a_calendar_entry_opens_the_post(page, signed_in):
    signed_in()
    _serve_posts(page, _on(id="c1", topic="Sourdough day"))
    page.route("**/api/posts/c1", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "id": "c1", "topic": "Sourdough day", "format": "single",
            "status": "scheduled", "platform": "instagram", "slides": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        })))
    open_section(page, "calendar")
    page.locator("#cal-grid").get_by_text("Sourdough").click()
    expect(page.locator("#view-create")).to_be_visible()


# ------------------------------------- the profile grid, a view of the calendar

def test_the_feed_grid_renders_a_card(page, signed_in):
    signed_in()
    _serve_posts(page, _post(id="g1", topic="Grid post", status="published"))
    open_calendar(page, "profile")
    expect(page.locator("#grid-container")).to_contain_text("Grid post")
    expect(page.locator("#grid-empty")).to_be_hidden()


def test_the_feed_grid_stays_instagram_only(page, signed_in):
    """Deliberately NOT "show everything": this screen simulates an Instagram
    profile page, so a tweet in it would be wrong rather than unfiltered. Drop
    the filter and the X post appears."""
    signed_in()
    _serve_posts(page,
                 _post(id="g1", topic="Gram post", status="published"),
                 _post(id="g2", topic="Tweet post", status="published", platform="x"))
    open_calendar(page, "profile")
    expect(page.locator("#grid-container")).to_contain_text("Gram post")
    expect(page.locator("#grid-container")).not_to_contain_text("Tweet post")


def test_an_empty_feed_grid_says_so(page, signed_in):
    signed_in()
    _serve_posts(page)
    open_calendar(page, "profile")
    expect(page.locator("#grid-empty")).to_be_visible()


def test_the_profile_grid_and_the_calendar_are_not_both_on_screen(page, signed_in):
    """Two views of the same posts, one at a time. They live in one section now,
    so nothing but the mode switch keeps them apart — and a panel left visible
    under the other mode is the exact defect this file exists to catch."""
    signed_in()
    _serve_posts(page, _post(id="g1", topic="Grid post", status="published"))
    open_calendar(page, "profile")
    expect(page.locator("#calendar-panel")).to_be_hidden()
    open_calendar(page, "calendar")
    expect(page.locator("#profile-panel")).to_be_hidden()
    expect(page.locator("#cal-grid")).to_be_visible()


# ------------------------------------------------------------------ results

def test_results_opens_on_published_posts(page, signed_in):
    """Whatever else Results grows, the thing a person came for is their posts."""
    signed_in()
    _serve_posts(page, _post(id="r1", topic="Shipped it", status="published"))
    open_section(page, "results")
    expect(page.locator("#results-posts")).to_be_visible()
    expect(page.locator("#analytics-list")).to_contain_text("Shipped it")


def test_a_creator_is_not_offered_the_business_result_tabs(page, signed_in):
    signed_in()
    _serve_posts(page)
    open_section(page, "results")
    expect(page.locator('#results-tabs [data-results-tab="posts"]')).to_be_visible()
    for tab in ("sources", "journal"):
        expect(page.locator(f'#results-tabs [data-results-tab="{tab}"]')).to_be_hidden()


def test_a_creator_asking_for_a_business_tab_gets_their_posts(page, signed_in):
    """The tab strip hides what a creator may not have, but hiding a button is
    not a guard — `openResults` is reachable from anywhere, and a stale
    S.resultsTab survives an account switch. Ask for the Journal as a creator
    and Results must fall back to Posts rather than render an empty Business
    screen with no way back to the tab strip."""
    signed_in()
    _serve_posts(page)
    open_section(page, "results")
    page.evaluate("openResults('journal')")
    expect(page.locator("#results-posts")).to_be_visible()
    expect(page.locator("#results-journal")).to_be_hidden()


def test_results_fetches_only_the_tab_that_is_open(page, signed_in):
    """Three panels, one screen — but a screen that loads all three on entry
    spends three round-trips to show one."""
    signed_in(account_type="business")
    _serve_posts(page)
    calls = []
    page.on("request", lambda r: calls.append(r.url))
    open_results(page, "journal")
    expect(page.locator("#biz-journal-list")).not_to_contain_text("Loading")
    assert not [u for u in calls if "source-analytics" in u], calls


def test_a_business_account_gets_all_three_result_tabs(page, signed_in):
    signed_in(account_type="business")
    _serve_posts(page)
    open_section(page, "results")
    for tab in ("posts", "sources", "journal"):
        expect(page.locator(f'#results-tabs [data-results-tab="{tab}"]')).to_be_visible()


def test_source_analytics_is_a_tab_of_results(page, signed_in):
    signed_in(account_type="business")
    page.route("**/api/business/source-analytics*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "totals": {"leads": 3, "worthy": 2, "drafts": 1, "approved": 1},
            "sources": [{"id": "s1", "url": "https://example.com/feed", "kind": "feed",
                         "leads": 3, "worthy": 2, "drafts": 1, "approved": 1}],
        })))
    open_results(page, "sources")
    expect(page.locator("#biz-analytics-list")).to_contain_text("example.com/feed")


def test_results_lists_a_published_post_with_its_link(page, signed_in):
    """The link is new: `published_url` was read here but never sent, so a
    published post never linked anywhere."""
    signed_in()
    _serve_posts(page, _post(
        id="r1", topic="Shipped it", status="published",
        published_at=datetime.now(timezone.utc), published_url="https://example.com/p/1"))
    open_results(page)
    expect(page.locator("#analytics-list")).to_contain_text("Shipped it")
    expect(page.locator("#analytics-list a")).to_have_attribute(
        "href", "https://example.com/p/1")


def test_results_shows_both_networks(page, signed_in):
    signed_in()
    _serve_posts(page,
                 _post(id="r1", topic="Gram result", status="published"),
                 _post(id="r2", topic="X result", status="published", platform="x"))
    open_results(page)
    expect(page.locator("#analytics-list")).to_contain_text("Gram result")
    expect(page.locator("#analytics-list")).to_contain_text("X result")
    expect(page.locator('#analytics-list [title="X"]')).to_be_visible()


def test_results_leaves_out_what_is_not_published(page, signed_in):
    signed_in()
    _serve_posts(page,
                 _post(id="r1", topic="Shipped it", status="published"),
                 _post(id="r2", topic="Still a draft", status="draft"))
    open_results(page)
    expect(page.locator("#analytics-list")).to_contain_text("Shipped it")
    expect(page.locator("#analytics-list")).not_to_contain_text("Still a draft")


# ------------------------------------------------------------------ journal

def test_the_journal_renders_an_entry(page, signed_in):
    """A journal row is the approved copy itself, not a title — it exists to
    show what a human signed off on, and whether they changed it first."""
    signed_in(account_type="business")
    page.route("**/api/business/journal*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps([{
            "id": "a1",
            "ai_draft": "What the model wrote.",
            "human_edits": "What the human approved.",
            "source_url": "https://example.com/news",
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }])))
    open_results(page, "journal")
    expect(page.locator("#biz-journal-list")).to_contain_text("What the human approved")
    expect(page.locator("#biz-journal-list")).to_contain_text("edited by a human")


def test_an_empty_journal_says_so(page, signed_in):
    signed_in(account_type="business")
    page.route("**/api/business/journal*", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    open_results(page, "journal")
    expect(page.locator("#biz-journal-list")).to_contain_text("No approvals")


def test_opening_a_post_from_the_calendar_returns_to_the_wizard(page, signed_in):
    """The likeliest regression in the Create merge, and the one worth its own
    test: openPost sends you to the Create section, but the section is a
    container of three panels now. Land there with Video showing and you get
    the right section and a hidden wizard — a click that appears to do
    nothing."""
    from tests.e2e.nav import open_create

    signed_in()
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.route("**/api/models/providers", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"text": [], "image": [], "video": []})))
    open_create(page, "video")

    _serve_posts(page, _on(id="c9", topic="From the calendar"))
    page.route("**/api/posts/c9", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "id": "c9", "topic": "From the calendar", "format": "single",
            "status": "scheduled", "platform": "instagram", "slides": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        })))
    open_section(page, "calendar")
    page.locator("#cal-grid").get_by_text("From the").click()

    expect(page.locator("#create-post-panel")).to_be_visible()
    expect(page.locator("#create-video-panel")).to_be_hidden()


# ------------------------------------------------------------------ queue
#
# One section, two contents. A creator's queue is their own unpublished work; a
# Business account's is the approval pipeline, which has buttons a creator has
# no use for. Merging the two lists would mean showing a creator approval
# controls that do nothing, or hiding from Business the thing the screen is for.

def test_the_queue_lists_unpublished_work(page, signed_in):
    signed_in()
    _serve_posts(page,
                 _post(id="q1", topic="A draft", status="draft"),
                 _on(id="q2", topic="Scheduled one"))
    open_section(page, "queue")
    expect(page.locator("#queue-list")).to_contain_text("A draft")
    expect(page.locator("#queue-list")).to_contain_text("Scheduled one")


def test_the_queue_leaves_out_what_is_already_published(page, signed_in):
    """Published work belongs to Results. A queue that keeps everything ever
    made stops being a queue on the day it matters."""
    signed_in()
    _serve_posts(page,
                 _post(id="q1", topic="A draft", status="draft"),
                 _post(id="q2", topic="Already out", status="published"))
    open_section(page, "queue")
    expect(page.locator("#queue-list")).to_contain_text("A draft")
    expect(page.locator("#queue-list")).not_to_contain_text("Already out")


def test_a_queue_row_says_which_network_it_is_for(page, signed_in):
    signed_in()
    _serve_posts(page, _post(id="q1", topic="A tweet draft", status="draft", platform="x"))
    open_section(page, "queue")
    expect(page.locator('#queue-list [title="X"]')).to_be_visible()


def test_an_empty_queue_says_so(page, signed_in):
    signed_in()
    _serve_posts(page)
    open_section(page, "queue")
    expect(page.locator("#queue-list")).to_contain_text("Nothing waiting")


def test_clicking_a_queue_row_opens_the_post(page, signed_in):
    signed_in()
    _serve_posts(page, _post(id="q1", topic="A draft", status="draft"))
    page.route("**/api/posts/q1", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "id": "q1", "topic": "A draft", "format": "single", "status": "draft",
            "platform": "instagram", "slides": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        })))
    open_section(page, "queue")
    page.locator("#queue-list").get_by_text("A draft").click()
    expect(page.locator("#create-post-panel")).to_be_visible()


def test_a_business_queue_is_the_approval_pipeline(page, signed_in):
    """Same section, different content — the drafts screen moved in here rather
    than being merged with the creator list."""
    signed_in(account_type="business")
    page.route("**/api/business/drafts", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps([{
            "id": "d1", "topic": "t", "hook": "A drafted lead", "caption": "c",
            "thread_parts": [], "hashtags": [], "source_kind": "lead",
            "source_url": "https://example.com/x", "platform": "instagram",
            "status": "draft", "schedule_error": None,
            "checked_claims": [], "brand_flags": {},
            "created_at": "2026-07-20T00:00:00+00:00",
        }])))
    _serve_posts(page, _post(id="q1", topic="A creator draft", status="draft"))
    open_section(page, "queue")
    # The approval pipeline, actually loaded — not merely an empty container
    # with the right id. Drop the branch and the creator list renders instead.
    expect(page.locator("#biz-drafts-list")).to_contain_text("A drafted lead")
    expect(page.locator("#queue-list")).to_be_hidden()
    expect(page.locator("#view-queue")).not_to_contain_text("A creator draft")
