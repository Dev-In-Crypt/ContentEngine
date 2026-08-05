"""Publish-to-X UI (Phase 8): the button/badge on a video card, and the
composer's platform-aware Reel-card button. Fakes are built from the server's
own response models (MediaAssetDetail / VideoPublishJobStatus), same
discipline as test_video_library_e2e.py.
"""
import json

import pytest
from playwright.sync_api import expect

from models.schemas import MediaAssetDetail, VideoPublishJobStatus

from tests.e2e.nav import open_create

pytestmark = pytest.mark.e2e

_READY_ID = "33333333-3333-4333-8333-333333333333"
_PENDING_ID = "44444444-4444-4444-8444-444444444444"


def _video_asset(asset_id=_READY_ID, **over) -> dict:
    fields = dict(
        id=asset_id, kind="video", status="ready", source="ai_gen",
        url=f"/api/media/{asset_id}/file",
        title="a cat walking on a windowsill", duration_sec=5.0, bytes=999999,
        created_at="2026-08-03T00:00:00Z",
        provider="kling", model="kling-v1-6",
        prompt="a cat walking on a windowsill",
    )
    fields.update(over)
    return MediaAssetDetail(**fields).model_dump(mode="json")


def _job(job_id="job1", **over) -> dict:
    fields = dict(
        id=job_id, platform="x", status="uploading",
        post_id=None, asset_id=_READY_ID, tweet_id=None, permalink=None,
        error=None, progress_pct=40, warning=None,
        created_at="2026-08-03T00:00:00Z", updated_at="2026-08-03T00:00:00Z",
    )
    fields.update(over)
    return VideoPublishJobStatus(**fields).model_dump(mode="json")


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


def _route_jobs(page, jobs):
    page.route("**/api/publish-jobs", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(jobs)))


def test_the_publish_to_x_button_shows_on_a_ready_video_card(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [_video_asset()])
    _route_jobs(page, [])
    open_create(page, "video")
    expect(page.locator("#library-video-grid button:has-text('Publish to X')")).to_have_count(1)


def test_the_button_is_absent_on_a_pending_card(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [_video_asset(_PENDING_ID, status="pending", url=None)])
    _route_jobs(page, [])
    open_create(page, "video")
    expect(page.locator("#library-video-grid button:has-text('Publish to X')")).to_have_count(0)


def test_missing_x_credentials_opens_the_needKey_prompt(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [_video_asset()])
    _route_jobs(page, [])
    page.route(f"**/api/media/{_READY_ID}/publish-x", lambda r: r.fulfill(
        status=400, content_type="application/json",
        body=json.dumps({"detail": "X (Twitter) API credentials not configured"})))
    open_create(page, "video")

    page.locator("#library-video-grid button:has-text('Publish to X')").click()
    expect(page.locator("#publish-x-modal")).to_be_visible()
    page.locator("#publish-x-text").fill("Check this out.")
    page.locator("#publish-x-submit-btn").click()

    expect(page.locator("#need-key-modal")).to_be_visible()
    expect(page.locator("#need-key-msg")).to_contain_text("X (Twitter)")


def test_a_successful_queue_closes_the_modal(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [_video_asset()])
    _route_jobs(page, [])   # nothing in flight yet — the click is what creates the job
    page.route(f"**/api/media/{_READY_ID}/publish-x", lambda r: r.fulfill(
        status=202, content_type="application/json", body=json.dumps(_job(status="queued"))))
    open_create(page, "video")

    page.locator("#library-video-grid button:has-text('Publish to X')").click()
    page.locator("#publish-x-text").fill("Check this out.")
    page.locator("#publish-x-submit-btn").click()

    expect(page.locator("#publish-x-modal")).to_be_hidden()


def test_a_finished_job_shows_a_view_on_x_link(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [_video_asset()])
    _route_jobs(page, [_job(status="published", tweet_id="tw1",
                            permalink="https://x.com/i/web/status/tw1")])
    open_create(page, "video")

    link = page.locator("#library-video-grid a:has-text('View on X')")
    expect(link).to_be_visible()
    expect(link).to_have_attribute("href", "https://x.com/i/web/status/tw1")


def test_a_failed_job_shows_a_retry_button(page, signed_in):
    signed_in()
    _route_catalog(page)
    _route_video_list(page, [_video_asset()])
    _route_jobs(page, [_job(status="failed", error="X rejected the video")])
    open_create(page, "video")

    expect(page.locator("#library-video-grid button:has-text('Retry')")).to_be_visible()


# ------------------------------------------------------------------ composer: platform-aware button

