"""The Photos library tab, driven by a real browser.

Two requests are faked, both built from the server's own response models
(`MediaAssetSummary`/`MediaAssetDetail`) rather than hand-written JSON, the
same discipline test_composer_e2e.py uses: a hand-rolled fake drifts from the
real payload and the drift looks exactly like a passing test.
"""
import json

import pytest
from playwright.sync_api import expect

from models.schemas import MediaAssetDetail

from tests.e2e.nav import open_create

pytestmark = pytest.mark.e2e


def _asset(**over) -> dict:
    fields = dict(
        id="11111111-1111-4111-8111-111111111111",
        kind="image", status="ready", source="ai_gen",
        url="/api/media/11111111-1111-4111-8111-111111111111/file",
        title="a cat on a windowsill", width=1024, height=1024, bytes=12345,
        created_at="2026-07-29T00:00:00Z",
        provider="openrouter", model="google/gemini-image",
        prompt="a cat on a windowsill",
    )
    fields.update(over)
    return MediaAssetDetail(**fields).model_dump(mode="json")


def test_switching_to_photos_hides_the_other_modes(page, signed_in):
    """The exact defect a duplicated hide-list produces: one panel shown
    without the others actually hiding. Photos stopped being a section in 3.5,
    so the guard moved down a level with it — and it is still needed there."""
    signed_in()
    open_create(page, "photo")
    expect(page.locator("#create-post-panel")).to_be_hidden()
    expect(page.locator("#create-video-panel")).to_be_hidden()
    open_create(page, "post")
    expect(page.locator("#create-photo-panel")).to_be_hidden()


def test_an_empty_library_shows_an_empty_state_not_a_blank_grid(page, signed_in):
    signed_in()
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    open_create(page, "photo")
    expect(page.locator("#library-image-grid")).to_contain_text("Nothing here yet")


def test_the_grid_renders_a_card_per_asset(page, signed_in):
    signed_in()
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps([_asset(), _asset(id="22222222-2222-4222-8222-222222222222")])))
    open_create(page, "photo")
    expect(page.locator("#library-image-grid img")).to_have_count(2)


def test_a_pending_asset_shows_a_placeholder_not_a_broken_image(page, signed_in):
    signed_in()
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps([_asset(status="pending", url=None)])))
    open_create(page, "photo")
    expect(page.locator("#library-image-grid")).to_contain_text("Generating…")
    expect(page.locator("#library-image-grid img")).to_have_count(0)


def test_a_failed_asset_shows_its_error(page, signed_in):
    signed_in()
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps([_asset(status="failed", url=None,
                                error="The provider rejected the key.")])))
    open_create(page, "photo")
    expect(page.locator("#library-image-grid")).to_contain_text("rejected the key")


def test_a_short_prompt_never_reaches_the_server(page, signed_in):
    signed_in()
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "media/images" in r.url else None)
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    open_create(page, "photo")

    page.locator("#lib-img-prompt").fill("ok")
    page.locator("#lib-img-generate-btn").click()
    expect(page.locator("#lib-img-status")).to_contain_text("at least 3 characters")
    assert calls == []


def test_generating_without_a_key_offers_the_way_to_set_one_up(page, signed_in):
    """A guard error about the provider/model must not just sit in a status
    line — it has to point at Account, the same as the composer's own guard."""
    signed_in()
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.route("**/api/media/images", lambda r: r.fulfill(
        status=400, content_type="application/json",
        body=json.dumps({"detail": "No image provider configured. Choose one in Account → AI models."})))
    open_create(page, "photo")

    page.locator("#lib-img-prompt").fill("a cat on a windowsill")
    page.locator("#lib-img-generate-btn").click()
    expect(page.locator("#need-key-modal")).to_be_visible()
    expect(page.locator("#need-key-msg")).to_contain_text("Account")


def test_a_provider_error_is_shown_inline_not_swallowed(page, signed_in):
    signed_in()
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.route("**/api/media/images", lambda r: r.fulfill(
        status=502, content_type="application/json",
        body=json.dumps({"detail": "The provider rejected the key."})))
    open_create(page, "photo")

    page.locator("#lib-img-prompt").fill("a cat on a windowsill")
    page.locator("#lib-img-generate-btn").click()
    expect(page.locator("#lib-img-status")).to_contain_text("rejected the key")


def test_a_successful_generation_clears_the_prompt_and_refreshes_the_grid(page, signed_in):
    signed_in()
    calls = {"n": 0}

    def _list(route):
        calls["n"] += 1
        body = [] if calls["n"] == 1 else [_asset()]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/media?kind=image", _list)
    page.route("**/api/media/images", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(_asset())))
    open_create(page, "photo")
    expect(page.locator("#library-image-grid")).to_contain_text("Nothing here yet")

    page.locator("#lib-img-prompt").fill("a cat on a windowsill")
    page.locator("#lib-img-generate-btn").click()

    expect(page.locator("#lib-img-prompt")).to_have_value("")
    expect(page.locator("#library-image-grid img")).to_have_count(1)


def test_deleting_a_card_asks_first_then_removes_it(page, signed_in):
    signed_in()
    deleted = []

    def _list(route):
        body = [] if deleted else [_asset()]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/media?kind=image", _list)
    page.route("**/api/media/11111111-1111-4111-8111-111111111111",
              lambda r: (deleted.append(1), r.fulfill(status=204))[1]
              if r.request.method == "DELETE" else r.fallback())
    open_create(page, "photo")
    expect(page.locator("#library-image-grid img")).to_have_count(1)

    page.on("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Delete").click()
    expect(page.locator("#library-image-grid")).to_contain_text("Nothing here yet")


def test_declining_the_confirm_leaves_the_card(page, signed_in):
    signed_in()
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps([_asset()])))
    calls = []
    page.on("request", lambda r: calls.append(r.url)
            if r.method == "DELETE" and "media/" in r.url else None)
    open_create(page, "photo")

    page.on("dialog", lambda d: d.dismiss())
    page.get_by_role("button", name="Delete").click()
    assert calls == []
    expect(page.locator("#library-image-grid img")).to_have_count(1)
