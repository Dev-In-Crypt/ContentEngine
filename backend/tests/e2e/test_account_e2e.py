"""Connections, the data screen and erasure, in a real browser.

The delete-account flow is the one place in the product where a UI slip destroys
data, so its guards are worth exercising for real rather than in a stub.
"""
import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect

from tests.e2e.nav import open_settings

pytestmark = pytest.mark.e2e


def _open_account(page):
    """Data lives on the "Keys & spend" tab — spend, backup and GDPR together,
    because they are all about the account rather than about a brand."""
    _dismiss_wizard(page)
    open_settings(page, "keys")


def _dismiss_wizard(page):
    """Get the first-run modal out of the way, then carry on.

    It has to be *waited* for, not merely polled once: it opens a tick after load,
    so an immediate is_visible() says no and every later click then lands on the
    overlay instead of the page. Missing it entirely is not fatal here — that is
    the wizard's own tests' business — so a timeout just moves on."""
    modal = page.locator("#onboarding-modal")
    try:
        modal.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeout:
        return
    page.get_by_text("Close setup").click()
    expect(modal).to_be_hidden()


def test_the_data_section_offers_export_and_erasure(page, signup):
    signup()
    _open_account(page)
    expect(page.locator("#mydata-section")).to_be_visible()
    expect(page.get_by_role("button", name="Download my data")).to_be_visible()
    expect(page.get_by_role("button", name="Delete my account")).to_be_visible()


def test_export_downloads_an_archive(page, signup):
    signup()
    _open_account(page)
    with page.expect_download() as dl:
        page.get_by_role("button", name="Download my data").click()
    assert dl.value.suggested_filename.endswith(".zip")


def test_deleting_without_a_password_never_reaches_the_server(page, signup):
    signup()
    _open_account(page)
    page.get_by_role("button", name="Delete my account").click()
    expect(page.locator("#delete-account-modal")).to_be_visible()

    calls = []
    page.on("request", lambda r: calls.append(r.url) if "auth/delete" in r.url else None)
    page.get_by_role("button", name="Delete permanently").click()
    expect(page.locator("#del-acct-error")).to_contain_text("Enter your password")
    assert calls == []


def test_a_wrong_password_leaves_the_account_signed_in(page, signup):
    signup()
    _open_account(page)
    page.get_by_role("button", name="Delete my account").click()
    page.locator("#del-acct-password").fill("not-my-password")
    page.get_by_role("button", name="Delete permanently").click()
    expect(page.locator("#del-acct-error")).to_contain_text("not correct")
    expect(page.locator("#delete-account-modal")).to_be_visible()
    assert page.evaluate("localStorage.getItem('api_token')") is not None


def test_deleting_for_real_signs_the_user_out(page, signup):
    signup()
    _open_account(page)
    page.on("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Delete my account").click()
    page.locator("#del-acct-password").fill("password123")
    page.get_by_role("button", name="Delete permanently").click()
    page.wait_for_function("() => localStorage.getItem('api_token') === null")
    expect(page.locator("#landing-screen")).to_be_visible()


def test_the_escape_key_closes_the_delete_dialog(page, signup):
    signup()
    _open_account(page)
    page.get_by_role("button", name="Delete my account").click()
    page.keyboard.press("Escape")
    expect(page.locator("#delete-account-modal")).to_be_hidden()


def test_the_connections_page_shows_the_key_fields(page, signup):
    signup()
    page.get_by_text("Close setup").click()
    open_settings(page, "connections")
    expect(page.locator("#keys-section")).to_be_visible()
    expect(page.locator('#keys-form input[data-cred]').first).to_be_visible()


def test_the_account_page_offers_a_kling_key_for_video(page, signup):
    """Account, not Connections — Kling is an account-scoped key like ElevenLabs
    and Pexels, not a per-network publishing credential."""
    signup()
    page.get_by_text("Close setup").click()
    open_settings(page, "keys")
    expect(page.locator('#keys-form input[data-cred="kling_api_key"]')).to_be_visible()


# ── brand profiles (UX phase 2) ─────────────────────────────────────────────

def test_the_brand_switcher_starts_with_the_users_own_profile(page, signup):
    """"Personal" used to be a hardcoded option meaning "no brand row". Every
    user owns a profile now, so the switcher lists real rows only — and it must
    not be empty, which is what a stale hardcoded option was hiding."""
    signup()
    _dismiss_wizard(page)
    page.locator("#avatar-btn").click()
    switcher = page.locator("#menu-acct-switcher")
    expect(switcher).to_be_visible()
    expect(switcher.locator("option")).to_have_count(1)
    assert switcher.input_value()          # a real id, not ""