def test_the_reel_button_label_follows_the_posts_platform(page, signed_in):
    """Mutation guard: hard-code the Instagram label and an X post would offer
    'Publish Reel', which hits the Instagram-only publish-reel route and 400s.

    The tab is deliberately left on Instagram. What the button follows is the
    POST's own platform — the docstring used to say the opposite, and saying it
    was what made the bug look intended: the chrome read the tab, so opening an
    X post from the calendar while the tab sat on Instagram offered the wrong
    button and, worse, sent the wrong endpoint.
    """
    import base64
    from datetime import datetime, timezone

    from models.schemas import AISettingsResponse, PostPreview, SlidePreview

    signed_in()
    _route_jobs(page, [])
    page.route("**/api/settings/ai", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(AISettingsResponse(
            text_provider="openrouter", text_model="anthropic/claude-sonnet-5",
            image_provider="openrouter", image_model="google/gemini-image",
            keys={"openrouter": {"set": True, "masked": "sk-…9f2c"}},
        ).model_dump(mode="json"))))
    pixel = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    page.route("**/slides/*/image", lambda r: r.fulfill(
        status=200, content_type="image/png", body=pixel))

    def _post(platform):
        return PostPreview(
            id="e2e-post-x", topic="t", format="single", status="preview",
            caption="c", hashtags=[], seo_keywords=[], cta="c", hook="h",
            platform=platform,
            slides=[SlidePreview(slide_number=1, image_url="/api/posts/e2e-post-x/slides/1/image",
                                 image_source="stock", width=1080, height=1350)],
            text_model_used="m", image_model_used=None,
            created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        ).model_dump(mode="json")

    page.route("**/api/posts/generate", lambda r: r.fulfill(
        status=200, content_type="text/event-stream",
        body="".join(f"data: {json.dumps(f)}\n\n" for f in
                     [{"type": "complete", "post": _post("x")}])))

    page.locator("#topic").fill("Sourdough starters")
    page.get_by_role("button", name="Next →").click()
    page.locator("#generate-btn").click()
    expect(page.locator("#step-4")).to_be_visible()

    expect(page.locator("#reel-publish-btn")).to_have_text("𝕏 Publish video to X")


def test_the_reel_button_never_sends_an_instagram_post_to_x(page, signed_in):
    """The label is cosmetic; this is the half that did damage.

    publishReelOrToX dispatched on the active network TAB. The Reel card is
    hidden for X posts, so the reachable combination was the mirror of what you
    would guess: tab on X, post on Instagram, card visible, button offering
    "Publish video to X" — and clicking it sent an INSTAGRAM post to the X
    endpoint. Following the post instead makes that impossible.
    """
    import base64
    from datetime import datetime, timezone

    from models.schemas import AISettingsResponse, PostPreview, SlidePreview

    signed_in()
    _route_jobs(page, [])
    page.locator("#net-toggle-x").click()   # the composer is aimed at X…
    page.route("**/api/settings/ai", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(AISettingsResponse(
            text_provider="openrouter", text_model="anthropic/claude-sonnet-5",
            image_provider="openrouter", image_model="google/gemini-image",
            keys={"openrouter": {"set": True, "masked": "sk-…9f2c"}},
        ).model_dump(mode="json"))))
    pixel = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    page.route("**/slides/*/image", lambda r: r.fulfill(
        status=200, content_type="image/png", body=pixel))

    # …but the post that comes back is an Instagram one.
    post = PostPreview(
        id="e2e-post-ig", topic="t", format="single", status="preview",
        caption="c", hashtags=[], seo_keywords=[], cta="c", hook="h",
        platform="instagram", video_url="/api/posts/e2e-post-ig/reel/video",
        slides=[SlidePreview(slide_number=1,
                             image_url="/api/posts/e2e-post-ig/slides/1/image",
                             image_source="stock", width=1080, height=1350)],
        text_model_used="m", image_model_used=None,
        created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    ).model_dump(mode="json")
    page.route("**/api/posts/generate", lambda r: r.fulfill(
        status=200, content_type="text/event-stream",
        body="data: " + json.dumps({"type": "complete", "post": post}) + "\n\n"))

    hit = []

    def _record(name):
        # A one-argument handler on purpose: Playwright passes the Request as a
        # second argument when the handler takes one, which would clobber a
        # `name=name` default and record the request instead of the label.
        def handler(route):
            hit.append(name)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"success": True, "instagram_media_id": "m1"}))
        return handler

    page.route("**/api/posts/*/publish-video", _record("video"))
    page.route("**/api/posts/*/publish-reel", _record("reel"))

    page.locator("#topic").fill("Sourdough starters")
    page.get_by_role("button", name="Next →").click()
    page.locator("#generate-btn").click()
    expect(page.locator("#step-4")).to_be_visible()
    expect(page.locator("#reel-publish-btn")).to_have_text("📤 Publish Reel")

    # Called directly rather than clicked: #reel-preview, which holds the
    # button, stays hidden until a reel has actually been rendered, and staging
    # a real ffmpeg render here would test everything except the dispatch.
    page.on("dialog", lambda d: d.accept())
    with page.expect_response("**/api/posts/*/publish-*"):
        page.evaluate("publishReelOrToX()")
    assert hit == ["reel"], f"an Instagram post went to {hit or 'nowhere'}"
