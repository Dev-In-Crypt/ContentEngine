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


def _demo_503(page, detail):
    page.route("**/api/demo/post", lambda r: r.fulfill(
        status=503, content_type="application/json",
        body=json.dumps({"detail": detail})))


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


def test_an_empty_result_card_is_not_sitting_there_waiting(page, live_server):
    """Found by a mutation that should have failed and did not.

    `hidden` on the same element as a responsive display utility loses: Tailwind
    emits sm:* after the base utilities at equal specificity, so `hidden sm:flex`
    is `display:flex` on any screen at or above 640px — and the empty result card
    was on the landing from first paint, above the fold, on every desktop.

    Nothing asserted the card starts hidden, so nothing noticed.
    """
    _land(page, live_server)

    expect(page.locator("#hero-result")).to_be_hidden()
    expect(page.locator("#hero-gate")).to_be_hidden()


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
    """503 for a spent budget is our problem, not theirs, and the sentence
    should read that way.

    It used to stub a 503 with no body and still assert this wording, which
    passed only because the hero invented the sentence for every 503 — including
    the one that means "no key was ever configured here". The reason now comes
    from the server, so the test has to serve one.
    """
    _demo_503(page, "The free demo is resting until tomorrow. Sign up to keep going.")
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


# ── the soft gate (UX phase 7.3) ────────────────────────────────────────────
#
# Two runs, then an account. The counter lives in localStorage and is a polite
# request rather than a defence — clearing it takes two clicks, which is why the
# real limits are per-IP and the daily ceiling, both server-side. What this
# buys is the moment: somebody who has seen it work twice is being asked at the
# only point where the answer is obviously worth it.

FREE_TRIES = 2


def _run_twice(page):
    for _ in range(FREE_TRIES):
        _run(page)
        expect(page.locator("#hero-result")).to_be_visible()
        page.locator("#hero-input").fill("")


def test_the_first_two_runs_are_free(page, live_server):
    calls = _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run_twice(page)

    assert len(calls) == FREE_TRIES


def _refuse_with_402(page):
    """What the server answers once the address has had its two."""
    page.route("**/api/demo/post", lambda r: r.fulfill(
        status=402, content_type="application/json",
        body=json.dumps({"detail": "That's your two free posts. "
                                   "Create a free account for two more."})))


def test_the_third_try_asks_for_an_account(page, live_server):
    """The allowance moved to the server (services/anon_quota.py), so what the
    landing has to get right is no longer counting — it is reacting. A 402 means
    "that was the free part" and shows the gate; a 429 means "too fast" and says
    something else entirely."""
    calls = _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run_twice(page)
    _refuse_with_402(page)

    _run(page)

    expect(page.locator("#hero-gate")).to_be_visible()
    expect(page.locator("#hero-gate")).to_contain_text("free posts")
    assert len(calls) == FREE_TRIES          # the refusal costs us nothing


def test_the_gate_says_what_is_waiting_on_the_other_side(page, live_server):
    """A wall that only says "no" reads as the end. The five free posts an
    account comes with (UX phase 6) are the reason the answer is worth giving."""
    _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run_twice(page)
    _run(page)

    expect(page.locator("#hero-gate")).to_contain_text("5")


def test_the_gate_leads_to_sign_up(page, live_server):
    _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run_twice(page)
    _refuse_with_402(page)
    _run(page)

    page.locator("#hero-gate-signup").click()
    expect(page.locator("#auth-screen")).to_be_visible()


def test_downloading_never_asks_who_you_are(page, live_server):
    """Even at the gate. What they made is theirs — holding it hostage is a
    different product than the one this landing is arguing for."""
    _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run_twice(page)
    _refuse_with_402(page)
    _run(page)

    expect(page.locator("#hero-gate")).to_be_visible()
    expect(page.locator("#hero-download")).to_be_visible()
    expect(page.locator("#hero-download")).to_be_enabled()


def test_the_last_post_stays_on_screen_behind_the_gate(page, live_server):
    """Nothing is taken away when the wall arrives: the second post is still
    there to read, copy and save."""
    _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run_twice(page)
    _refuse_with_402(page)
    _run(page)

    # Wait for the gate FIRST. Both assertions below are already true the
    # instant the click is sent, and Playwright's expect passes on its first
    # poll — so without something to wait for they race the very change they
    # are meant to observe and report success before it happens.
    expect(page.locator("#hero-gate")).to_be_visible()

    # And visibility as well as the text: to_contain_text reads textContent and
    # passes on a hidden element, so the words alone would not notice the gate
    # sweeping the post off the screen on its way in.
    expect(page.locator("#hero-result")).to_be_visible()
    expect(page.locator("#hero-result")).to_contain_text("flour, water and patience")


