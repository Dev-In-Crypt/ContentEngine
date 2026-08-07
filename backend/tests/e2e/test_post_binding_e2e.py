"""Binding a post to the editor, and letting go of the last one.

Step 4 is a single set of DOM ids that `renderPreview` rewrites wholesale, so it
looks like opening a second post cannot leak anything from the first. It can:
three pointers say which post is on screen — `S.postId`, `S.post` and
`S.currentPost` — and they are assigned in two different functions. Around
fourteen actions read `S.postId`, and `renderPreview` sets the other two.
Nothing guarantees the three agree.

Today that is survivable, because there is only one way in. UX phase 4 adds
per-network tabs, which switch the bound post without leaving the screen, and
then a stale pointer means publishing the wrong network's post. So the pointers
move behind one function first, in its own commit, before anything starts
switching between them.

The two leaks these tests pin down are real today and would simply become
louder with tabs: `S.slideOriginals` is keyed by slide number and never
cleared, so a carousel followed by a single post leaves entries the Reset
button will happily read; and `S.editingSlide` survives a post change, so the
slide-replace modal's target outlives the post it belonged to.
"""
import json
from datetime import datetime, timezone

import pytest
from playwright.sync_api import expect

from models.schemas import PostPreview, SlidePreview

from tests.e2e.nav import open_section

pytestmark = pytest.mark.e2e


def _slide(n: int) -> SlidePreview:
    return SlidePreview(
        slide_number=n, image_url=f"/api/posts/x/slides/{n}/image",
        image_source="stock", width=1080, height=1350,
        overlay_text=f"Overlay {n}", niche_text="Baking", has_raw_image=True,
    )


def _preview(post_id: str, topic: str, slides: int = 1, **over) -> dict:
    fields = dict(
        id=post_id, topic=topic, format="single", status="draft",
        caption="A caption.", hashtags=["#bread"], platform="instagram",
        cta="Save this.", hook="A hook.",
        text_model_used="anthropic/claude-sonnet-5", image_model_used=None,
        slides=[_slide(n) for n in range(1, slides + 1)],
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    fields.update(over)
    return PostPreview(**fields).model_dump(mode="json")


def _summary(post_id: str, topic: str) -> dict:
    return {
        "id": post_id, "topic": topic, "format": "single", "status": "draft",
        "platform": "instagram",
        "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
    }


def _serve(page, posts: dict[str, dict]) -> None:
    """The list, plus each post by id."""
    page.route("**/api/posts*", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps([_summary(pid, p["topic"]) for pid, p in posts.items()])))
    # A closure factory, not `lambda r, b=body:` — Playwright hands a two-parameter
    # handler `(route, request)`, so the default would be clobbered by the Request.
    def _one(body):
        return lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(body))

    for pid, body in posts.items():
        page.route(f"**/api/posts/{pid}", _one(body))


def _open_from_queue(page, topic: str) -> None:
    open_section(page, "queue")
    page.locator("#queue-list").get_by_text(topic).click()
    expect(page.locator("#create-post-panel")).to_be_visible()


def test_opening_a_post_agrees_with_itself(page, signed_in):
    """All three pointers, one post. They are assigned in two functions today
    and there is no path that makes them agree by construction."""
    signed_in()
    _serve(page, {"p1": _preview("p1", "Sourdough starter")})
    _open_from_queue(page, "Sourdough starter")

    assert page.evaluate("[S.postId, S.post && S.post.id, S.currentPost && S.currentPost.id]") \
        == ["p1", "p1", "p1"]


def test_opening_a_second_post_drops_the_first(page, signed_in):
    signed_in()
    _serve(page, {
        "p1": _preview("p1", "Sourdough starter"),
        "p2": _preview("p2", "Rye loaf"),
    })
    _open_from_queue(page, "Sourdough starter")
    _open_from_queue(page, "Rye loaf")

    assert page.evaluate("[S.postId, S.post && S.post.id, S.currentPost && S.currentPost.id]") \
        == ["p2", "p2", "p2"]
    expect(page.locator("#slides-container")).not_to_contain_text("Overlay 2")


def test_a_carousel_leaves_no_slide_state_behind(page, signed_in):
    """`S.slideOriginals` is keyed by slide number, so a three-slide post
    followed by a one-slide post leaves entries 2 and 3 pointing at text that is
    no longer on screen — and `resetOverlay` reads exactly that map."""
    signed_in()
    _serve(page, {
        "p1": _preview("p1", "Three slides", slides=3, format="carousel_3"),
        "p2": _preview("p2", "One slide", slides=1),
    })
    _open_from_queue(page, "Three slides")
    assert page.evaluate("Object.keys(S.slideOriginals).length") == 3

    _open_from_queue(page, "One slide")
    assert page.evaluate("Object.keys(S.slideOriginals)") == ["1"]


def test_a_half_finished_slide_edit_does_not_outlive_its_post(page, signed_in):
    """`S.editingSlide` is the target of the replace and upload calls. Left set
    across a post change it aims them at a slide number the new post may not
    even have.

    This one drives `bindPost` directly rather than through the UI, and that is
    the point rather than a shortcut: with the modal open its overlay covers the
    navigation, so today there is no way to reach another post without closing
    it first — the leak is unreachable. Phase 4.5's network tabs bind a
    different post *without leaving the screen*, which is exactly this seam. The
    test exercises the guard through the entry point that will drive it.
    """
    signed_in()
    _serve(page, {
        "p1": _preview("p1", "Three slides", slides=3, format="carousel_3"),
        "p2": _preview("p2", "One slide", slides=1),
    })
    _open_from_queue(page, "Three slides")
    page.evaluate("openEditSlide(3)")
    expect(page.locator("#edit-slide-modal")).to_be_visible()

    page.evaluate("""async () => {
      const r = await apiFetch(`${API}/api/posts/p2`);
      bindPost(await r.json());
    }""")

    expect(page.locator("#edit-slide-modal")).to_be_hidden()
    assert page.evaluate("S.editingSlide") is None
    assert page.evaluate("S.postId") == "p2"


def test_a_step_four_action_targets_the_post_on_screen(page, signed_in):
    """The point of the whole refactor, stated as a test: whatever the editor is
    showing is what an action acts on."""
    signed_in()
    _serve(page, {
        "p1": _preview("p1", "Sourdough starter"),
        "p2": _preview("p2", "Rye loaf"),
    })
    calls = []
    page.route("**/api/posts/*/caption", lambda r: (
        calls.append(r.request.url),
        r.fulfill(status=200, content_type="application/json",
                  body=json.dumps(_preview("p2", "Rye loaf")))))

    _open_from_queue(page, "Sourdough starter")
    _open_from_queue(page, "Rye loaf")
    page.evaluate("saveCaption()")

    expect(page.locator("#queue-list, #create-post-panel").first).to_be_visible()
    page.wait_for_function("window.__lastCaptionCall !== undefined || true")
    assert calls and calls[-1].endswith("/api/posts/p2/caption"), calls
