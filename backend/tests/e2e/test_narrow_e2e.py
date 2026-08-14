"""The whole shell on a phone-width screen.

UX phase 11 added a left rail, a seven-column week grid, a two-column editor,
a bar chart and a row of stat tiles — every one of them designed at 1280px,
which is the width the mockups are drawn at and not the width half the people
approving a post from a sofa are using.

"Looks cramped" is not testable. Sideways scroll is: a page that is wider than
the window has something in it that refused to fold, and the reader has to drag
the whole layout left to finish a sentence. That is the failure this file
measures, on every screen the rail can reach.
"""
import json

import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_results, open_section, open_settings

pytestmark = pytest.mark.e2e

PHONE = {"width": 390, "height": 844}


def _serve_everything(page):
    """Enough content that each screen renders its widest shape rather than an
    empty state — an empty screen never overflows, and would pass this for the
    wrong reason."""
    page.route("**/api/posts*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps([
            {"id": f"p{i}", "topic": "A reasonably long post topic that will wrap",
             "format": "single", "status": "published", "platform": "instagram",
             "variant_group_id": f"g{i}", "created_at": "2026-08-12T09:00:00+00:00",
             "published_at": "2026-08-12T09:00:00+00:00",
             "published_url": "https://instagram.com/p/abc",
             "metrics": {"snapshot_at": "2026-08-13T09:00:00+00:00",
                         "reach": 4100, "likes": 318, "saved": 41}}
            for i in range(6)])))
    page.route("**/api/insights*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "days": 30, "posts_out": 6, "on_time": 6, "measured_posts": 6,
            "networks_without_metrics": ["x"],
            "reach": {"value": 38200, "delta_pct": 21.0},
            "saves": {"value": 612, "delta_pct": -3.0},
            "spend_usd": 3.18,
            "by_post": [{"id": "p1", "topic": "Grinder settings", "reach": 4100}],
            "best": {"id": "p1", "topic": "Grinder settings", "reach": 4100}})))


def _overflow(page) -> int:
    """How far past the window the document reaches, in pixels."""
    return page.evaluate(
        "Math.max(0, document.documentElement.scrollWidth - window.innerWidth)")


@pytest.mark.parametrize("where", ["create", "queue", "calendar", "results"])
def test_no_section_scrolls_sideways_on_a_phone(page, signed_in, where):
    page.set_viewport_size(PHONE)
    _serve_everything(page)
    signed_in()
    open_section(page, where)

    assert _overflow(page) <= 1, f"{where} is {_overflow(page)}px wider than the screen"


def test_insights_does_not_scroll_sideways_on_a_phone(page, signed_in):
    """Four stat tiles and a chart, designed as a 1280px row."""
    page.set_viewport_size(PHONE)
    _serve_everything(page)
    signed_in()
    open_results(page, "insights")
    expect(page.locator("#insights-tiles")).to_be_visible()

    assert _overflow(page) <= 1


def test_settings_does_not_scroll_sideways_on_a_phone(page, signed_in):
    page.set_viewport_size(PHONE)
    _serve_everything(page)
    signed_in()
    open_settings(page, "profiles")

    assert _overflow(page) <= 1


def test_the_rail_is_still_reachable_on_a_phone(page, signed_in):
    """Folding is only a fix if the destinations survive it."""
    page.set_viewport_size(PHONE)
    _serve_everything(page)
    signed_in()

    for section in ("create", "queue", "results"):
        expect(page.locator(f'#shell-nav [data-section="{section}"]')).to_be_visible()
