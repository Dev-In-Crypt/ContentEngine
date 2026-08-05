"""The Video library tab, driven by a real browser.

Fakes are built from the server's own response models
(`MediaAssetDetail`/`MediaAssetSummary`) where one exists — same discipline as
test_media_library_e2e.py. `GET /api/models/providers` has no response_model
(it returns a plain dict straight from services.ai.catalog.list_video_providers()),
so its fake is hand-built from that function's known, hand-written shape
instead — there is no schema to build it from, not a shortcut taken here.
"""
import json

import pytest
from playwright.sync_api import expect

from models.schemas import MediaAssetDetail

from tests.e2e.nav import open_create

pytestmark = pytest.mark.e2e

_VIDEO_ID = "33333333-3333-4333-8333-333333333333"
_IMAGE_ID = "11111111-1111-4111-8111-111111111111"


def _video_asset(**over) -> dict:
    fields = dict(
        id=_VIDEO_ID, kind="video", status="ready", source="ai_gen",
        url=f"/api/media/{_VIDEO_ID}/file",
        title="a cat walking on a windowsill", duration_sec=5.0, bytes=999999,
        created_at="2026-07-30T00:00:00Z",
        provider="kling", model="kling-v1-6",
        prompt="a cat walking on a windowsill",
    )
    fields.update(over)
    return MediaAssetDetail(**fields).model_dump(mode="json")


def _image_asset(**over) -> dict:
    fields = dict(
        id=_IMAGE_ID, kind="image", status="ready", source="ai_gen",
        url=f"/api/media/{_IMAGE_ID}/file", title="a windowsill", bytes=1234,
        created_at="2026-07-30T00:00:00Z",
    )
    fields.update(over)
    return MediaAssetDetail(**fields).model_dump(mode="json")


def _providers_body() -> dict:
    """The exact shape services.ai.catalog.list_video_providers() produces —
    not response_model-validated by FastAPI, so there is no schema to build
    this from; this mirrors that function's known fields instead."""
    return {
        "text": [], "image": [],
        "video": [{
            "key": "kling", "label": "Kling", "hint": "Billed per second.",
            "key_field": "kling_api_key", "key_url": "https://kling.ai/dev/api-key",
            "models": [
                {"id": "kling-v1-6", "label": "Kling 1.6 (default)", "price_per_sec": 0.075},
                {"id": "kling-v3-0-turbo", "label": "Kling 3.0 Turbo", "price_per_sec": 0.106},
            ],
        }],
    }


def _route_catalog(page):
    page.route("**/api/models/providers", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(_providers_body())))


def test_the_video_tab_is_hidden_from_a_business_account(page, signed_in):
    signed_in(account_type="business")
    expect(page.locator('[data-section="library-video"]')).to_be_hidden()


def test_switching_to_video_hides_every_other_view(page, signed_in):
    signed_in()
    _route_catalog(page)
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    open_create(page, "video")
    expect(page.locator("#view-create")).to_be_hidden()
    open_create(page, "post")
    expect(page.locator("#view-library-video")).to_be_hidden()


def test_an_empty_library_shows_an_empty_state(page, signed_in):
    signed_in()
    _route_catalog(page)
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    open_create(page, "video")
    expect(page.locator("#library-video-grid")).to_contain_text("Nothing here yet")


def test_the_model_dropdown_is_populated_from_the_catalog(page, signed_in):
    signed_in()
    _route_catalog(page)
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    open_create(page, "video")
    expect(page.locator("#lib-vid-model option")).to_have_count(2)
    expect(page.locator("#lib-vid-model")).to_contain_text("Kling 3.0 Turbo")


def test_the_cost_estimate_scales_with_duration(page, signed_in):
    signed_in()
    _route_catalog(page)
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    open_create(page, "video")
    page.locator("#lib-vid-duration").select_option("5")
    expect(page.locator("#lib-vid-cost")).to_contain_text("$")
    five_sec_text = page.locator("#lib-vid-cost").inner_text()

    page.locator("#lib-vid-duration").select_option("10")
    expect(page.locator("#lib-vid-cost")).not_to_have_text(five_sec_text)
    expect(page.locator("#lib-vid-cost")).to_contain_text("0.75")   # 10 * 0.075, no rounding ambiguity


