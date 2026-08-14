"""The Insights screen, and the sentence under its numbers.

The rollup is tested against the database in tests/test_insights_rollup.py.
What is here is the half no server test can see: whether the screen prints the
part that makes the numbers honest.

Four large numbers over a month whose posts were mostly never refreshed is not
a month's reach — it is a few posts' reach wearing a month's label. The route
reports how much it measured; if the screen drops that line, the screen lies
with numbers that are individually correct.
"""
import json

import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_results

pytestmark = pytest.mark.e2e


def _body(**over):
    fields = dict(days=30, posts_out=8, on_time=7, measured_posts=8,
                  networks_without_metrics=[],
                  reach={"value": 38200, "delta_pct": 21.0},
                  saves={"value": 612, "delta_pct": None},
                  spend_usd=3.18,
                  by_post=[{"id": "p1", "topic": "Grinder settings", "reach": 4100}],
                  best={"id": "p1", "topic": "Grinder settings", "reach": 4100})
    fields.update(over)
    return fields


def _serve(page, **over):
    page.route("**/api/insights*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(_body(**over))))


def test_the_numbers_are_on_the_screen(page, signed_in):
    _serve(page)
    signed_in()
    open_results(page, "insights")

    tiles = page.locator("#insights-tiles")
    expect(tiles).to_contain_text("Reach")
    expect(tiles).to_contain_text("Posts out")
    expect(tiles).to_contain_text("7 on time")


def test_a_partly_measured_month_says_so(page, signed_in):
    """The one line that stops this screen being a confident lie."""
    _serve(page, posts_out=18, measured_posts=4)
    signed_in()
    open_results(page, "insights")

    coverage = page.locator("#insights-coverage")
    expect(coverage).to_contain_text("4 of 18")


def test_a_fully_measured_month_does_not_apologise(page, signed_in):
    """The warning has to disappear when it does not apply, or it becomes
    furniture nobody reads."""
    _serve(page, posts_out=8, measured_posts=8)
    signed_in()
    open_results(page, "insights")

    expect(page.locator("#insights-coverage")).to_have_text("")


def test_a_network_with_no_metrics_is_named_on_the_screen(page, signed_in):
    _serve(page, networks_without_metrics=["x"])
    signed_in()
    open_results(page, "insights")

    expect(page.locator("#insights-coverage")).to_contain_text("X reports no metrics")


def test_no_earlier_period_is_said_rather_than_shown_as_a_jump(page, signed_in):
    """Saves has no previous window here. "+100%" would be an invention."""
    _serve(page, saves={"value": 612, "delta_pct": None})
    signed_in()
    open_results(page, "insights")

    expect(page.locator("#insights-tiles")).to_contain_text("no earlier period")


def test_a_month_with_nothing_measured_says_that_too(page, signed_in):
    _serve(page, by_post=[], best=None, posts_out=3, measured_posts=0)
    signed_in()
    open_results(page, "insights")

    expect(page.locator("#insights-chart")).to_contain_text("Nothing measured")
    expect(page.locator("#insights-best")).to_be_hidden()
