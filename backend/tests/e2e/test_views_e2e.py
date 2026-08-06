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
from tests.e2e.nav import open_section

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
    page.route("**/api/posts", lambda r: r.fulfill(
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


# ------------------------------------------------------------------ feed grid

def test_the_feed_grid_renders_a_card(page, signed_in):
    signed_in()
    _serve_posts(page, _post(id="g1", topic="Grid post", status="published"))
    open_section(page, "feed")
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
    open_section(page, "feed")
    expect(page.locator("#grid-container")).to_contain_text("Gram post")
    expect(page.locator("#grid-container")).not_to_contain_text("Tweet post")


def test_an_empty_feed_grid_says_so(page, signed_in):
    signed_in()
    _serve_posts(page)
    open_section(page, "feed")
    expect(page.locator("#grid-empty")).to_be_visible()


# ------------------------------------------------------------------ results

def test_results_lists_a_published_post_with_its_link(page, signed_in):
    """The link is new: `published_url` was read here but never sent, so a
    published post never linked anywhere."""
    signed_in()
    _serve_posts(page, _post(
        id="r1", topic="Shipped it", status="published",
        published_at=datetime.now(timezone.utc), published_url="https://example.com/p/1"))
    open_section(page, "results")
    expect(page.locator("#analytics-list")).to_contain_text("Shipped it")
    expect(page.locator("#analytics-list a")).to_have_attribute(
        "href", "https://example.com/p/1")


def test_results_shows_both_networks(page, signed_in):
    signed_in()
    _serve_posts(page,
                 _post(id="r1", topic="Gram result", status="published"),
                 _post(id="r2", topic="X result", status="published", platform="x"))
    open_section(page, "results")
    expect(page.locator("#analytics-list")).to_contain_text("Gram result")
    expect(page.locator("#analytics-list")).to_contain_text("X result")
    expect(page.locator('#analytics-list [title="X"]')).to_be_visible()


def test_results_leaves_out_what_is_not_published(page, signed_in):
    signed_in()
    _serve_posts(page,
                 _post(id="r1", topic="Shipped it", status="published"),
                 _post(id="r2", topic="Still a draft", status="draft"))
    open_section(page, "results")
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
    page.locator('[data-section="biz-journal"]').click()
    expect(page.locator("#biz-journal-list")).to_contain_text("What the human approved")
    expect(page.locator("#biz-journal-list")).to_contain_text("edited by a human")


def test_an_empty_journal_says_so(page, signed_in):
    signed_in(account_type="business")
    page.route("**/api/business/journal*", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.locator('[data-section="biz-journal"]').click()
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
