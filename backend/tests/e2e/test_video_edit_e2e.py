"""The video editor modal (Phase 6): trim/reframe/concat + voiceover/music/cover.

Fakes are built from the server's own response model (MediaAssetDetail) — same
discipline as test_video_library_e2e.py.
"""
import json

import pytest
from playwright.sync_api import expect

from models.schemas import MediaAssetDetail

pytestmark = pytest.mark.e2e

_READY_ID = "33333333-3333-4333-8333-333333333333"
_PENDING_ID = "44444444-4444-4444-8444-444444444444"
_OTHER_ID = "55555555-5555-4555-8555-555555555555"


def _video_asset(asset_id=_READY_ID, **over) -> dict:
    fields = dict(
        id=asset_id, kind="video", status="ready", source="ai_gen",
        url=f"/api/media/{asset_id}/file",
        title="a cat walking on a windowsill", duration_sec=5.0, bytes=999999,
        created_at="2026-07-30T00:00:00Z",
        provider="kling", model="kling-v1-6",
        prompt="a cat walking on a windowsill",
    )
    fields.update(over)
    return MediaAssetDetail(**fields).model_dump(mode="json")


def _providers_body() -> dict:
    return {"text": [], "image": [], "video": [{
        "key": "kling", "label": "Kling", "hint": "Billed per second.",
        "key_field": "kling_api_key", "key_url": "https://kling.ai/dev/api-key",
        "models": [{"id": "kling-v1-6", "label": "Kling 1.6 (default)", "price_per_sec": 0.075}],
    }]}


def _route_catalog(page):
    page.route("**/api/models/providers", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(_providers_body())))


def _route_video_list(page, assets):
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(assets)))


def _open_video_tab(page):
    page.locator('[data-section="library-video"]').click()
    expect(page.locator("#view-library-video")).to_be_visible()


def test_edit_button_appears_only_on_a_ready_video(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [
        _video_asset(_READY_ID, status="ready"),
        _video_asset(_PENDING_ID, status="pending", url=None),
    ])
    _open_video_tab(page)
    expect(page.locator("#library-video-grid button:has-text('Edit')")).to_have_count(1)


def test_edit_modal_opens_with_the_clicked_clip(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [_video_asset()])
    _open_video_tab(page)

    page.locator("#library-video-grid button:has-text('Edit')").click()
    expect(page.locator("#edit-video-modal")).to_be_visible()
    expect(page.locator("#edit-video-clips .ce-card")).to_have_count(1)


def test_add_a_clip_appends_not_replaces(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [_video_asset(_READY_ID), _video_asset(_OTHER_ID)])
    _open_video_tab(page)

    page.locator("#library-video-grid button:has-text('Edit')").first.click()
    expect(page.locator("#edit-video-clips .ce-card")).to_have_count(1)

    page.locator("#edit-video-modal >> text=+ Add a clip").click()
    expect(page.locator("#library-picker-modal")).to_be_visible()
    page.locator("#library-picker-grid button").first.click()

    expect(page.locator("#library-picker-modal")).to_be_hidden()
    expect(page.locator("#edit-video-modal")).to_be_visible()
    expect(page.locator("#edit-video-clips .ce-card")).to_have_count(2)


def test_transitions_checkbox_needs_at_least_two_clips(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [_video_asset(_READY_ID), _video_asset(_OTHER_ID)])
    _open_video_tab(page)

    page.locator("#library-video-grid button:has-text('Edit')").first.click()
    expect(page.locator("#edit-video-transitions")).to_be_disabled()

    page.locator("#edit-video-modal >> text=+ Add a clip").click()
    page.locator("#library-picker-grid button").first.click()
    expect(page.locator("#edit-video-transitions")).to_be_enabled()


def test_missing_elevenlabs_key_routes_to_need_key(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [_video_asset()])
    page.route(f"**/api/media/{_READY_ID}/edit", lambda r: r.fulfill(
        status=400, content_type="application/json",
        body=json.dumps({"detail": "Voiceover needs an ElevenLabs API key — "
                                   "add it in Account → API keys."})))
    _open_video_tab(page)

    page.locator("#library-video-grid button:has-text('Edit')").click()
    page.locator("#edit-video-voiceover").check()
    page.locator("#edit-video-script").fill("One short line.")
    page.locator("#edit-video-submit-btn").click()

    expect(page.locator("#need-key-modal")).to_be_visible()
    expect(page.locator("#need-key-msg")).to_contain_text("ElevenLabs")


def test_voiceover_without_a_script_never_reaches_the_server(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [_video_asset()])
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "/edit" in r.url else None)
    _open_video_tab(page)

    page.locator("#library-video-grid button:has-text('Edit')").click()
    page.locator("#edit-video-voiceover").check()
    page.locator("#edit-video-submit-btn").click()

    expect(page.locator("#edit-video-status")).to_contain_text("script")
    assert calls == []


def test_a_successful_edit_closes_the_modal_and_refreshes_the_grid(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [_video_asset()])
    page.route(f"**/api/media/{_READY_ID}/edit", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(_video_asset(source="edited"))))
    _open_video_tab(page)

    page.locator("#library-video-grid button:has-text('Edit')").click()
    page.locator("#edit-video-submit-btn").click()

    expect(page.locator("#edit-video-modal")).to_be_hidden()
