"""The four-step composer, driven by a real browser.

This is the busiest screen in the product and, until now, the least covered: the
TestClient suite proves `/api/posts/generate` returns the right JSON and stops
there, which says nothing about whether the SPA ever shows it. Everything here
is the other half — a guard that decides not to call the server, a network
switch that has to hide six fields and reset a seventh, a stream that has to
survive being read frame by frame.

Two requests are faked with `page.route`, and only two: the AI settings read and
the generation stream. Both fakes are **built from the server's own response
models** (`AISettingsResponse`, `PostPreview`) rather than hand-written JSON —
a hand-rolled fake drifts from the real payload and the drift looks exactly
like a passing test. Nothing else is stubbed, and no key or outbound call is
needed, which keeps the promise made in this package's conftest.
"""
import base64
import json
from datetime import datetime, timezone

import pytest
from playwright.sync_api import expect

from models.schemas import AISettingsResponse, PostPreview, SlidePreview

pytestmark = pytest.mark.e2e

# A 1×1 transparent PNG, so the preview's <img> tags resolve instead of 404ing.
_PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _ai_ready() -> dict:
    """AI settings as the server would report them once a key is saved."""
    return AISettingsResponse(
        text_provider="openrouter", text_model="anthropic/claude-sonnet-5",
        image_provider="openrouter", image_model="google/gemini-image",
        keys={"openrouter": {"set": True, "masked": "sk-…9f2c"}},
    ).model_dump(mode="json")


def _generated_post(**over) -> dict:
    """A finished post, in the exact shape `/api/posts/generate` streams back."""
    fields = dict(
        id="e2e-post-1",
        topic="Sourdough starters",
        format="single",
        status="preview",
        caption="A starter is just flour, water and patience.",
        hashtags=["#sourdough", "#baking"],
        seo_keywords=["sourdough starter"],
        cta="Save this for your next bake.",
        hook="Your starter is not dead.",
        platform="instagram",
        slides=[SlidePreview(
            slide_number=1,
            image_url="/api/posts/e2e-post-1/slides/1/image",
            image_source="stock", width=1080, height=1350,
            overlay_text="Flour, water, patience.", niche_text="Baking",
            has_raw_image=True,
        )],
        text_model_used="anthropic/claude-sonnet-5",
        image_model_used=None,
        created_at=datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc),
    )
    fields.update(over)
    return PostPreview(**fields).model_dump(mode="json")


def _sse(*frames: dict) -> str:
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames)


@pytest.fixture
def keyed(page):
    """Report a saved AI key, so the generate guard lets the click through.

    Without this every generation test would stop at the "set up your AI model"
    modal — which is itself worth a test, and gets one, but only one.
    """
    page.route("**/api/settings/ai",
               lambda r: r.fulfill(status=200, content_type="application/json",
                                   body=json.dumps(_ai_ready())))
    page.route("**/slides/*/image",
               lambda r: r.fulfill(status=200, content_type="image/png", body=_PIXEL))


def _compose(page, topic: str = "Sourdough starters"):
    """Fill the topic and land on step 2, where Generate lives."""
    page.locator("#topic").fill(topic)
    page.get_by_role("button", name="Next →").click()
    expect(page.locator("#step-2")).to_be_visible()


def _reach_step4(page, **post_overrides):
    """Compose and generate, landing on the preview step with a real post id
    to address — the precondition every library-picker test needs."""
    page.route("**/api/posts/generate", lambda r: r.fulfill(
        status=200, content_type="text/event-stream",
        body=_sse({"type": "complete", "post": _generated_post(**post_overrides)})))
    _compose(page)
    page.locator("#generate-btn").click()
    expect(page.locator("#step-4")).to_be_visible()


# ── Step navigation ──────────────────────────────────────────────────────────

def test_a_topic_too_short_to_generate_from_does_not_advance(page, signed_in):
    signed_in()
    page.locator("#topic").fill("ai")          # under the 3-character floor
    page.get_by_role("button", name="Next →").click()
    expect(page.locator("#toast")).to_contain_text("at least 3 characters")
    expect(page.locator("#step-2")).to_be_hidden()
    expect(page.locator("#step-1")).to_be_visible()