def test_a_returning_visitor_is_still_out_of_tries(page, live_server):
    """The allowance is the server's, so a reload — or a new browser, or cleared
    storage — changes nothing. That was the whole reason it stopped being kept
    in localStorage, where two clicks bought two more posts forever."""
    calls = _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run_twice(page)
    _refuse_with_402(page)

    page.reload()
    _run(page)

    expect(page.locator("#hero-gate")).to_be_visible()
    assert len(calls) == FREE_TRIES


def test_a_failed_run_is_not_counted_against_them(page, live_server):
    """It produced nothing. Charging a try for our own error would spend a
    stranger's patience on our failure."""
    calls = _serve(page, {"type": "error", "message": "Something went wrong."})
    _land(page, live_server)
    _run(page)
    expect(page.locator("#hero-status")).to_contain_text("went wrong")

    page.unroute("**/api/demo/post")
    calls = _serve(page, {"type": "complete", "post": _post()})
    _run_twice(page)

    expect(page.locator("#hero-gate")).to_be_hidden()
    assert len(calls) == FREE_TRIES


# ── the draft rides along (UX phase 7.4) ────────────────────────────────────
#
# The landing stores nothing on the server, so the post lives in the browser
# until there is an account to give it to. What is asserted here is the handover:
# parked on the way to sign-up, spent once afterwards, and never twice.

DRAFT_KEY = "landing_draft"


def test_the_last_post_is_parked_for_the_sign_up(page, live_server):
    _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run(page)
    expect(page.locator("#hero-result")).to_be_visible()

    parked = page.evaluate(f"JSON.parse(localStorage.getItem('{DRAFT_KEY}') || 'null')")
    assert parked and parked["caption"] == "A starter is flour, water and patience."


def test_signing_up_carries_it_into_the_app(page, live_server, signup):
    carried = []
    page.route("**/api/posts/from-draft", lambda route, request: (
        carried.append(json.loads(request.post_data or "{}")),
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(_carried_preview()))))
    _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run(page)
    expect(page.locator("#hero-result")).to_be_visible()

    signup()

    expect(page.locator("#step-4")).to_be_visible()
    assert len(carried) == 1
    assert carried[0]["caption"] == "A starter is flour, water and patience."


def test_it_is_spent_once_and_not_again(page, live_server, signup):
    """Parked in localStorage and removed before the call, so a reload after the
    handover does not create a second copy of the same post."""
    carried = []
    page.route("**/api/posts/from-draft", lambda route, request: (
        carried.append(1),
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(_carried_preview()))))
    _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run(page)
    expect(page.locator("#hero-result")).to_be_visible()

    signup()
    expect(page.locator("#step-4")).to_be_visible()
    page.reload()
    page.wait_for_load_state("networkidle")

    assert len(carried) == 1
    assert page.evaluate(f"localStorage.getItem('{DRAFT_KEY}')") is None


def test_an_ordinary_sign_up_carries_nothing(page, signup):
    """Nothing parked, nothing sent. Without this the handover could fire on
    every registration and the tests above would still pass."""
    carried = []
    page.route("**/api/posts/from-draft", lambda route, request: (
        carried.append(1),
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(_carried_preview()))))

    signup()
    page.wait_for_load_state("networkidle")

    assert carried == []


def test_a_refused_handover_still_leaves_the_topic_in_the_composer(page, live_server, signup):
    """Losing the picture is a bad day; losing the idea as well is a worse one,
    and the topic is one line of text we already have."""
    page.route("**/api/posts/from-draft", lambda r: r.fulfill(
        status=422, content_type="application/json",
        body=json.dumps({"detail": "That picture could not be read."})))
    _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run(page)
    expect(page.locator("#hero-result")).to_be_visible()

    signup()

    expect(page.locator("#topic")).to_have_value("Sourdough starters")


def _carried_preview() -> dict:
    """What /from-draft answers with: an ordinary PostPreview."""
    from datetime import datetime, timezone

    from models.schemas import PostPreview, SlidePreview
    return PostPreview(
        id="carried-1", topic="Sourdough starters", format="single", status="preview",
        caption="A starter is flour, water and patience.",
        hashtags=["#sourdough"], seo_keywords=[], cta="Save this.",
        hook="Your starter is not dead.", platform="instagram",
        text_model_used="our/model", image_model_used="our/image-model",
        slides=[SlidePreview(slide_number=1,
                             image_url="/api/posts/carried-1/slides/1/image",
                             image_source="ai_gen", width=1080, height=1350,
                             has_raw_image=False)],
        created_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    ).model_dump(mode="json")


