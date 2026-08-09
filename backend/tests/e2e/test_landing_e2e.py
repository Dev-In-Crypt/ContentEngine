"""The landing's working field, in a real browser.

The home page used to answer "show me what it does" with a sign-up form. The
field is now the first thing on it: a topic or a link goes in, a finished post
comes back, and nothing has been asked of the visitor yet.

Everything here runs signed out — which is the point, and also why the tests
have no fixture beyond `page`: an account is exactly what this screen is not
allowed to require.

/api/demo/post is stubbed. What it does with a request is settled in
tests/test_landing_post_api.py against the real route; what the browser does
with each kind of answer is settled here, and that is the half no server test
can see.
"""
import json

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def _sse(*frames: dict) -> str:
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames)


#: A 1×1 PNG as a data URL, the shape the route sends a slide back in.
PIXEL = ("data:image/png;base64,"
         "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
         "hKmMIQAAAABJRU5ErkJggg==")


def _post(**over) -> dict:
    body = dict(topic="Sourdough starters",
                caption="A starter is flour, water and patience.",
                hook="Your starter is not dead.", cta="Save this.",
                hashtags=["#sourdough", "#baking"], image_data_url=PIXEL)
    body.update(over)
    return body


def _serve(page, *frames, status=200):
    """Answer the landing's generate call with a canned stream."""
    calls = []

    def handler(route, request):
        calls.append(json.loads(request.post_data or "{}"))
        route.fulfill(status=status, content_type="text/event-stream",
                      body=_sse(*frames))

    page.route("**/api/demo/post", handler)
    return calls


def _land(page, live_server):
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()


def _run(page, text="Sourdough starters"):
    page.locator("#hero-input").fill(text)
    page.locator("#hero-run").click()


# ── the field is the first thing ────────────────────────────────────────────

def test_the_field_is_on_the_first_screen_for_everybody(page, live_server):
    """Not behind a tab. The two doors are still down the page — they describe
    two audiences, which is a slower question than "show me"."""
    _land(page, live_server)

    expect(page.locator("#hero-field")).to_be_visible()
    expect(page.locator("#ltab-creator")).to_be_visible()
    expect(page.locator("#ltab-business")).to_be_visible()


def test_nothing_here_asks_who_you_are(page, live_server):
    """The whole argument of the phase, asserted where it can regress: no login
    wall, no sign-up gate, no account of any kind before the first result."""
    _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run(page)

    expect(page.locator("#hero-result")).to_be_visible()
    expect(page.locator("#auth-screen")).to_be_hidden()
    assert page.evaluate("localStorage.getItem('api_token')") is None


# ── a topic ─────────────────────────────────────────────────────────────────

def test_a_topic_comes_back_as_a_post(page, live_server):
    calls = _serve(page, {"type": "progress", "message": "Writing your post…"},
                   {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run(page)

    expect(page.locator("#hero-result")).to_contain_text("flour, water and patience")
    expect(page.locator("#hero-result")).to_contain_text("#sourdough")
    expect(page.locator("#hero-image")).to_be_visible()
    assert calls == [{"topic": "Sourdough starters"}]


def test_a_stage_reaches_the_screen(page, live_server):
    """A blank screen for ten seconds reads as broken, and this is the one place
    where nobody has any reason yet to wait for us.

    The stream deliberately stops after the stage: a finished run clears the
    status line, so a test that also sent `complete` would be asserting against
    a line the app had already — correctly — wiped.
    """
    _serve(page, {"type": "progress", "message": "Writing your post…"})
    _land(page, live_server)
    _run(page)

    expect(page.locator("#hero-status")).to_contain_text("Writing your post")


def test_a_short_topic_never_reaches_the_server(page, live_server):
    """The server would 422 it. Saying so here costs nothing and spends nobody's
    round trip."""
    calls = _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run(page, "ai")

    expect(page.locator("#hero-status")).to_contain_text("a little more")
    assert calls == []


# ── a link ──────────────────────────────────────────────────────────────────

def test_the_link_tab_sends_a_url_instead_of_a_topic(page, live_server):
    """One field, two meanings — and the server takes exactly one of them, so
    the tab decides which name the value travels under."""
    calls = _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    page.locator('[data-hero-mode="link"]').click()
    _run(page, "https://crumb.example")

    assert calls == [{"url": "https://crumb.example"}]


def test_something_that_is_not_a_link_is_caught_before_the_request(page, live_server):
    calls = _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    page.locator('[data-hero-mode="link"]').click()
    _run(page, "crumb.example")

    expect(page.locator("#hero-status")).to_contain_text("http")
    assert calls == []


# ── the examples ────────────────────────────────────────────────────────────

def test_an_example_fills_the_field_and_runs(page, live_server):
    """Three of them, because the hardest part of an empty field is the first
    idea. Clicking one is the shortest path from arriving to seeing."""
    calls = _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    page.locator("#hero-examples button").first.click()

    expect(page.locator("#hero-result")).to_be_visible()
    assert len(calls) == 1
    assert calls[0]["topic"] == page.locator("#hero-input").input_value()


# ── when it does not work ───────────────────────────────────────────────────

def test_an_error_frame_is_shown_and_the_button_comes_back(page, live_server):
    """A dead button after a failure is worse than the failure: it turns "try
    again" into "reload the page"."""
    _serve(page, {"type": "error", "message": "Something went wrong. Please try again."})
    _land(page, live_server)
    _run(page)

    expect(page.locator("#hero-status")).to_contain_text("went wrong")
    expect(page.locator("#hero-run")).to_be_enabled()


def test_a_paused_demo_says_so_rather_than_failing_silently(page, live_server):
    """503 is the daily ceiling — our problem, not theirs, and the sentence
    should read that way."""
    _serve(page, status=503)
    _land(page, live_server)
    _run(page)

    expect(page.locator("#hero-status")).to_contain_text("Sign up")


def test_a_second_run_started_mid_flight_does_not_ask_twice(page, live_server):
    """Every run costs us a model call and a picture, and this screen is public.

    Both runs are started in one tick rather than through two clicks: a stubbed
    route answers instantly, so a second CLICK always lands after the first run
    has finished and would prove nothing. The flag is set synchronously before
    the first await, which is exactly the window this guards.
    """
    calls = _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    page.locator("#hero-input").fill("Sourdough starters")

    page.evaluate("() => { runHeroPost(); runHeroPost(); }")

    expect(page.locator("#hero-result")).to_be_visible()
    assert len(calls) == 1