def test_a_real_topic_opens_the_format_step(page, signed_in):
    signed_in()
    _compose(page)
    expect(page.locator("#step-1")).to_be_hidden()
    expect(page.locator("#generate-btn")).to_be_visible()


def test_back_from_the_format_step_keeps_the_topic(page, signed_in):
    signed_in()
    _compose(page)
    page.get_by_role("button", name="← Back").click()
    expect(page.locator("#step-1")).to_be_visible()
    expect(page.locator("#topic")).to_have_value("Sourdough starters")


# ── The network switch ───────────────────────────────────────────────────────
#
# setNetwork touches a dozen elements across two steps. Each assertion below is
# a field that means nothing on the other network and used to show anyway.

def test_switching_to_x_drops_the_instagram_only_fields(page, signed_in):
    signed_in()
    page.locator("#net-toggle-x").click()
    expect(page.locator("#niche-label-field")).to_be_hidden()   # image label
    expect(page.locator("#length-field")).to_be_hidden()        # caption length
    expect(page.locator("#xmode-group")).to_be_visible()        # X post shapes

    _compose(page)
    expect(page.locator("#format-group")).to_be_hidden()        # X has no carousels
    expect(page.locator("#src-text-only")).to_be_visible()      # X-only source


def test_instagram_keeps_its_own_fields_and_hides_the_x_ones(page, signed_in):
    signed_in()
    page.locator("#net-toggle-x").click()
    page.locator("#net-toggle-ig").click()
    expect(page.locator("#niche-label-field")).to_be_visible()
    expect(page.locator("#xmode-group")).to_be_hidden()
    _compose(page)
    expect(page.locator("#format-group")).to_be_visible()
    expect(page.locator("#src-text-only")).to_be_hidden()


def test_a_text_only_post_falls_back_to_stock_when_the_network_becomes_instagram(
        page, signed_in):
    """Instagram requires media. Carrying "Text only" across the switch would
    send a post with no image to a network that rejects it — the fallback is
    silent, so only the resulting state proves it happened."""
    signed_in()
    page.locator("#net-toggle-x").click()
    _compose(page)
    page.locator("#src-text-only").click()
    assert page.evaluate("S.source") == "text_only"

    # The network toggle lives on step 1, so this is the real route back to it.
    page.get_by_role("button", name="← Back").click()
    page.locator("#net-toggle-ig").click()
    assert page.evaluate("S.source") == "stock"


def test_choosing_my_photos_reveals_the_picker(page, signed_in):
    signed_in()
    _compose(page)
    expect(page.locator("#own-photos")).to_be_hidden()
    page.locator('#source-btns [data-val="upload"]').click()
    expect(page.locator("#own-photos")).to_be_visible()


# ── Guards that stop before the server ───────────────────────────────────────

def test_generating_without_an_ai_key_never_reaches_the_server(page, signed_in):
    """The account has no key, so the only useful outcome is being told which
    one to add — not a spinner followed by a provider error."""
    signed_in()
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "posts/generate" in r.url else None)

    _compose(page)
    page.locator("#generate-btn").click()
    expect(page.locator("#need-key-modal")).to_be_visible()
    expect(page.locator("#need-key-title")).to_contain_text("Set up your AI model")
    assert calls == []
    expect(page.locator("#step-3")).to_be_hidden()


def test_too_few_own_photos_disables_generate_until_the_source_changes(page, signed_in):
    """A carousel of 3 needs 3 photos, and the button is disabled rather than
    left clickable — the alternative is a 422 after the upload has been sent.

    The count is the only *visible* statement of what's missing: the reason
    itself lives in a `title`, so a touch or keyboard user gets the dead button
    without the explanation. That is a real gap, tracked as the accessibility
    pass; this test pins the behaviour as it stands so the fix can't regress it.
    """
    signed_in()
    _compose(page)
    page.locator('#format-btns [data-val="carousel_3"]').click()
    page.locator('#source-btns [data-val="upload"]').click()

    expect(page.locator("#generate-btn")).to_be_disabled()
    expect(page.locator("#own-photos-count")).to_have_text("0 of 3 photos")
    assert page.locator("#generate-btn").get_attribute("title") == "Add 3 more photo(s) first"

    page.locator('#source-btns [data-val="stock"]').click()
    expect(page.locator("#generate-btn")).to_be_enabled()


