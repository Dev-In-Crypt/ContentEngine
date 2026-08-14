"""Where the money went, and one honest word about the threshold.

The per-model breakdown already existed — in a popover hanging off the header
badge, as a run of plain text, on a screen nobody opens to plan spending. The
Keys tab, which is where somebody stands when they are thinking about cost,
showed a single sentence with two totals in it.

The threshold is the part worth being careful about. The mockup draws a
"spending cap: stop generating above $25 / month". There is no such thing here:
the only cap in the product is platform-wide, applies to OUR key, and is not
user-editable. What does exist is a number that turns the header badge red. So
that is what the screen offers, and it says what it does — a cap that does not
cap is a promise the product breaks the first time it matters.
"""
import json

import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_settings

pytestmark = pytest.mark.e2e


def _usage(page, **over):
    body = dict(today={"cost": 1.24, "tokens": 900, "calls": 12},
                month={"cost": 3.18, "tokens": 9000, "calls": 120},
                by_model=[{"model": "anthropic/claude-sonnet", "cost": 2.29, "calls": 80},
                          {"model": "anthropic/claude-haiku", "cost": 0.71, "calls": 32},
                          {"model": "elevenlabs/tts", "cost": 0.18, "calls": 8}],
                free=None)
    body.update(over)
    page.route("**/api/usage", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(body)))


def test_the_breakdown_is_where_the_money_is_discussed(page, signed_in):
    """It existed only in a popover on the header badge — the one place nobody
    is standing when they think about cost."""
    _usage(page)
    signed_in()
    open_settings(page, "keys")

    usage = page.locator("#admin-usage")
    expect(usage).to_contain_text("claude-sonnet")
    expect(usage).to_contain_text("2.29")
    expect(usage).to_contain_text("elevenlabs")


def test_a_month_with_no_calls_says_so_rather_than_showing_an_empty_chart(page, signed_in):
    _usage(page, by_model=[], month={"cost": 0, "tokens": 0, "calls": 0})
    signed_in()
    open_settings(page, "keys")

    expect(page.locator("#admin-usage")).to_contain_text("Nothing spent")


def test_the_threshold_is_described_as_a_warning_not_a_cap(page, signed_in):
    """The mockup says "stop generating above $25". Nothing here stops
    anything — this number colours a badge, and saying otherwise is a promise
    the product breaks the first time somebody relies on it."""
    _usage(page)
    signed_in()
    open_settings(page, "keys")

    row = page.locator("#spend-alert-row")
    expect(row).to_be_visible()
    expect(row).to_contain_text("Warn")
    expect(row).not_to_contain_text("stop")
    expect(row).not_to_contain_text("cap")


def test_the_threshold_is_remembered(page, signed_in):
    _usage(page)
    signed_in()
    open_settings(page, "keys")

    page.locator("#spend-alert").fill("12")
    page.locator("#spend-alert").dispatch_event("change")

    assert page.evaluate("localStorage.getItem('cost_limit')") == "12"


def test_nothing_offers_a_plan_that_does_not_exist(page, signed_in):
    """No billing exists in any form. The mockup's "flat seat fee — see plans"
    would be selling something that cannot be bought."""
    _usage(page)
    signed_in()
    open_settings(page, "keys")

    text = page.locator("#view-settings").inner_text().lower()
    for word in ("see plans", "seat fee", "upgrade to", "subscription"):
        assert word not in text, f"settings offers {word!r}"
