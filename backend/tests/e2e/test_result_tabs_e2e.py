"""The result screen's network tabs.

The promise of phase 4, on screen: one idea, and a tab per network it has been
written for. A tab that has no post yet shows "＋ Adapt" instead, because
clicking it spends a full caption generation on the user's own key — the owner's
decision was that money is only ever spent by an explicit press, never by
browsing tabs.

What makes this delicate is that there is exactly ONE editor. `renderPreview`
rewrites `#slides-container`, `#caption-edit`, the hashtags, the thread, the
claims panel, the reel card and the schedule state wholesale, from its argument.
Switching tabs means re-binding it to a different post without leaving the
screen — which is the seam 4.0 built `bindPost` for, and the reason its guards
exist at all.

Three things here are guards rather than polish, and each has its own test:
unsaved text is saved before the switch and the switch is abandoned if that save
fails; the tab bar is rebuilt on a change of GROUP, never on `variants` being
short, because most endpoints deliberately return it empty; and a second click
while an adaptation is in flight must not buy a second one.
"""
import json
import re
from datetime import datetime, timezone

import pytest
from playwright.sync_api import expect

from models.schemas import PostPreview, SlidePreview

from tests.e2e.nav import open_section

pytestmark = pytest.mark.e2e


def _slide(n: int = 1) -> SlidePreview:
    return SlidePreview(
        slide_number=n, image_url=f"/api/posts/x/slides/{n}/image",
        image_source="stock", width=1080, height=1350,
        overlay_text=f"Overlay {n}", niche_text="Baking", has_raw_image=True,
    )


def _preview(post_id: str, *, platform: str = "instagram", group: str = "g1",
             variants=(), **over) -> dict:
    fields = dict(
        id=post_id, topic="Sourdough starter", format="single", status="draft",
        caption=f"Caption for {platform}.", hashtags=["#bread"],
        platform=platform, cta="Save this.", hook="A hook.",
        text_model_used="anthropic/claude-sonnet-5", image_model_used=None,
        variant_group_id=group, variants=list(variants), slides=[_slide()],
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    fields.update(over)
    return PostPreview(**fields).model_dump(mode="json")


def _variant(post_id: str, platform: str, status: str = "draft") -> dict:
    return {"id": post_id, "platform": platform, "status": status}


def _json(route, body, status=200):
    route.fulfill(status=status, content_type="application/json",
                  body=json.dumps(body))


def _one(body):
    """A closure factory: a two-parameter handler gets (route, request) from
    Playwright, so a default argument would be clobbered by the Request."""
    return lambda route: _json(route, body)


def _serve_group(page, posts: dict[str, dict]) -> None:
    page.route("**/api/posts*", lambda r: _json(r, [{
        "id": pid, "topic": "Sourdough starter", "format": "single",
        "status": "draft", "platform": p["platform"],
        "variant_group_id": p["variant_group_id"],
        "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
    } for pid, p in posts.items()]))
    for pid, body in posts.items():
        page.route(f"**/api/posts/{pid}", _one(body))


def _open(page, topic="Sourdough starter"):
    open_section(page, "queue")
    page.locator("#queue-list").get_by_text(topic).first.click()
    expect(page.locator("#create-post-panel")).to_be_visible()


# ── the bar ─────────────────────────────────────────────────────────────────

def test_a_lone_post_offers_the_other_network(page, signed_in):
    """Instagram exists; X does not yet, so its tab is an invitation to spend
    rather than a place that is already there."""
    signed_in()
    _serve_group(page, {"p1": _preview("p1", variants=[_variant("p1", "instagram")])})
    _open(page)

    expect(page.locator('#result-tabs [data-result-tab="instagram"]')).to_be_visible()
    x_tab = page.locator('#result-tabs [data-result-tab="x"]')
    expect(x_tab).to_be_visible()
    expect(x_tab).to_contain_text("Adapt")


def test_the_active_tab_is_the_post_on_screen(page, signed_in):
    signed_in()
    _serve_group(page, {"p1": _preview("p1", variants=[_variant("p1", "instagram")])})
    _open(page)
    expect(page.locator('#result-tabs [data-result-tab="instagram"]')).to_have_class(
        __import__("re").compile(r"\btab-active\b"))


def test_switching_to_an_existing_sibling_rebinds_the_editor(page, signed_in):
    signed_in()
    variants = [_variant("p1", "instagram"), _variant("p2", "x")]
    _serve_group(page, {
        "p1": _preview("p1", variants=variants),
        "p2": _preview("p2", platform="x", variants=variants),
    })
    _open(page)
    page.locator('#result-tabs [data-result-tab="x"]').click()

    expect(page.locator("#caption-edit")).to_have_value("Caption for x.")
    assert page.evaluate("[S.postId, S.post.platform]") == ["p2", "x"]


def test_an_existing_sibling_costs_nothing_to_open(page, signed_in):
    """Only a tab with no post behind it may spend."""
    signed_in()
    variants = [_variant("p1", "instagram"), _variant("p2", "x")]
    _serve_group(page, {
        "p1": _preview("p1", variants=variants),
        "p2": _preview("p2", platform="x", variants=variants),
    })
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "/adapt/" in r.url else None)

    _open(page)
    page.locator('#result-tabs [data-result-tab="x"]').click()
    expect(page.locator("#caption-edit")).to_have_value("Caption for x.")
    assert calls == []