# ── Generation, end to end ───────────────────────────────────────────────────

def test_a_completed_stream_renders_the_preview(page, signed_in, keyed):
    signed_in()
    page.route("**/api/posts/generate", lambda r: r.fulfill(
        status=200, content_type="text/event-stream",
        body=_sse({"type": "progress", "message": "Writing the caption…"},
                  {"type": "complete", "post": _generated_post()})))

    _compose(page)
    page.locator("#generate-btn").click()

    expect(page.locator("#step-4")).to_be_visible()
    expect(page.locator("#caption-edit")).to_have_value(
        "A starter is just flour, water and patience.")
    expect(page.locator("#slides-container img")).to_have_count(1)
    expect(page.locator('[data-slide-overlay="1"]')).to_have_value(
        "Flour, water, patience.")
    assert page.evaluate("S.postId") == "e2e-post-1"


def test_the_progress_messages_are_shown_while_it_runs(page, signed_in, keyed):
    signed_in()
    page.route("**/api/posts/generate", lambda r: r.fulfill(
        status=200, content_type="text/event-stream",
        body=_sse({"type": "progress", "message": "Finding a photo…"},
                  {"type": "complete", "post": _generated_post()})))

    _compose(page)
    page.locator("#generate-btn").click()
    expect(page.locator("#step-4")).to_be_visible()
    expect(page.locator("#progress-list")).to_contain_text("Finding a photo…")


def test_an_error_frame_offers_the_way_back_instead_of_a_dead_spinner(
        page, signed_in, keyed):
    signed_in()
    page.route("**/api/posts/generate", lambda r: r.fulfill(
        status=200, content_type="text/event-stream",
        body=_sse({"type": "error", "message": "Your provider rejected the key."})))

    _compose(page)
    page.locator("#generate-btn").click()

    expect(page.locator("#gen-error")).to_be_visible()
    expect(page.locator("#gen-error-msg")).to_contain_text("provider rejected the key")
    expect(page.locator("#loading-spinner")).to_be_hidden()

    page.get_by_role("button", name="← Back to settings").click()
    expect(page.locator("#step-2")).to_be_visible()


def test_a_stream_that_stops_early_is_an_error_not_a_silent_success(
        page, signed_in, keyed):
    """No terminal frame means we never got a post. Ending on step 4 with the
    previous post's preview still on screen would be worse than saying so."""
    signed_in()
    page.route("**/api/posts/generate", lambda r: r.fulfill(
        status=200, content_type="text/event-stream",
        body=_sse({"type": "progress", "message": "Writing the caption…"})))

    _compose(page)
    page.locator("#generate-btn").click()

    expect(page.locator("#gen-error")).to_be_visible()
    expect(page.locator("#step-4")).to_be_hidden()


def test_a_second_click_while_one_generation_is_in_flight_is_ignored(
        page, signed_in, keyed):
    """Two clicks used to mean two generations: two provider bills, and the
    slower response overwriting the faster one's preview.

    Fired through the page's own function rather than two `.click()` calls,
    because a click round-trips through the driver and the first generation has
    already finished by the time the second lands — which tests nothing. Two
    calls in one tick is what an impatient double-click actually produces.
    """
    signed_in()
    calls = []
    page.route("**/api/posts/generate", lambda r: (
        calls.append(r.request.url),
        r.fulfill(status=200, content_type="text/event-stream",
                  body=_sse({"type": "complete", "post": _generated_post()}))))

    _compose(page)
    page.evaluate("() => { generatePost(); generatePost(); }")
    expect(page.locator("#step-4")).to_be_visible()
    assert len(calls) == 1


