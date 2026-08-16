"""The gallery of finished slides, and the two things it must not pretend.

These are the only pictures on the home page. They are rendered by
scripts/render_gallery.py through the same engine that draws a slide inside a
post, which is the whole reason they are allowed to be there — and it is a
claim that decays silently, because a broken path, a missing file or a
hand-drawn replacement all look like "a gallery" until somebody measures.

So: the files must actually decode, at the size the product actually renders,
and the caption must keep saying which parts of them are ours rather than a
model's. The second is the honesty guard for this section — the neighbouring
one in test_landing_honesty_e2e.py forbids invented customers, and this forbids
inviting a visitor to read written words as generated ones.
"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

#: What the brand engine renders a portrait slide at, in
#: PillowBrandEngine.INSTAGRAM_SIZES["portrait"]. Asserted rather than trusted
#: because a placeholder of the wrong shape is the failure this catches.
SLIDE = (1080, 1350)

EXPECTED = 3


def _images(page):
    return page.locator("#gallery img")


def test_the_gallery_shows_three_slides(page, live_server):
    page.goto(live_server)
    expect(page.locator("#gallery")).to_be_visible()
    expect(_images(page)).to_have_count(EXPECTED)


def test_the_gallery_images_decode(page, live_server):
    """naturalWidth, not the src attribute. A broken path still has a src, and
    a page of broken pictures is worse than a page of none — this repository
    has made that exact mistake before, in the brand preview that could not
    carry its own token."""
    page.goto(live_server)
    expect(page.locator("#gallery")).to_be_visible()
    # These carry loading="lazy" — worth it on a page this long, and it means a
    # picture below the fold reports 0x0 for the honest reason "not fetched
    # yet". Scroll to them and wait for the fetch to settle, so a 0 afterwards
    # means the file is not there rather than not yet wanted.
    page.locator("#gallery").scroll_into_view_if_needed()
    page.wait_for_function(
        """() => [...document.querySelectorAll('#gallery img')]
             .every(i => i.complete)""",
        timeout=10_000)

    sizes = page.evaluate(
        """() => [...document.querySelectorAll('#gallery img')].map(i => (
            {src: i.getAttribute('src'), w: i.naturalWidth, h: i.naturalHeight}))"""
    )
    assert len(sizes) == EXPECTED

    for s in sizes:
        assert (s["w"], s["h"]) == SLIDE, (
            f"{s['src']} decoded as {s['w']}x{s['h']}, not the {SLIDE[0]}x{SLIDE[1]} "
            "the brand engine renders — a missing file reports 0x0")


def test_every_slide_says_what_it_shows(page, live_server):
    """Alt text, because a wall of pictures with none is a wall of nothing to
    anybody reading with a screen reader."""
    page.goto(live_server)
    expect(page.locator("#gallery")).to_be_visible()

    alts = page.evaluate(
        """() => [...document.querySelectorAll('#gallery img')]
             .map(i => (i.getAttribute('alt') || '').trim())"""
    )
    assert all(len(a) > 10 for a in alts), f"thin or missing alt text: {alts}"


def test_the_gallery_does_not_pass_written_words_off_as_generated(page, live_server):
    """The one sentence this section is not allowed to lose.

    Everything else on this page that looks like product output IS product
    output, made in front of the visitor. These three are the exception: the
    engine is real, the words are written, the grounds are drawn. Saying so
    costs a line and is the difference between a sample and a claim."""
    page.goto(live_server)
    expect(page.locator("#gallery")).to_be_visible()

    text = page.locator("#gallery").inner_text().lower()
    # Two words rather than two sentences: the wording is the copywriter's, the
    # admission is not optional. An `or` between a full sentence and a single
    # word would have been false precision — the sentence half could never fail
    # on its own.
    assert "written" in text, (
        "the gallery no longer says its words are written rather than generated")
    assert "drawn" in text, (
        "the gallery no longer says its backgrounds are drawn rather than "
        "photographed")


def test_a_phone_still_gets_a_slide_to_look_at(page, live_server):
    """Three full-width portraits is about 1400 points of scrolling for a
    sample, so two of them step aside below 640 — but exactly two. Hiding the
    row entirely would make "what comes out" a section that shows nothing on
    the device most people arrive on."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(live_server)
    expect(page.locator("#gallery")).to_be_visible()

    shown = page.evaluate(
        """() => [...document.querySelectorAll('#gallery img')]
             .filter(i => i.getClientRects().length).length"""
    )
    assert shown == 1, f"a phone shows {shown} of the gallery slides, not 1"


def test_a_wide_screen_gets_all_three(page, live_server):
    """The other half of the pair above: a rule that hides two on a phone and
    forgets to bring them back is the same bug wearing a different width."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(live_server)
    expect(page.locator("#gallery")).to_be_visible()

    shown = page.evaluate(
        """() => [...document.querySelectorAll('#gallery img')]
             .filter(i => i.getClientRects().length).length"""
    )
    assert shown == EXPECTED, f"a wide screen shows {shown} slides, not {EXPECTED}"
