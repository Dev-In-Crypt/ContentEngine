"""One idea, one card — the cost of phase 4 paid back in the lists.

Siblings are ordinary posts, which is what makes publishing and scheduling work
untouched. The price is that one idea is several rows, so without grouping the
Queue and the Calendar would show the same thought two or three times and the
list would grow every time somebody pressed Adapt.

Two screens are deliberately NOT grouped, and each has a test saying so:

  * the profile grid simulates an Instagram profile page, so it filters to
    Instagram and therefore already shows at most one member per group — adding
    grouping there would be wrong, and removing the filter would put a tweet in
    a simulated Instagram feed;
  * Results lists what was published, and siblings publish separately with
    separate permalinks and separate metrics — collapsing them would hide a
    result the user went looking for.

The Calendar groups WITHIN a day rather than across the month, because siblings
are scheduled independently: an idea can legitimately straddle two dates, and a
group-level date would have to be invented.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from models.schemas import PostSummary

from tests.e2e.nav import open_calendar, open_results, open_section

pytestmark = pytest.mark.e2e


def _post(**over):
    fields = dict(
        id="p1", topic="Sourdough starter", format="single", status="draft",
        platform="instagram", variant_group_id="g1",
        created_at=datetime.now(timezone.utc),
    )
    fields.update(over)
    return PostSummary(**fields).model_dump(mode="json")


def _serve(page, *posts):
    page.route("**/api/posts*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(list(posts))))


def _on(day_offset=1, **over):
    when = (datetime.now(timezone.utc) + timedelta(days=day_offset)).isoformat()
    return _post(scheduled_at=when, status="scheduled", **over)


# ── the queue ───────────────────────────────────────────────────────────────

def test_two_networks_of_one_idea_are_one_card(page, signed_in):
    signed_in()
    _serve(page,
           _post(id="p1", platform="instagram"),
           _post(id="p2", platform="x"))
    open_section(page, "queue")

    assert page.locator("#queue-list > .ce-card").count() == 1
    card = page.locator("#queue-list > .ce-card").first
    expect(card).to_contain_text("Sourdough starter")
    expect(card).to_contain_text("📸")
    expect(card).to_contain_text("𝕏")


def test_two_separate_ideas_stay_two_cards(page, signed_in):
    signed_in()
    _serve(page,
           _post(id="p1", topic="Sourdough starter", variant_group_id="g1"),
           _post(id="p2", topic="Rye loaf", variant_group_id="g2"))
    open_section(page, "queue")
    assert page.locator("#queue-list > .ce-card").count() == 2


def test_a_group_card_reports_the_sibling_in_trouble(page, signed_in):
    """A card that says "scheduled" while one network failed is a lie the user
    acts on — they see it going out and never look again."""
    signed_in()
    _serve(page,
           _post(id="p1", platform="instagram", status="scheduled",
                 scheduled_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
           _post(id="p2", platform="x", status="failed",
                 schedule_error="X rejected the media"))
    open_section(page, "queue")

    # The collapsed header only — the per-network rows below it are in the DOM
    # (hidden) and carry their own dots, so asserting on the whole card would
    # find the red one whatever the group reported.
    header = page.locator("#queue-list > .ce-card > div").first
    expect(header).to_contain_text("🔴")


def test_a_group_can_be_opened_out_into_its_networks(page, signed_in):
    """Collapsed by default, because the common case is one idea going out. But
    the siblings are separate posts with separate schedules, so there has to be
    a way to reach each of them."""
    signed_in()
    _serve(page,
           _post(id="p1", platform="instagram"),
           _post(id="p2", platform="x"))
    open_section(page, "queue")

    # Present in the DOM but not on screen — count would pass either way.
    expect(page.locator('#queue-list [data-group-row]').first).to_be_hidden()
    page.locator('#queue-list [data-expand-group]').click()
    expect(page.locator('#queue-list [data-group-row]')).to_have_count(2)
    expect(page.locator('#queue-list [data-group-row]').first).to_be_visible()
    expect(page.locator('#queue-list [data-group-row]').last).to_be_visible()


def test_a_lone_post_has_nothing_to_expand(page, signed_in):
    signed_in()
    _serve(page, _post(id="p1"))
    open_section(page, "queue")
    expect(page.locator('#queue-list [data-expand-group]')).to_have_count(0)


def test_clicking_a_group_card_opens_a_post(page, signed_in):
    signed_in()
    _serve(page, _post(id="p1", platform="instagram"), _post(id="p2", platform="x"))
    page.route("**/api/posts/p1", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "id": "p1", "topic": "Sourdough starter", "format": "single",
            "status": "draft", "platform": "instagram", "slides": [],
            "variant_group_id": "g1", "variants": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        })))
    open_section(page, "queue")
    page.locator("#queue-list > .ce-card").first.click()
    expect(page.locator("#create-post-panel")).to_be_visible()


# ── the calendar ────────────────────────────────────────────────────────────

def test_the_calendar_collapses_a_group_inside_one_day(page, signed_in):
    signed_in()
    _serve(page,
           _on(1, id="p1", platform="instagram"),
           _on(1, id="p2", platform="x"))
    open_calendar(page)
    expect(page.locator("#cal-grid [data-cal-entry]")).to_have_count(1)


def test_the_calendar_keeps_siblings_on_their_own_days(page, signed_in):
    """Siblings schedule independently, so an idea can straddle two dates. A
    group-level date would have to be invented, and would be wrong on one of
    them."""
    signed_in()
    _serve(page,
           _on(1, id="p1", platform="instagram"),
           _on(2, id="p2", platform="x"))
    open_calendar(page)
    expect(page.locator("#cal-grid [data-cal-entry]")).to_have_count(2)


# ── the two screens that must NOT group ─────────────────────────────────────

def test_the_profile_grid_is_group_safe_because_it_is_instagram_only(page, signed_in):
    """It already shows at most one member per group, so it needs no grouping —
    and must not lose the filter, or a tweet lands in a simulated Instagram
    profile."""
    signed_in()
    _serve(page,
           _post(id="p1", platform="instagram", status="published"),
           _post(id="p2", platform="x", status="published"))
    open_calendar(page, "profile")
    expect(page.locator("#grid-container > div")).to_have_count(1)


def test_results_lists_every_network_separately(page, signed_in):
    """Deliberately ungrouped: siblings publish separately and carry separate
    permalinks and metrics, so collapsing them would hide a result."""
    signed_in()
    _serve(page,
           _post(id="p1", platform="instagram", status="published",
                 published_at=datetime.now(timezone.utc).isoformat()),
           _post(id="p2", platform="x", status="published",
                 published_at=datetime.now(timezone.utc).isoformat()))
    open_results(page)
    expect(page.locator("#analytics-list > .ce-card")).to_have_count(2)