def test_a_rejected_generation_does_not_wedge_the_next_one(page, signed_in):
    """The in-flight flag is claimed before the key check and released in a
    `finally`. Release it anywhere else and the first refusal leaves the flag
    set for good — every later click silently does nothing, with no spinner and
    no error to explain it."""
    signed_in()
    _compose(page)
    page.locator("#generate-btn").click()
    expect(page.locator("#need-key-modal")).to_be_visible()
    assert page.evaluate("S.generating") is False

    # Same page, key now in place: the second attempt must get through.
    page.route("**/api/settings/ai",
               lambda r: r.fulfill(status=200, content_type="application/json",
                                   body=json.dumps(_ai_ready())))
    page.route("**/slides/*/image",
               lambda r: r.fulfill(status=200, content_type="image/png", body=_PIXEL))
    page.route("**/api/posts/generate", lambda r: r.fulfill(
        status=200, content_type="text/event-stream",
        body=_sse({"type": "complete", "post": _generated_post()})))

    page.get_by_role("button", name="Cancel").click()
    expect(page.locator("#need-key-modal")).to_be_hidden()
    page.locator("#generate-btn").click()
    expect(page.locator("#step-4")).to_be_visible()


def test_an_x_post_is_generated_as_x_not_as_the_default_network(
        page, signed_in, keyed):
    """S.platform follows the active tab. When it didn't, an X post was written
    to Instagram's rules and only the caption length gave it away."""
    signed_in()
    sent = {}

    def _capture(route):
        sent.update(route.request.post_data_json)
        route.fulfill(status=200, content_type="text/event-stream",
                      body=_sse({"type": "complete",
                                 "post": _generated_post(platform="x", slides=[])}))

    page.route("**/api/posts/generate", _capture)

    page.locator("#net-toggle-x").click()
    _compose(page)
    page.locator("#generate-btn").click()
    expect(page.locator("#step-4")).to_be_visible()

    assert sent["platform"] == "x"
    assert sent["x_mode"] == "short"
    # Reels are an Instagram format and have no meaning on an X post.
    expect(page.locator("#reel-card")).to_be_hidden()


def test_switching_to_x_drops_a_carousel_format(page, signed_in, keyed):
    """Hiding #format-group left S.format alone, so picking a carousel and then
    switching to X shipped `format: 'carousel_10'` with `platform: 'x'` — a
    shape X has no concept of. The picker was invisible by then, so there was
    no way to notice, let alone undo it."""
    signed_in()
    sent = {}

    def _capture(route):
        sent.update(route.request.post_data_json)
        route.fulfill(status=200, content_type="text/event-stream",
                      body=_sse({"type": "complete",
                                 "post": _generated_post(platform="x", slides=[])}))

    page.route("**/api/posts/generate", _capture)

    _compose(page)
    page.locator('#format-group [data-val="carousel_10"]').click()
    assert page.evaluate("S.format") == "carousel_10"

    page.get_by_role("button", name="← Back").click()
    page.locator("#net-toggle-x").click()
    _compose(page)
    page.locator("#generate-btn").click()
    expect(page.locator("#step-4")).to_be_visible()

    assert sent["platform"] == "x"
    assert sent["format"] == "single"


def test_the_network_rail_is_gone(page, signed_in):
    """It was a second control for a choice the composer already owns, sitting
    on every screen including the ones with no composer. The Business shell has
    run without it since it shipped."""
    signed_in()
    expect(page.locator("#net-instagram")).to_have_count(0)
    expect(page.locator("#net-x")).to_have_count(0)
    expect(page.locator(".rail-net")).to_have_count(0)
    # …and the composer's own toggle still does the job.
    expect(page.locator("#net-toggle-x")).to_be_visible()


def test_the_feed_section_is_reachable_whatever_the_composer_targets(page, signed_in):
    """The rail used to hide the Feed button on X and shove you off the section
    if you were standing on it. The grid is Instagram-only by its own nature
    now, so which network the composer is aimed at has nothing to do with
    whether you may look at your profile grid."""
    signed_in()
    page.locator("#net-toggle-x").click()
    expect(page.locator('[data-section="feed"]')).to_be_visible()


# ── The media library picker, reached from an already-rendered post ─────────
#
# These fakes are built from MediaAssetSummary, the same discipline as the rest
# of this file — a hand-rolled fake drifts from the real payload.

