"""Connections, the data screen and erasure, in a real browser.

The delete-account flow is the one place in the product where a UI slip destroys
data, so its guards are worth exercising for real rather than in a stub.
"""
import pytest
from playwright.sync_api import expect

from tests.e2e.nav import (dismiss_onboarding, open_brands, open_onboarding,
                           open_settings)

pytestmark = pytest.mark.e2e


def _open_account(page):
    """Data lives on the "Keys & spend" tab — spend, backup and GDPR together,
    because they are all about the account rather than about a brand."""
    dismiss_onboarding(page)
    open_settings(page, "keys")


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
    dismiss_onboarding(page)
    open_settings(page, "connections")
    expect(page.locator("#keys-section")).to_be_visible()
    expect(page.locator('#keys-form input[data-cred]').first).to_be_visible()


def test_the_account_page_offers_a_kling_key_for_video(page, signup):
    """Account, not Connections — Kling is an account-scoped key like ElevenLabs
    and Pexels, not a per-network publishing credential."""
    signup()
    dismiss_onboarding(page)
    open_settings(page, "keys")
    expect(page.locator('#keys-form input[data-cred="kling_api_key"]')).to_be_visible()


# ── brand profiles (UX phase 2) ─────────────────────────────────────────────

def test_the_brand_switcher_starts_with_the_users_own_profile(page, signup):
    """"Personal" used to be a hardcoded option meaning "no brand row". Every
    user owns a profile now, so the switcher lists real rows only — and it must
    not be empty, which is what a stale hardcoded option was hiding."""
    signup()
    dismiss_onboarding(page)
    switcher = page.locator("#brand-switcher")
    expect(switcher).to_be_visible()
    expect(switcher.locator("option")).to_have_count(1)
    assert switcher.input_value()          # a real id, not ""


def test_a_new_brand_joins_the_switcher_below_the_main_one(page, signup):
    signup()
    dismiss_onboarding(page)
    open_brands(page)
    page.fill("#brand-new-name", "Client A")
    page.locator("#brands-modal").get_by_role("button", name="Add").click()
    expect(page.locator("#brand-editor")).to_be_visible()
    page.locator("#brands-modal").get_by_text("✕").click()

    options = page.locator("#brand-switcher option")
    expect(options).to_have_count(2)
    expect(options.nth(1)).to_have_text("Client A")   # primary stays first


def test_the_main_profile_offers_no_delete_button(page, signup):
    """The API answers 409. Offering a button that always fails is worse than
    not offering it."""
    signup()
    dismiss_onboarding(page)
    open_brands(page)
    page.locator("#brands-list button", has_text="Edit").first.click()
    expect(page.locator("#brand-editor")).to_be_visible()
    expect(page.locator("#brand-delete")).to_be_hidden()


def test_a_client_brand_does_offer_delete(page, signup):
    signup()
    dismiss_onboarding(page)
    open_brands(page)
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
    open_onboarding(page)


def test_there_is_one_brand_switcher_and_it_is_in_the_rail(page, signed_in):
    """Three separate dropdowns in the header was the thing the UX document
    named as the problem; phase 3 folded them into the avatar menu, and 11.1
    moved this one out again — into the rail, where the answer to "which brand
    am I in" is visible without opening anything.

    Both older ids are asserted gone. The first, `#acct-switcher`, is the header
    control phase 3 removed — and the change handler went on reading it for
    eight months afterwards, so choosing a brand threw and did nothing. An id
    that no longer exists anywhere is the only version of that which cannot
    happen again.
    """
    signed_in()
    expect(page.locator("#acct-switcher")).to_have_count(0)        # the header one
    expect(page.locator("#menu-acct-switcher")).to_have_count(0)   # the avatar-menu one
    expect(page.locator("#brand-switcher")).to_be_visible()
