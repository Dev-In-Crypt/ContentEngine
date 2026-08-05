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


# ------------------------------------------------------------------ composer: the video card
#
# The card is `#reel-card`. It is shown on BOTH networks — its render button
# turns the post's slides into an MP4, and each network has somewhere to send
# that MP4: Instagram publish-reel, X publish-video. It hid on X until the fix
# below, which is what made publishReelOrToX's X branch unreachable: the button
# that dispatches to it lives inside the card.
#
# The network tab and the post's platform are deliberately crossed wherever it
# matters: what the card follows is the POST's own platform, and a test where
# the two agree cannot tell that apart from following the tab.

_PIXEL = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def _composer_post(post_id: str, platform: str, *, slides: bool = True) -> dict:
    from datetime import datetime, timezone

    from models.schemas import PostPreview, SlidePreview

    return PostPreview(
        id=post_id, topic="t", format="single", status="preview",
        caption="c", hashtags=[], seo_keywords=[], cta="c", hook="h",
        platform=platform,
        slides=[SlidePreview(slide_number=1,
                             image_url=f"/api/posts/{post_id}/slides/1/image",
                             image_source="stock", width=1080, height=1350)]
        if slides else [],
        text_model_used="m", image_model_used=None,
        created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    ).model_dump(mode="json")


def _reach_step4(page, post: dict) -> None:
    """Generate `post` through the wizard and land on the editor."""
    import base64

    from models.schemas import AISettingsResponse

    _route_jobs(page, [])
    page.route("**/api/settings/ai", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(AISettingsResponse(
            text_provider="openrouter", text_model="anthropic/claude-sonnet-5",
            image_provider="openrouter", image_model="google/gemini-image",
            keys={"openrouter": {"set": True, "masked": "sk-…9f2c"}},
        ).model_dump(mode="json"))))
    page.route("**/slides/*/image", lambda r: r.fulfill(
        status=200, content_type="image/png", body=base64.b64decode(_PIXEL)))
    page.route("**/api/posts/generate", lambda r: r.fulfill(
        status=200, content_type="text/event-stream",
        body="data: " + json.dumps({"type": "complete", "post": post}) + "\n\n"))

    page.locator("#topic").fill("Sourdough starters")
    page.get_by_role("button", name="Next →").click()
    page.locator("#generate-btn").click()
    expect(page.locator("#step-4")).to_be_visible()


def _record_publish_routes(page) -> list:
    """Route both publish endpoints, recording which one the SPA reaches for.

    The two answer differently on purpose, because the SPA reads the bodies:
    Instagram publishes in-request, X returns 202 and a job the poller drives.
    """
    hit: list[str] = []

    def _reel(route):
        hit.append("reel")
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"success": True, "instagram_media_id": "m1"}))

    def _video(route):
        hit.append("video")
        # .../api/posts/<id>/publish-video — the job has to name the post it is
        # for, or the SPA files it under `undefined` and the button never
        # switches to the job's status.
        post_id = route.request.url.rsplit("/", 2)[-2]
        route.fulfill(status=202, content_type="application/json",
                      body=json.dumps(_job(status="queued", post_id=post_id,
                                           asset_id=None, progress_pct=0)))

    page.route("**/api/posts/*/publish-video", _video)
    page.route("**/api/posts/*/publish-reel", _reel)
    return hit


def _render_a_reel(page, post_id: str) -> None:
    """Fake the render so #reel-preview — and the publish button inside it —
    appears without staging a real ffmpeg run."""
    page.route(f"**/api/posts/{post_id}/reel", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"video_url": f"/api/posts/{post_id}/reel/video?t=1",
                         "broll_fallbacks": 0, "broll_clips": 0})))
    page.locator("#make-reel-btn").click()
    expect(page.locator("#reel-preview")).to_be_visible()


def test_the_video_card_is_offered_on_an_x_post(page, signed_in):
    """The regression this file's composer half exists for.

    renderPreview hid #reel-card whenever the post wasn't Instagram's, so an X
    post had no way to reach publishVideoToX — the backend route, the job table
    and the poller all worked and nothing in the UI could start them.
    """
    signed_in()
    _reach_step4(page, _composer_post("e2e-post-x", "x"))
    expect(page.locator("#reel-card")).to_be_visible()


def test_a_text_only_post_still_has_no_video_card(page, signed_in):
    """Slides are the render's input; without them the card is an error waiting
    to happen (POST /reel 400s with "No slides to build a reel from")."""
    signed_in()
    _reach_step4(page, _composer_post("e2e-post-x", "x", slides=False))
    expect(page.locator("#reel-card")).to_be_hidden()


def test_the_card_copy_follows_the_posts_platform(page, signed_in):
    """Mutation guard: hard-code the Instagram labels and an X post would offer
    'Publish Reel', which hits the Instagram-only publish-reel route and 400s.
    "Reel" is also Instagram's word — X just takes an MP4."""
    signed_in()
    _reach_step4(page, _composer_post("e2e-post-x", "x"))
    expect(page.locator("#make-reel-btn")).to_have_text("Make video")
    expect(page.locator("#reel-publish-btn")).to_have_text("𝕏 Publish video to X")


def test_an_instagram_post_keeps_the_reel_wording(page, signed_in):
    signed_in()
    page.locator("#net-x").click()
    _reach_step4(page, _composer_post("e2e-post-ig", "instagram"))
    expect(page.locator("#make-reel-btn")).to_have_text("Make Reel")
    expect(page.locator("#reel-publish-btn")).to_have_text("📤 Publish Reel")


def test_clicking_publish_on_an_x_post_reaches_the_video_route(page, signed_in):
    """The whole point of the fix: a real click, on the real button, from the
    composer. Asserting the dispatch by calling publishReelOrToX() directly —
    as this file used to have to — passes just as happily when the button the
    user needs is unreachable."""
    signed_in()
    _reach_step4(page, _composer_post("e2e-post-x", "x"))
    hit = _record_publish_routes(page)
    _render_a_reel(page, "e2e-post-x")

    page.on("dialog", lambda d: d.accept())
    with page.expect_response("**/api/posts/*/publish-video"):
        page.locator("#reel-publish-btn").click()

    assert hit == ["video"], f"an X post went to {hit or 'nowhere'}"


def test_clicking_publish_on_an_instagram_post_reaches_the_reel_route(page, signed_in):
    """The mirror, and the half that did damage before 3.2: publishReelOrToX
    dispatched on the active network TAB, so this exact pair — tab on X, post
    on Instagram — sent an Instagram post to X's endpoint.

    It is also the one crossed pair that was reachable back then, because the
    card hid on X posts. Now that it doesn't, the click is a real one on both
    sides of the dispatch rather than a page.evaluate() standing in for it.
    """
    signed_in()
    page.locator("#net-x").click()          # the composer is aimed at X…
    _reach_step4(page, _composer_post("e2e-post-ig", "instagram"))  # …the post isn't
    hit = _record_publish_routes(page)
    _render_a_reel(page, "e2e-post-ig")

    page.on("dialog", lambda d: d.accept())
    with page.expect_response("**/api/posts/*/publish-reel"):
        page.locator("#reel-publish-btn").click()

    assert hit == ["reel"], f"an Instagram post went to {hit or 'nowhere'}"