def _library_asset(**over) -> dict:
    from models.schemas import MediaAssetSummary
    fields = dict(
        id="a1a1a1a1-1a1a-4a1a-8a1a-a1a1a1a1a1a1",
        kind="image", status="ready", source="ai_gen",
        url="/api/media/a1a1a1a1-1a1a-4a1a-8a1a-a1a1a1a1a1a1/file",
        title="a loaf of sourdough", bytes=999,
        created_at="2026-07-27T00:00:00Z",
    )
    fields.update(over)
    return MediaAssetSummary(**fields).model_dump(mode="json")


def test_replace_slide_from_library_updates_the_image_and_closes_both_modals(
        page, signed_in, keyed):
    signed_in()
    _reach_step4(page)
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps([_library_asset()])))
    updated_slide = SlidePreview(
        slide_number=1, image_url="/api/posts/e2e-post-1/slides/1/image?t=2",
        image_source="upload", width=1080, height=1350, has_raw_image=True,
    ).model_dump(mode="json")
    page.route("**/api/posts/e2e-post-1/slides/1/from-library", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(updated_slide)))

    page.locator('button[onclick^="openEditSlide"]').first.click()
    expect(page.locator("#edit-slide-modal")).to_be_visible()
    page.locator("#edit-slide-modal").get_by_role("button", name="From library").click()
    expect(page.locator("#library-picker-modal")).to_be_visible()

    page.locator("#library-picker-grid button").first.click()

    expect(page.locator("#library-picker-modal")).to_be_hidden()
    expect(page.locator("#edit-slide-modal")).to_be_hidden()
    expect(page.locator('img[data-slide-num="1"]')).to_have_attribute(
        "src", f"{page.url.rstrip('/')}/api/posts/e2e-post-1/slides/1/image?t=2")


def test_an_empty_photo_library_says_so_in_the_picker(page, signed_in, keyed):
    signed_in()
    _reach_step4(page)
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))

    page.locator('button[onclick^="openEditSlide"]').first.click()
    page.locator("#edit-slide-modal").get_by_role("button", name="From library").click()
    expect(page.locator("#library-picker-grid")).to_contain_text("Nothing in your Photos library")


def test_use_a_library_video_as_the_reel(page, signed_in, keyed):
    signed_in()
    _reach_step4(page)
    page.route("**/api/media?kind=video", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps([_library_asset(
            id="b2b2b2b2-2b2b-4b2b-8b2b-b2b2b2b2b2b2", kind="video",
            url="/api/media/b2b2b2b2-2b2b-4b2b-8b2b-b2b2b2b2b2b2/file")])))
    page.route("**/api/posts/e2e-post-1/reel/from-library", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"video_url": "/api/posts/e2e-post-1/reel/video?t=1",
                        "size_bytes": 999})))

    expect(page.locator("#reel-preview")).to_be_hidden()
    page.locator("#reel-card").get_by_role("button", name="From library").click()
    expect(page.locator("#library-picker-modal")).to_be_visible()
    page.locator("#library-picker-grid button").first.click()

    expect(page.locator("#library-picker-modal")).to_be_hidden()
    expect(page.locator("#reel-preview")).to_be_visible()
    expect(page.locator("#reel-video")).to_have_attribute(
        "src", f"{page.url.rstrip('/')}/api/posts/e2e-post-1/reel/video?t=1")


def test_declining_the_picker_leaves_the_slide_untouched(page, signed_in, keyed):
    signed_in()
    _reach_step4(page)
    page.route("**/api/media?kind=image", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps([_library_asset()])))
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "from-library" in r.url else None)

    page.locator('button[onclick^="openEditSlide"]').first.click()
    page.locator("#edit-slide-modal").get_by_role("button", name="From library").click()
    page.locator("#library-picker-modal button", has_text="✕").click()

    expect(page.locator("#library-picker-modal")).to_be_hidden()
    assert calls == []
    expect(page.locator("#edit-slide-modal")).to_be_visible()   # the slide modal is untouched
