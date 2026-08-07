"""Two ways the app could show you nothing at all.

Both are live today and both sit directly under UX phase 5: the onboarding it
replaces ends by calling `setSection` with a name that no longer exists, and the
missing-key modal — the escape hatch the new flow leans on the moment the wizard
stops offering to take an AI key — sends you to a Settings TAB through the
SECTION router.

Neither throws. `setSection` derives its view list from its map and hides every
view whose id is not the match, so an unknown name hides all of them and leaves
the page blank. That is the worst shape a bug can have here: nothing in the
console, nothing in the logs, and a user looking at an empty screen.
"""
import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_create

pytestmark = pytest.mark.e2e


def test_an_unknown_section_still_leaves_a_screen_on_display(page, signed_in):
    """`biz-sources` stopped being a section in 3.8, and the old wizard's Finish
    button still asks for it — so a Business account that finishes setup lands on
    a blank page today. Rather than chase every stale caller, the router refuses
    to end up with nothing on screen."""
    signed_in()
    page.evaluate("setSection('biz-sources')")
    expect(page.locator("#view-create")).to_be_visible()

    # ...and a real section still wins, so the fallback cannot be masking a
    # router that simply always shows Create.
    page.evaluate("setSection('queue')")
    expect(page.locator("#view-queue")).to_be_visible()
    expect(page.locator("#view-create")).to_be_hidden()


def test_open_settings_from_the_missing_key_modal_shows_the_ai_models_page(page, signed_in):
    """"Open settings" in the missing-key modal passed 'keys' — a Settings tab —
    to the section router, which knows five section names and none of them is
    that. Every view got hidden and the page went blank, one click after the
    product told the user what was missing."""
    signed_in()
    page.evaluate("""needKey('Set up your AI model', 'You need a key.', 'keys')""")
    expect(page.locator("#need-key-modal")).to_be_visible()

    page.evaluate("gotoNeedKey()")

    expect(page.locator("#need-key-modal")).to_be_hidden()
    expect(page.locator("#view-settings")).to_be_visible()
    expect(page.locator("#ai-models-section")).to_be_visible()


def test_the_real_generate_guard_lands_on_the_ai_models_page(page, signed_in):
    """Through the actual guard rather than a hand-written needKey call: the tab
    names live at the call sites, and a wrong one there is a smaller version of
    the same failure — the product explains what is missing and then shows you
    somewhere it isn't."""
    signed_in()
    assert page.evaluate("guardGenerateKeys()") is not True
    expect(page.locator("#need-key-modal")).to_be_visible()

    page.evaluate("gotoNeedKey()")
    expect(page.locator("#view-settings")).to_be_visible()
    expect(page.locator("#ai-models-section")).to_be_visible()


def test_the_publishing_guard_opens_the_page_with_the_network_fields(page, signed_in):
    """The other tab the modal points at. Keys & spend holds the AI providers;
    the network credentials are on Connections, and sending somebody to the
    wrong one is a smaller version of the same failure."""
    signed_in()
    page.evaluate("""needKey('Connect X', 'Publishing to X needs your keys.', 'connections')""")
    page.evaluate("gotoNeedKey()")

    expect(page.locator("#view-settings")).to_be_visible()
    expect(page.locator("#keys-section")).to_be_visible()


def test_the_composer_survives_the_round_trip(page, signed_in):
    """A guard that fires mid-compose must leave somewhere to come back to."""
    signed_in()
    open_create(page, "post")
    page.evaluate("""needKey('Set up your AI model', 'You need a key.', 'keys')""")
    page.evaluate("gotoNeedKey()")
    expect(page.locator("#view-settings")).to_be_visible()

    page.evaluate("setSection('create')")
    expect(page.locator("#create-post-panel")).to_be_visible()