# ── adapting ────────────────────────────────────────────────────────────────

def test_adapting_binds_the_new_sibling(page, signed_in):
    signed_in()
    _serve_group(page, {"p1": _preview("p1", variants=[_variant("p1", "instagram")])})
    page.route("**/api/posts/p1/adapt/x", _one(_preview(
        "p2", platform="x",
        variants=[_variant("p1", "instagram"), _variant("p2", "x")])))

    _open(page)
    page.locator('#result-tabs [data-result-tab="x"]').click()

    expect(page.locator("#caption-edit")).to_have_value("Caption for x.")
    assert page.evaluate("S.postId") == "p2"
    # ...and the tab stops being an invitation.
    expect(page.locator('#result-tabs [data-result-tab="x"]')).not_to_contain_text("Adapt")


def test_a_second_click_while_adapting_does_not_buy_a_second_one(page, signed_in):
    """Adapting takes seconds and the button stays on screen. Without the
    in-flight flag an impatient double-click is two generations on the user's
    key — and the server's idempotency only saves the row, not the spend."""
    signed_in()
    _serve_group(page, {"p1": _preview("p1", variants=[_variant("p1", "instagram")])})
    calls = []

    def _slow(route):
        calls.append(route.request.url)
        page.wait_for_timeout(700)
        _json(route, _preview("p2", platform="x",
                              variants=[_variant("p1", "instagram"), _variant("p2", "x")]))

    page.route("**/api/posts/p1/adapt/x", _slow)

    _open(page)
    # Both calls in one tick, before the bar can re-render with the button
    # disabled — so this exercises the S.adapting flag rather than the `disabled`
    # attribute that follows from it. A real double-click can land in exactly
    # that window.
    page.evaluate("() => { setResultTab('x'); setResultTab('x'); }")
    expect(page.locator("#caption-edit")).to_have_value("Caption for x.")
    assert len(calls) == 1, calls


def test_a_failed_adaptation_says_so_and_keeps_the_screen(page, signed_in):
    """The e2e server has no AI key, so this is the ordinary case rather than an
    exotic one. The editor must not go blank and the tab must stay offerable."""
    signed_in()
    _serve_group(page, {"p1": _preview("p1", variants=[_variant("p1", "instagram")])})
    page.route("**/api/posts/p1/adapt/x", lambda r: _json(
        r, {"detail": "No text model selected."}, status=400))

    _open(page)
    page.locator('#result-tabs [data-result-tab="x"]').click()

    expect(page.locator("#toast")).to_contain_text("No text model selected")
    assert page.evaluate("S.postId") == "p1"
    expect(page.locator("#caption-edit")).to_have_value("Caption for instagram.")
    expect(page.locator('#result-tabs [data-result-tab="x"]')).to_contain_text("Adapt")


