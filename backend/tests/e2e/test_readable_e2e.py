"""Every semantic colour, measured, in both themes.

Reported from the running product, twice in a row: a warning in pale yellow on
the near-white light theme, unreadable; then the verify banner in the dark
theme, a muddy brown nobody chose.

One cause. The theme drives every grey and every accent through variables, but
the *semantic* colours — warning, error, success — were left as stock Tailwind
from when the only theme was dark. `text-yellow-300` is lovely on #141416 and
invisible on #f2f2f3; `bg-yellow-900/90` is a warm banner on black and mud on
white. Thirty-odd such classes across the app, each legible in exactly one
theme, which is how they survived this long.

These tests do not check which class is used or what the variable is called.
They measure the contrast a person actually gets, so the fix is free to change
and the requirement is not.
"""
import json

import pytest
from playwright.sync_api import expect

from tests.e2e.contrast import assert_readable

pytestmark = pytest.mark.e2e


def _sse(*frames: dict) -> str:
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames)


# ── a warning with nothing behind it ────────────────────────────────────────

def test_the_niche_we_could_not_guess_is_readable(page, signup):
    """The one that was reported: a status line with no background of its own,
    so it sits directly on the page in whichever theme is on."""
    signup()
    page.route("**/api/brand/extract", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"source_url": "https://crumb.example", "name": "Crumb & Co",
                         "description": "A bakery.", "niche": "", "target_audience": "",
                         "colors": ["#8a4b2a"], "logo_data_url": None})))
    page.locator('[data-onb-type="creator"]').click()
    expect(page.locator("#onb-s2")).to_be_visible()
    page.locator("#onb-site").fill("https://crumb.example")
    page.locator("#onb-read-site").click()

    status = page.locator("#onb-brand-status")
    expect(status).to_contain_text("couldn't guess")
    assert_readable(page, status, "the niche warning")


def test_a_saved_brand_says_so_readably(page, signup):
    """The success half of the same helper. Green on white was never as bad as
    yellow, which is exactly why it would have been left behind."""
    signup()
    page.route("**/api/brand/extract", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"source_url": "https://crumb.example", "name": "Crumb & Co",
                         "description": "A bakery.", "niche": "", "target_audience": "",
                         "colors": [], "logo_data_url": None})))
    page.locator('[data-onb-type="creator"]').click()
    page.locator("#onb-no-site").click()
    page.locator("#onb-niche").fill("x")          # too short → the helper speaks
    page.locator("#onb-continue-brand").click()

    assert_readable(page, page.locator("#onb-brand-status"), "the brand status")


# ── a warning that paints its own background ────────────────────────────────

def test_the_verify_email_banner_is_readable(page, signed_in):
    """The second report. A full-width banner is the largest coloured surface
    in the product and the first thing on the page for a new account."""
    signed_in()
    banner = page.locator("#verify-banner")
    expect(banner).to_be_visible()

    assert_readable(page, banner, "the verify-email banner")


# ── an error that paints its own background ─────────────────────────────────

def test_the_generation_error_frame_is_readable(page, signed_in):
    signed_in()
    page.route("**/api/settings/ai", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"text_provider": "openrouter", "text_model": "m",
                         "image_provider": "openrouter", "image_model": "m",
                         "keys": {"openrouter": {"set": True, "masked": "sk-…1"}}})))
    page.route("**/api/posts/generate", lambda r: r.fulfill(
        status=200, content_type="text/event-stream",
        body=_sse({"type": "error", "message": "Your provider rejected the key."})))

    page.locator("#topic").fill("Sourdough starters")
    page.locator("#generate-btn").click()
    expect(page.locator("#gen-error")).to_be_visible()

    assert_readable(page, page.locator("#gen-error-msg"), "the generation error")
