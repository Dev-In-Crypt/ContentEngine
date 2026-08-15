"""The shell: one rail down the left, and the two facts it holds.

Until UX phase 11 the chrome was a header plus a horizontal strip of four
buttons, and the two things a person needs at a glance — which brand they are
working in, and how much free generation is left — were not in the chrome at
all. The brand switcher was a `<select>` inside the avatar dropdown; the
allowance was one line of text underneath the Generate button, visible only
while standing on the composer.

The mockups put both in the rail, which is also where the bug was found: the
switcher's change handler has been reading an element id that does not exist in
the markup, so choosing a brand threw on its first line and did nothing at all.
"""
import json

import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_section

pytestmark = pytest.mark.e2e


def _usage(free):
    return {"today": {"cost": 0.0, "tokens": 0, "calls": 0},
            "month": {"cost": 0.0, "tokens": 0, "calls": 0},
            "by_model": [], "free": free}


def _serve(page, pattern, payload):
    page.route(pattern, lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(payload)))


# ── one navigation ──────────────────────────────────────────────────────────

def test_the_shell_has_exactly_one_navigation(page, signed_in):
    """The rail replaces the strip rather than joining it. Two navigations is
    the state phase 3 spent nine commits leaving."""
    signed_in()

    expect(page.locator("#shell-nav")).to_be_visible()
    assert page.locator("#section-nav").count() == 0


def test_every_destination_is_still_reachable(page, signed_in):
    """The rail is a new shape for the same map — no screen may be stranded."""
    signed_in()
    for section in ("create", "calendar", "queue", "results"):
        expect(page.locator(f'#shell-nav [data-section="{section}"]')).to_be_visible()
    # Promoted out of the avatar menu, where they were two clicks and a guess.
    for tab in ("profiles", "keys"):
        expect(page.locator(f'#shell-nav [data-settings-tab="{tab}"]')).to_be_visible()


def test_the_rail_takes_you_to_settings_on_the_right_tab(page, signed_in):
    signed_in()

    page.locator('#shell-nav [data-settings-tab="keys"]').click()

    expect(page.locator("#view-settings")).to_be_visible()
    expect(page.locator("#keys-section")).to_be_visible()


# ── the brand switcher, and the bug it was hiding ───────────────────────────

def test_switching_brands_actually_switches(page, signed_in):
    """Found while moving this control, not by any test.

    `onAcctSwitch` read `document.getElementById('acct-switcher')` while the
    markup called it `menu-acct-switcher`. There is no element by that name, so
    the very first line threw and the request was never made — brand switching
    has been dead for every agency account, silently, with a control that looked
    like it worked.
    """
    signed_in()
    _serve(page, "**/api/accounts", {
        "accounts": [{"id": "b1", "name": "Northbeam", "is_primary": True},
                     {"id": "b2", "name": "Halden", "is_primary": False}],
        "active_account_id": "b1"})
    page.reload()
    switched = []
    page.route("**/api/accounts/switch", lambda r: (
        switched.append(json.loads(r.request.post_data or "{}")),
        r.fulfill(status=200, content_type="application/json",
                  body=json.dumps({"active_account_id": "b2"}))))

    page.locator("#brand-switcher").select_option("b2")

    expect(page.locator("#toast")).to_contain_text("Switched")
    assert switched and switched[0]["account_id"] == "b2"


# ── the allowance, where it can be seen ─────────────────────────────────────

def test_the_meter_shows_the_number_the_server_gives(page, signed_in):
    """Two, not five. The mockup drew "2 / 5" and the server's limit is 2 — the
    landing made exactly this mistake and shipped it for weeks."""
    _serve(page, "**/api/usage", _usage({"remaining": 1, "limit": 2}))
    signed_in()

    meter = page.locator("#shell-meter")
    expect(meter).to_be_visible()
    expect(meter).to_contain_text("1")
    expect(meter).to_contain_text("2")
    expect(meter).not_to_contain_text("5")


def test_an_account_with_no_allowance_is_not_shown_one(page, signed_in):
    """`free` is null for the desktop owner, for anybody paying with their own
    key, and for a deployment with no application key. For them it is not a
    smaller number — it is not a subject."""
    _serve(page, "**/api/usage", _usage(None))
    signed_in()
    open_section(page, "create")

    expect(page.locator("#shell-meter")).to_be_hidden()


def test_the_avatar_shows_who_is_signed_in(page, signed_in):
    """Found by looking at the header and then grepping for the id.

    `#avatar-initial` shipped with a hardcoded "?" and no code anywhere wrote to
    it — so every signed-in account, with its email two centimetres to the
    right, was shown a question mark inside the most heavily painted shape on
    the page. It read as a help button. There is no help button.
    """
    signed_in()

    initial = page.locator("#avatar-initial")
    expect(initial).to_be_visible()
    expect(initial).not_to_have_text("?")
    # Whatever the fixture's account is, the badge and the chip agree.
    email = page.locator("#user-chip").inner_text().strip()
    assert email, "fixture problem: no email on the chip to compare against"
    assert initial.inner_text().strip().upper() == email[0].upper()


# ── the brand preview (UX phase 11.8) ───────────────────────────────────────

def test_the_brand_kit_shows_a_real_slide(page, signed_in):
    """Seeing what a colour did used to mean generating a post — a model call,
    and on the free tier one of two.

    The assertion is that the picture DECODED, not that the src reads well.
    Shipped and found on the live site: the route requires the bearer token and
    `<img src>` cannot carry one, so the element sat there with a correct-looking
    URL and no pixels behind it. The old test asserted the attribute and passed
    the whole time. `naturalWidth` is the only claim a wrong src cannot satisfy.
    """
    from tests.e2e.nav import open_settings
    signed_in()
    open_settings(page, "profiles")

    img = page.locator("#brand-preview")
    expect(img).to_be_visible()
    # Polls: the fetch, the decode and this read are three separate moments.
    expect(img).to_have_js_property("naturalWidth", 1080)


def test_the_preview_is_asked_for_again_rather_than_remembered(page, signed_in):
    """The URL is constant and the picture is not. An agency switching brands
    all day would otherwise be shown the previous client's slide, plausibly."""
    from tests.e2e.nav import open_settings
    signed_in()
    open_settings(page, "profiles")
    expect(page.locator("#brand-preview")).to_have_js_property("naturalWidth", 1080)
    first = page.locator("#brand-preview").get_attribute("src")

    page.evaluate("refreshBrandPreview()")

    expect(page.locator("#brand-preview")).not_to_have_js_property("src", first)