# ── deep links (UX phase 7.5) ───────────────────────────────────────────────
#
# A link that arrives already knowing what to write. Outreach sends one per
# recipient, so the first thing somebody sees is a post about their own subject
# rather than an empty field — which is the same argument as the field itself,
# one step earlier.


def test_a_topic_in_the_link_fills_the_field_and_runs(page, live_server):
    calls = _serve(page, {"type": "complete", "post": _post()})
    page.goto(f"{live_server}/?topic=Sourdough+starters")

    expect(page.locator("#hero-result")).to_be_visible()
    assert calls == [{"topic": "Sourdough starters"}]
    expect(page.locator("#hero-input")).to_have_value("Sourdough starters")


def test_a_url_in_the_link_switches_the_tab_too(page, live_server):
    """Otherwise the field says "a topic" while holding a URL, and the next run
    — the one the visitor starts themselves — sends it under the wrong name."""
    calls = _serve(page, {"type": "complete", "post": _post()})
    page.goto(f"{live_server}/?url=https%3A%2F%2Fcrumb.example")

    expect(page.locator("#hero-result")).to_be_visible()
    assert calls == [{"url": "https://crumb.example"}]
    assert page.evaluate("S.heroMode") == "link"


def test_the_query_is_cleaned_off_the_address_bar(page, live_server):
    """Same reason /verify and /team/accept clean theirs: what is in the address
    bar gets shared, screenshotted and reloaded. A reload here would spend a
    second free try on the same topic."""
    _serve(page, {"type": "complete", "post": _post()})
    page.goto(f"{live_server}/?topic=Sourdough+starters")

    expect(page.locator("#hero-result")).to_be_visible()
    assert "topic=" not in page.url


def test_a_deep_link_past_the_gate_asks_for_an_account(page, live_server):
    """A link is not a way around the allowance — it lands on the same refusal."""
    calls = _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)
    _run_twice(page)
    _refuse_with_402(page)

    page.goto(f"{live_server}/?topic=One+more+please")

    expect(page.locator("#hero-gate")).to_be_visible()
    assert len(calls) == FREE_TRIES


def test_a_signed_in_visitor_spends_nothing_on_a_deep_link(page, live_server, signup):
    """The deep link is for strangers. Somebody who already has an account gets
    their app, and — the part worth asserting — no anonymous generation is fired
    on our key behind their back.

    Asserting only "the landing stays hidden" would pass for the wrong reason:
    running the hero field does not show the landing, so that check is true
    whether or not the link was acted on.
    """
    calls = _serve(page, {"type": "complete", "post": _post()})
    signup()

    page.goto(f"{live_server}/?topic=Sourdough+starters")
    page.wait_for_load_state("networkidle")

    assert calls == []
    expect(page.locator("#landing-screen")).to_be_hidden()


# ── the refusal has to be the server's, not the client's guess ──────────────
#
# `POST /api/demo/post` has two 503s and they mean opposite things: "nobody has
# configured a key here" and "today's budget is spent". The hero printed the
# second one for both, so a deployment with no key told every visitor to come
# back tomorrow — forever. Found in production, where it was the first sentence
# the product said to anybody.

def test_a_spent_budget_says_come_back_tomorrow(page, live_server):
    _demo_503(page, "The free demo is resting until tomorrow. Sign up to keep going.")
    page.goto(live_server)

    page.locator("#hero-input").fill("Sourdough starters")
    page.locator("#hero-run").click()

    expect(page.locator("#hero-status")).to_contain_text("tomorrow")


def test_an_unconfigured_deployment_does_not_promise_tomorrow(page, live_server):
    """Tomorrow it says exactly the same thing. A wait that never ends is worse
    than an honest "not right now" — the visitor plans a return that is pointless."""
    _demo_503(page, "Demo is temporarily unavailable.")
    page.goto(live_server)

    page.locator("#hero-input").fill("Sourdough starters")
    page.locator("#hero-run").click()

    expect(page.locator("#hero-status")).to_be_visible()
    expect(page.locator("#hero-status")).not_to_contain_text("tomorrow")


