"""The Feed: what went out, what did not, and how it did.

The screen showed a link and a date. Everything a person goes there to see —
did anyone look at it, and did the failed one ever get sorted — was either
absent or one post at a time behind a manual Refresh.

Two honesty rules the mockup does not state and this screen has to:

  * **No metrics is not zero metrics.** A post nobody has fetched numbers for
    shows none. "0 reach" is a claim that nobody saw it.
  * **X has no numbers at all.** There is no insights API for it here, so an X
    row shows the post and says so, rather than an empty space that reads as a
    flop.
"""
import json
import re

import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_results

pytestmark = pytest.mark.e2e


def _post(**over):
    fields = dict(id="p1", topic="Grinder settings, plainly", format="single",
                  status="published", platform="instagram", variant_group_id="g1",
                  created_at="2026-08-12T09:00:00+00:00",
                  published_at="2026-08-12T09:00:00+00:00",
                  published_url="https://instagram.com/p/abc", metrics=None)
    fields.update(over)
    return fields


def _serve(page, *rows):
    page.route("**/api/posts*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(list(rows))))


def test_a_measured_post_shows_its_numbers(page, signed_in):
    _serve(page, _post(metrics={"snapshot_at": "2026-08-13T09:00:00+00:00",
                                "reach": 4100, "likes": 318, "saved": 41}))
    signed_in()
    open_results(page, "posts")

    row = page.locator("#analytics-list .ce-card").first
    expect(row).to_contain_text("reach")
    # Digits only: the number is formatted for the reader's locale, so the
    # thousands separator is a space here and a comma somewhere else. Asserting
    # the rendered string would make this test fail on a colleague's machine.
    digits = re.sub(r"\D", "", row.inner_text())
    assert "4100" in digits and "318" in digits and "41" in digits


def test_an_unmeasured_post_shows_no_numbers_rather_than_zeros(page, signed_in):
    """Nobody has asked Instagram yet. Rendering that as 0 reach says nobody
    looked, which is a different claim and usually a false one."""
    _serve(page, _post(metrics=None))
    signed_in()
    open_results(page, "posts")

    row = page.locator("#analytics-list .ce-card").first
    expect(row).to_contain_text("Grinder settings")
    expect(row).not_to_contain_text("0 reach")


def test_an_x_post_says_why_it_has_no_numbers(page, signed_in):
    """There is no insights API for X here. An empty space where the numbers
    go reads as a post that flopped."""
    _serve(page, _post(id="x1", platform="x", topic="Roast is not strength",
                       published_url="https://x.com/i/status/1"))
    signed_in()
    open_results(page, "posts")

    expect(page.locator("#analytics-list")).to_contain_text("no metrics")


def test_the_failed_tab_shows_what_never_went_out(page, signed_in):
    """A failure on the Published screen is invisible, which is how a post that
    never went out gets counted as one that did."""
    _serve(page,
           _post(id="ok1", topic="This one went out"),
           _post(id="bad1", topic="Espresso myths", status="failed",
                 published_at=None, published_url=None,
                 schedule_error="Instagram token expired"))
    signed_in()
    open_results(page, "posts")

    page.locator('#feed-tabs [data-feed-tab="failed"]').click()

    expect(page.locator("#analytics-list")).to_contain_text("Espresso myths")
    expect(page.locator("#analytics-list")).to_contain_text("token expired")
    expect(page.locator("#analytics-list")).not_to_contain_text("This one went out")


def test_a_failed_post_offers_the_way_out(page, signed_in):
    """Telling somebody their token expired without a way to reconnect is a
    diagnosis, not help."""
    _serve(page, _post(id="bad1", topic="Espresso myths", status="failed",
                       published_at=None, published_url=None,
                       schedule_error="Instagram token expired"))
    signed_in()
    open_results(page, "posts")
    page.locator('#feed-tabs [data-feed-tab="failed"]').click()

    page.locator('#analytics-list [data-action="open-settings-tab"]').first.click()

    expect(page.locator("#view-settings")).to_be_visible()
    expect(page.locator("#x-settings-section")).to_be_visible()