def test_a_new_brand_joins_the_switcher_below_the_main_one(page, signup):
    signup()
    _dismiss_wizard(page)
    page.locator("#avatar-btn").click()
    page.locator("#avatar-menu").get_by_text("Manage brands").click()
    page.fill("#brand-new-name", "Client A")
    page.locator("#brands-modal").get_by_role("button", name="Add").click()
    expect(page.locator("#brand-editor")).to_be_visible()
    page.locator("#brands-modal").get_by_text("✕").click()

    page.locator("#avatar-btn").click()
    options = page.locator("#menu-acct-switcher option")
    expect(options).to_have_count(2)
    expect(options.nth(1)).to_have_text("Client A")   # primary stays first


def test_the_main_profile_offers_no_delete_button(page, signup):
    """The API answers 409. Offering a button that always fails is worse than
    not offering it."""
    signup()
    _dismiss_wizard(page)
    page.locator("#avatar-btn").click()
    page.locator("#avatar-menu").get_by_text("Manage brands").click()
    page.locator("#brands-list button", has_text="Edit").first.click()
    expect(page.locator("#brand-editor")).to_be_visible()
    expect(page.locator("#brand-delete")).to_be_hidden()


def test_a_client_brand_does_offer_delete(page, signup):
    signup()
    _dismiss_wizard(page)
    page.locator("#avatar-btn").click()
    page.locator("#avatar-menu").get_by_text("Manage brands").click()
    page.fill("#brand-new-name", "Client A")
    page.locator("#brands-modal").get_by_role("button", name="Add").click()
    expect(page.locator("#brand-delete")).to_be_visible()


# ── Settings: one screen, tabs (UX phase 3.4) ───────────────────────────────

def test_settings_opens_from_the_avatar_menu(page, signed_in):
    """Account and Connections used to be two of fourteen top-level buttons.
    They are one screen behind the avatar now, which is where the four-section
    nav needs them to be."""
    signed_in()
    page.locator("#avatar-btn").click()
    expect(page.locator("#avatar-menu")).to_be_visible()
    page.locator("#avatar-menu").get_by_text("Settings").click()
    expect(page.locator("#view-settings")).to_be_visible()
    expect(page.locator("#settings-tabs")).to_be_visible()


def test_one_tab_at_a_time(page, signed_in):
    """The guard the top-level nav already has, rebuilt one level down. Two
    panel sets on screen at once is exactly the defect a hand-maintained hide
    list produces, which is why setSection derives its list from its map — and
    why this needs its own test rather than inheriting that one."""
    signed_in()
    open_settings(page, "profiles")
    expect(page.locator("#brand-profile-section")).to_be_visible()
    expect(page.locator("#usage-section")).to_be_hidden()

    open_settings(page, "keys")
    expect(page.locator("#usage-section")).to_be_visible()
    expect(page.locator("#brand-profile-section")).to_be_hidden()


def test_connections_shows_both_networks(page, signed_in):
    """There is no active network any more, so a page of "this network's keys"
    has no meaning. Scoping it to one would hide half a user's credentials with
    nothing on screen to say so."""
    signed_in()
    open_settings(page, "connections")
    expect(page.locator('#keys-form input[data-cred="instagram_access_token"]')).to_be_visible()
    expect(page.locator('#keys-form input[data-cred="x_api_key"]')).to_be_visible()
    # …and the account-wide keys stay on their own tab rather than on both.
    expect(page.locator('#keys-form input[data-cred="kling_api_key"]')).to_have_count(0)


def test_the_keys_tab_stays_account_scoped(page, signed_in):
    """Publishing credentials belong to Connections. Leaking them here would
    make the split pointless."""
    signed_in()
    open_settings(page, "keys")
    expect(page.locator('#keys-form input[data-cred="kling_api_key"]')).to_be_visible()
    expect(page.locator('#keys-form input[data-cred="x_api_key"]')).to_have_count(0)


def test_the_setup_guide_moved_to_the_avatar_menu(page, signed_in):
    """It was filed inside the credentials page — a first-run wizard reachable
    only from one screen. It belongs where every screen can reach it."""
    signed_in()
    page.locator("#avatar-btn").click()
    page.locator("#avatar-menu").get_by_text("Setup guide").click()
    expect(page.locator("#onboarding-modal")).to_be_visible()


def test_the_brand_switcher_moved_into_the_menu(page, signed_in):
    """Three separate dropdowns in the header is the thing the UX document
    names as the problem. They are one menu now."""
    signed_in()
    expect(page.locator("#acct-switcher")).to_have_count(0)   # the old header one
    page.locator("#avatar-btn").click()
    expect(page.locator("#avatar-menu #menu-acct-switcher")).to_be_visible()