def test_a_short_prompt_never_reaches_the_server(page, signed_in):
    signed_in()
    _route_catalog(page)
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "media/videos" in r.url else None)
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    open_create(page, "video")

    page.locator("#lib-vid-prompt").fill("ok")
    page.locator("#lib-vid-generate-btn").click()
    expect(page.locator("#lib-vid-status")).to_contain_text("at least 3 characters")
    assert calls == []


def test_generating_without_a_key_offers_the_way_to_set_one_up(page, signed_in):
    signed_in()
    _route_catalog(page)
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.route("**/api/media/videos", lambda r: r.fulfill(
        status=400, content_type="application/json",
        body=json.dumps({"detail": "No Kling key configured. Add one in Account → API keys."})))
    open_create(page, "video")

    page.locator("#lib-vid-prompt").fill("a cat walking on a windowsill")
    page.locator("#lib-vid-generate-btn").click()
    expect(page.locator("#need-key-modal")).to_be_visible()
    expect(page.locator("#need-key-msg")).to_contain_text("Account")


def test_a_provider_error_is_shown_inline(page, signed_in):
    signed_in()
    _route_catalog(page)
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.route("**/api/media/videos", lambda r: r.fulfill(
        status=502, content_type="application/json",
        body=json.dumps({"detail": "Kling rejected the request."})))
    open_create(page, "video")

    page.locator("#lib-vid-prompt").fill("a cat walking on a windowsill")
    page.locator("#lib-vid-generate-btn").click()
    expect(page.locator("#lib-vid-status")).to_contain_text("rejected the request")


def test_a_successful_generation_clears_the_prompt_and_refreshes_the_grid(page, signed_in):
    signed_in()
    _route_catalog(page)
    calls = {"n": 0}

    def _list(route):
        calls["n"] += 1
        body = [] if calls["n"] == 1 else [_video_asset(status="pending", url=None)]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/media?kind=video", _list)
    page.route("**/api/media/videos", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(_video_asset(status="pending", url=None))))
    open_create(page, "video")
    expect(page.locator("#library-video-grid")).to_contain_text("Nothing here yet")

    page.locator("#lib-vid-prompt").fill("a cat walking on a windowsill")
    page.locator("#lib-vid-generate-btn").click()

    expect(page.locator("#lib-vid-prompt")).to_have_value("")
    expect(page.locator("#library-video-grid")).to_contain_text("Generating…")


def test_a_ready_video_renders_a_video_element_not_an_image(page, signed_in):
    signed_in()
    _route_catalog(page)
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps([_video_asset()])))
    open_create(page, "video")
    expect(page.locator("#library-video-grid video")).to_have_count(1)
    expect(page.locator("#library-video-grid img")).to_have_count(0)


def test_suggest_idea_fills_the_prompt(page, signed_in):
    signed_in()
    _route_catalog(page)
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.route("**/api/media/videos/suggest-idea", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"prompt": "A paper boat drifting down a rain-soaked gutter."})))
    open_create(page, "video")

    page.locator("#lib-vid-idea-btn").click()
    expect(page.locator("#lib-vid-prompt")).to_have_value(
        "A paper boat drifting down a rain-soaked gutter.")


def test_animate_a_photo_picks_a_seed_without_any_network_call(page, signed_in):
    """Picking a seed photo is a purely client-side choice — nothing is
    inserted or attached until Generate is actually clicked."""
    signed_in()
    _route_catalog(page)
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps([_image_asset()])))
    calls = []
    page.on("request", lambda r: calls.append(r.url)
            if r.method in ("POST", "PUT") and "from-library" in r.url else None)
    open_create(page, "video")

    page.locator("#lib-vid-seed-btn").click()
    expect(page.locator("#library-picker-modal")).to_be_visible()
    page.locator("#library-picker-grid button").first.click()

    expect(page.locator("#library-picker-modal")).to_be_hidden()
    expect(page.locator("#lib-vid-seed-preview")).to_be_visible()
    assert calls == []


def test_clearing_the_seed_hides_the_preview(page, signed_in):
    signed_in()
    _route_catalog(page)
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps([_image_asset()])))
    open_create(page, "video")

    page.locator("#lib-vid-seed-btn").click()
    page.locator("#library-picker-grid button").first.click()
    expect(page.locator("#lib-vid-seed-preview")).to_be_visible()

    page.locator("#lib-vid-seed-preview button").click()
    expect(page.locator("#lib-vid-seed-preview")).to_be_hidden()