def test_a_refusal_with_no_reason_still_says_something(page, live_server):
    """A 503 from in front of the app — a proxy, a restart — carries no JSON at
    all. Silence would leave the button looking broken."""
    page.route("**/api/demo/post", lambda r: r.fulfill(status=503, body="upstream"))
    page.goto(live_server)

    page.locator("#hero-input").fill("Sourdough starters")
    page.locator("#hero-run").click()

    expect(page.locator("#hero-status")).to_be_visible()
    expect(page.locator("#hero-status")).not_to_have_text("")


def test_enter_in_the_topic_field_runs_it(page, live_server):
    """The keydown that stayed out of the registry — a condition, not an action,
    and the only one in the file. It is wired directly at start-up instead, so
    it needs a test of its own: the mutation pass found nothing pressing Enter
    here, which meant the wiring could have been deleted in silence."""
    calls = _serve(page, {"type": "complete", "post": _post()})
    _land(page, live_server)

    page.locator("#hero-input").fill("Sourdough starters")
    page.locator("#hero-input").press("Enter")

    expect(page.locator("#hero-result")).to_be_visible()
    assert calls, "Enter did not start a generation"


# ── the preview shows what the download contains ────────────────────────────

#: A 40×50 PNG — the 4:5 the brand engine actually renders (1080×1350), small.
PORTRAIT = ("data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAACgAAAAyCAIAAACh0Q7HAAAAXklEQVR4nO3QQREDQQCEwA26"
            "TkRkR1a+NwbYDxjoKj6/73NuxBX1BIthYu+CtfCoLVgLj9qCtfCoLVgLj9qCtfCoLVgLj9qC"
            "tfCoLVgLj9qCtfCoLVgLj9qCta6t/gMYbgG45JfkRgAAAABJRU5ErkJggg==")

#: Taller than the picture beside it once rendered — which is the condition that
#: stretched the picture, so a shorter caption makes this test prove nothing.
#: Paragraphs, because the caption renders with `whitespace-pre-line` and a real
#: one from the model comes back in three or four of them.
_WORDY = "\n\n".join(
    ["A starter is flour, water and patience, and the jar does most of the work."] * 8)


def test_the_preview_is_not_a_cropped_version_of_the_download(page, live_server):
    """What the landing shows has to be what "Download the picture" saves.

    The image sat in a flex row with `object-cover` and no ratio of its own, so
    its height came from the text column next to it. A caption of ordinary
    length made that column taller than 4:5, the picture was stretched to match
    and then cropped to fit — about a fifth of the width, taken off the sides,
    which is exactly where the brand engine puts the headline. Live on prod the
    first screen of the product read "hy do B2B SaaS demos fail?".

    Asserted as a ratio rather than as a class name: the defect is what the
    visitor sees, and a class list can be rearranged without changing it.
    """
    _serve(page, {"type": "complete",
                  "post": _post(image_data_url=PORTRAIT, caption=_WORDY)})
    _land(page, live_server)
    _run(page)

    expect(page.locator("#hero-image")).to_be_visible()
    shown, natural = page.evaluate(
        """() => {
            const i = document.getElementById('hero-image');
            const r = i.getBoundingClientRect();
            return [r.width / r.height, i.naturalWidth / i.naturalHeight];
        }"""
    )
    assert abs(shown - natural) < 0.02, (
        f"preview is {shown:.3f} against the picture's own {natural:.3f} — "
        "the visitor is being shown a crop of what they would download")


# ── the landing is a screen, not a sheet laid over a live app ───────────────

def test_tabbing_around_the_landing_never_reaches_the_app_behind_it(page, live_server):
    """The overlay covers the application visually and nothing else.

    Counted on prod: 34 focusable elements on the page, 17 of them in the
    landing. Tab order started on an invisible control *above* the overlay and,
    past "Contact", walked into the whole product — Create, Calendar, Queue,
    Results, the composer's fields — none of it on screen. Same shape as the
    toast that was written into a hidden box: the interface says one thing and
    the DOM says another, and a keyboard or screen-reader user gets the DOM.

    Asserted by pressing Tab rather than by reading an attribute, because the
    attribute is the mechanism and this is the behaviour.
    """
    _land(page, live_server)
    page.evaluate("document.body.focus()")

    escaped = []
    for _ in range(30):
        page.keyboard.press("Tab")
        where = page.evaluate(
            """() => {
                const a = document.activeElement;
                if (!a || a === document.body || a === document.documentElement) return null;
                const landing = document.getElementById('landing-screen');
                return landing.contains(a) ? null
                     : (a.id || a.tagName + ':' + (a.innerText || '').trim().slice(0, 30));
            }"""
        )
        if where:
            escaped.append(where)

    assert not escaped, f"Tab reached {len(escaped)} controls behind the overlay: {escaped[:6]}"