# ── the guards ──────────────────────────────────────────────────────────────

def test_unsaved_edits_are_saved_before_the_switch(page, signed_in):
    """The editor autosaves to localStorage under the post's own key, so text
    typed and then abandoned by a tab switch would sit somewhere the user never
    returns to."""
    signed_in()
    variants = [_variant("p1", "instagram"), _variant("p2", "x")]
    _serve_group(page, {
        "p1": _preview("p1", variants=variants),
        "p2": _preview("p2", platform="x", variants=variants),
    })
    saved = []
    page.route("**/api/posts/p1/caption", lambda r: (
        saved.append(r.request.post_data),
        _json(r, _preview("p1", variants=variants))))

    _open(page)
    page.locator("#caption-edit").fill("Typed but never saved.")
    page.locator('#result-tabs [data-result-tab="x"]').click()

    expect(page.locator("#caption-edit")).to_have_value("Caption for x.")
    assert saved and "Typed but never saved." in saved[0]


def test_a_failed_save_abandons_the_switch(page, signed_in):
    """Losing the text silently would be worse than staying put. The tab that
    could not be left keeps the words on screen."""
    signed_in()
    variants = [_variant("p1", "instagram"), _variant("p2", "x")]
    _serve_group(page, {
        "p1": _preview("p1", variants=variants),
        "p2": _preview("p2", platform="x", variants=variants),
    })
    page.route("**/api/posts/p1/caption",
               lambda r: _json(r, {"detail": "nope"}, status=500))

    _open(page)
    page.locator("#caption-edit").fill("Do not lose me.")
    page.locator('#result-tabs [data-result-tab="x"]').click()

    expect(page.locator("#caption-edit")).to_have_value("Do not lose me.")
    assert page.evaluate("S.postId") == "p1"


def test_a_response_without_variants_does_not_shrink_the_bar(page, signed_in):
    """Most endpoints return `variants: []` on purpose — filling it costs a query
    nothing reads — so an empty list means "not asked", never "no siblings".

    Driven through bindPost rather than through the UI, and that is the point
    rather than a shortcut: today the only three things that bind a post all
    fill the list, so the branch is unreachable from the screen. It exists for
    the next endpoint that starts binding one, and this is the seam that would
    drive it. Without the group check, that day silently turns the X tab back
    into an offer to pay for a post that already exists.
    """
    signed_in()
    variants = [_variant("p1", "instagram"), _variant("p2", "x")]
    _serve_group(page, {
        "p1": _preview("p1", variants=variants),
        "p2": _preview("p2", platform="x", variants=variants),
    })

    _open(page)
    expect(page.locator('#result-tabs [data-result-tab="x"]')).not_to_contain_text("Adapt")

    page.evaluate("""() => {
      const same = JSON.parse(JSON.stringify(S.post));
      same.variants = [];
      bindPost(same);
    }""")

    expect(page.locator('#result-tabs [data-result-tab="x"]')).to_be_visible()
    expect(page.locator('#result-tabs [data-result-tab="x"]')).not_to_contain_text("Adapt")


def test_the_editor_chrome_follows_the_tab(page, signed_in):
    """The payoff of 3.2: the character counter and the SEO block read the POST,
    so switching to X changes them with no extra wiring."""
    signed_in()
    variants = [_variant("p1", "instagram"), _variant("p2", "x")]
    _serve_group(page, {
        "p1": _preview("p1", variants=variants),
        "p2": _preview("p2", platform="x", variants=variants),
    })
    _open(page)
    expect(page.locator("#seo-group")).to_be_visible()

    page.locator('#result-tabs [data-result-tab="x"]').click()
    expect(page.locator("#seo-group")).to_be_hidden()
    expect(page.locator("#caption-count")).to_be_visible()
