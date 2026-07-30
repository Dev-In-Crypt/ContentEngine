"""Connections, the data screen and erasure, in a real browser.

The delete-account flow is the one place in the product where a UI slip destroys
data, so its guards are worth exercising for real rather than in a stub.
"""
import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def _open_account(page):
    _dismiss_wizard(page)
    page.locator('[data-section="account"]').click()


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
    page.locator('[data-section="keys"]').click()
    expect(page.locator("#keys-section")).to_be_visible()
    expect(page.locator('#keys-form input[data-cred]').first).to_be_visible()


def test_the_account_page_offers_a_kling_key_for_video(page, signup):
    """Account, not Connections — Kling is an account-scoped key like ElevenLabs
    and Pexels, not a per-network publishing credential."""
    signup()
    page.get_by_text("Close setup").click()
    page.locator('[data-section="account"]').click()
    expect(page.locator('#keys-form input[data-cred="kling_api_key"]')).to_be_visible()
