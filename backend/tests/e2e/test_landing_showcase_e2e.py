"""The product, shown at the size of an argument.

Phase 13 put three facsimiles of the running interface on the home page, which
was the right answer to "this page has no picture of the product". Phase 15 is
about the next complaint: they were drawn small, stacked in one column, each in
a section of its own with air around it — so the page managed to be both empty
and busy, and the thing it was showing arrived as a thumbnail.

What is held here is size and rhythm, not taste. A showcase has to take real
width on a wide screen, and consecutive showcases have to sit on alternating
sides — one long column of identical blocks is the shape this phase is
replacing, and it is the shape a later edit would drift back into.

What each showcase may CONTAIN is held elsewhere and deliberately not repeated:
no interactive controls and no images (tests/e2e/test_landing_honesty_e2e.py),
nothing below the readable floor (tests/e2e/test_type_scale_e2e.py).
"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

#: Wide enough that a two-column showcase is expected to be side by side.
WIDE = 1440

#: A showcase is the page's main exhibit. Below this it is an illustration
#: beside the text rather than the thing the text is about.
#:
#: Chosen to separate the two layouts rather than picked for roundness: in the
#: page's own container the product's seven twelfths are about 644px, and in the
#: 1024-wide column these blocks used to live in it is about 541. A softer
#: number passes both, which makes it a guard against nothing — the first
#: version of this file had 460 and survived the mutation it existed to catch.
MIN_CARD_WIDTH = 600


def _showcases(page):
    return page.locator("#showcases [data-showcase]")


def _boxes(page):
    """Left edge and width of each showcase's prose and its facsimile.

    Raises rather than returning None halves: a missing marker is a real
    failure, and letting it through produces a TypeError three frames away
    from the thing that is actually wrong."""
    boxes = _raw_boxes(page)
    for b in boxes:
        assert b["prose"] and b["card"], (
            f"showcase {b['name']} is missing its prose column "
            f"([data-showcase-copy]) or its .ce-card")
    return boxes


def _raw_boxes(page):
    return page.evaluate(
        """() => [...document.querySelectorAll('#showcases [data-showcase]')].map(b => {
            const prose = b.querySelector('[data-showcase-copy]');
            const card  = b.querySelector('.ce-card');
            const r = el => { const x = el.getBoundingClientRect();
                              return {x: x.x, w: x.width}; };
            return {name: b.dataset.showcase, prose: prose && r(prose),
                    card: card && r(card)};
        })"""
    )


def test_every_showcase_names_its_two_halves(page, live_server):
    """The prose column is marked so the two below can measure it. Without the
    marker they would silently measure nothing and pass."""
    page.set_viewport_size({"width": WIDE, "height": 1000})
    page.goto(live_server)
    expect(_showcases(page).first).to_be_visible()

    for b in _raw_boxes(page):
        assert b["prose"], f"showcase {b['name']} has no [data-showcase-copy]"
        assert b["card"], f"showcase {b['name']} has no .ce-card to show"


def test_the_product_is_shown_at_size(page, live_server):
    """Not a thumbnail beside a paragraph."""
    page.set_viewport_size({"width": WIDE, "height": 1000})
    page.goto(live_server)
    expect(_showcases(page).first).to_be_visible()

    for b in _boxes(page):
        assert b["card"]["w"] >= MIN_CARD_WIDTH, (
            f"showcase {b['name']} draws the product {b['card']['w']:.0f}px wide "
            f"on a {WIDE}px screen — that is an illustration, not an exhibit")


def test_the_showcases_alternate_sides(page, live_server):
    """Text left, product right, then the other way round.

    Measured against each other rather than against a class name: what matters
    is that the eye is not walked down one identical column, and the class that
    achieves it is nobody's business but the markup's."""
    page.set_viewport_size({"width": WIDE, "height": 1000})
    page.goto(live_server)
    expect(_showcases(page).first).to_be_visible()

    boxes = _boxes(page)
    assert len(boxes) >= 3, f"expected three showcases, found {len(boxes)}"

    sides = [("copy-first" if b["prose"]["x"] < b["card"]["x"] else "product-first")
             for b in boxes]
    # strict=False on purpose: these are consecutive pairs, so the shifted
    # sequences are one shorter and stopping at the shortest is the intent.
    for a, b, sa, sb in zip(boxes, boxes[1:], sides, sides[1:], strict=False):
        assert sa != sb, (
            f"{a['name']} and {b['name']} are both {sa}; consecutive showcases "
            "alternate so the page is read as compositions, not as a list")


def test_a_narrow_screen_stacks_them_instead(page, live_server):
    """Side by side is a wide-screen answer. At 390 the two halves sit one above
    the other, and the page still does not scroll sideways."""
    page.set_viewport_size({"width": 390, "height": 900})
    page.goto(live_server)
    expect(_showcases(page).first).to_be_visible()

    for b in _boxes(page):
        assert abs(b["prose"]["x"] - b["card"]["x"]) < 8, (
            f"showcase {b['name']} still puts its halves side by side at 390px")

    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1, f"the page scrolls {overflow}px sideways on a phone"


# ── the other door, offered rather than hidden ──────────────────────────────


def test_the_creator_door_points_at_the_business_one(page, live_server):
    """Somebody posting for a company lands on the creator half, because that is
    the tab the page opens on. The two doors are a switch at the very top, and a
    visitor eight screens down does not scroll back to look for it.

    The offer switches the tab in place. There is no /business page in this
    product — main.py serves exactly two routes beside the app — so a link to
    one would be the broken promise the footer already refuses to make."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(live_server)
    expect(page.locator("#landing-creator")).to_be_visible()

    offer = page.locator("#landing-creator [data-action='set-landing-tab'][data-arg='business']")
    expect(offer).to_have_count(1)
    offer.click()

    expect(page.locator("#landing-business")).to_be_visible()
    expect(page.locator("#landing-creator")).to_be_hidden()


def test_the_page_does_not_promise_prices_will_never_exist(page, live_server):
    """A promise the owner has already decided to break is worse than silence.

    Paid plans are planned. The money section may say what is true today —
    nothing to pay, two free posts, then your own key — and may say that using
    your own key remains supported. It may not say the words that turn that
    into forever, because the people who would remember them are exactly the
    people the sentence was written to attract."""
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()

    for door in ("creator", "business"):
        page.locator(f"#ltab-{door}").click()
        text = page.locator("#landing-screen").inner_text().lower()
        for forever in ("not later", "never pay", "free forever",
                        "always be free", "no subscription, ever"):
            assert forever not in text, (
                f"the {door} door promises {forever!r} — paid plans are on the "
                "roadmap, and this is the sentence that would be taken back")


# ── bands and movement ──────────────────────────────────────────────────────


def test_the_page_is_not_one_flat_colour(page, live_server):
    """Twelve sections in one ground read as one long scroll with nothing
    telling the eye where an argument ends. Counted as distinct painted
    backgrounds rather than by class name, because the class is the mechanism
    and the bands are the point."""
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()

    grounds = page.evaluate(
        """() => [...new Set([...document.querySelectorAll('#landing-screen section')]
             .filter(el => el.getClientRects().length)
             .map(el => getComputedStyle(el).backgroundColor)
             .filter(c => c && c !== 'rgba(0, 0, 0, 0)'))]"""
    )
    assert len(grounds) >= 3, (
        f"the landing paints {len(grounds)} background(s): {grounds}")


def test_the_closing_call_is_readable_on_its_dark_band(page, live_server):
    """The one band that does not follow the theme, so it is the one place a
    token can be the wrong colour by construction. The light theme's ink is
    near-black and its button fills with near-black; either of them left alone
    here is invisible on this ground.

    Measured in the light theme on purpose — in the dark theme the mistake
    cannot happen, which is exactly why nobody would notice it."""
    page.goto(live_server)
    page.evaluate("() => { localStorage.setItem('theme', 'light'); applyTheme('light'); }")
    page.wait_for_timeout(100)

    band = page.locator(".mk-band-dark")
    expect(band).to_have_count(1)
    band.scroll_into_view_if_needed()

    ink = page.evaluate(
        r"""() => {
            const b = document.querySelector('.mk-band-dark');
            const btn = b.querySelector('.ce-btn');
            const lum = c => { const [r, g, bl] = c.match(/\d+/g).map(Number);
                               return (0.2126*r + 0.7152*g + 0.0722*bl) / 255; };
            return {band: lum(getComputedStyle(b).backgroundColor),
                    heading: lum(getComputedStyle(b.querySelector('h2')).color),
                    button: lum(getComputedStyle(btn).backgroundColor)};
        }"""
    )
    assert ink["band"] < 0.25, f"the closing band is not dark: {ink}"
    assert ink["heading"] > 0.6, (
        f"the heading is {ink['heading']:.2f} bright on a {ink['band']:.2f} "
        "ground — dark ink left on the one band that does not follow the theme")
    assert ink["button"] > 0.6, (
        f"the button fills at {ink['button']:.2f} on a {ink['band']:.2f} ground "
        "— a black button on a black band")


def test_motion_respects_the_setting(page, browser, live_server):
    """`prefers-reduced-motion` is set by people for whom movement causes real
    symptoms. The reveal must be off for them, and the content must still be
    there — an animation that hides its element and then declines to show it is
    the worst of both."""
    ctx = browser.new_context(reduced_motion="reduce")
    p = ctx.new_page()
    try:
        p.goto(live_server)
        expect(p.locator("#landing-screen")).to_be_visible()
        state = p.evaluate(
            """() => {
                const el = document.querySelector('#landing-screen section');
                const s = getComputedStyle(el);
                return {opacity: parseFloat(s.opacity), transition: s.transitionDuration};
            }"""
        )
        assert state["opacity"] == 1, (
            f"a section sits at opacity {state['opacity']} with reduced motion on")
        assert state["transition"] in ("0s", "0s, 0s"), (
            f"movement is still timed at {state['transition']} with reduced motion on")
    finally:
        ctx.close()


def test_the_page_still_shows_up_without_the_reveal(page, live_server):
    """The starting state lives in app.js, not the markup, so a page whose
    script never ran shows everything rather than nothing. Asserted by reading
    the HTML the server sends, because that is the only version a broken script
    leaves behind."""
    import re
    import urllib.request
    with urllib.request.urlopen(live_server) as r:      # noqa: S310 — local fixture
        html = r.read().decode()
    # Class attributes only. The name appears in the stylesheet by definition,
    # and matching that instead would be a test that can never pass.
    wearing = [c for c in re.findall(r'class="([^"]*)"', html)
               if "mk-rise" in c.split()]
    assert not wearing, (
        f"{len(wearing)} element(s) ship the hidden state in the markup; if "
        "app.js fails to run, the home page is blank")


def test_the_first_sections_arrive_immediately(page, live_server):
    """The reveal must not be a delay. Anything already on screen intersects at
    once, so the top of the page is never waiting on a scroll that a visitor
    who came to read the headline has not made yet."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()

    # Waited for, not slept on. A fixed 300ms pause landed in the middle of the
    # half-second transition and reported 0.84 as a failure — the trap this
    # repository's own notes describe as reading a computed style once.
    page.wait_for_function(
        """() => {
            const el = document.querySelector('#landing-screen section');
            return el && parseFloat(getComputedStyle(el).opacity) === 1;
        }""", timeout=4000)


def test_the_footer_links_go_somewhere(page, live_server):
    """The footer is where invented links go to hide.

    This product serves two pages beside the app; a "Docs" or "GitHub" column
    would look tidy and lead four different names to the same home page. So
    every href is checked against what exists: the two real routes, an address,
    or an anchor that is actually on the page."""
    page.goto(live_server)
    expect(page.locator("footer")).to_be_visible()

    hrefs = page.eval_on_selector_all("footer a", "els => els.map(e => e.getAttribute('href'))")
    assert hrefs, "the footer has no links at all"

    real_routes = {"/terms", "/privacy"}
    for href in hrefs:
        if href.startswith("mailto:"):
            assert "@" in href, f"{href!r} is not an address"
        elif href.startswith("#"):
            found = page.locator(href).count()
            assert found == 1, (
                f"the footer points at {href!r} and the page has {found} of them")
        else:
            assert href in real_routes, (
                f"the footer links to {href!r}, which this product does not "
                "serve — main.py has /terms and /privacy and nothing else")


def test_the_two_legal_pages_actually_answer(page, live_server):
    """Following them, not reading the attribute. These are the links a person
    clicks when they are already suspicious."""
    for path in ("/terms", "/privacy"):
        res = page.goto(live_server + path)
        assert res.status == 200, f"{path} answered {res.status}"


def test_no_two_sections_wear_the_same_label(page, live_server):
    """Section labels are how a reader knows they have moved on.

    Two consecutive sections under one label read as a single section that lost
    its place — which is what "WHAT YOU GET" over both the showcases and the
    outcomes did, and what nobody noticed until the whole page was seen at once
    rather than a screen at a time. Counted per door, because the two doors are
    allowed to reuse a label; a visitor sees only one of them."""
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()

    for door in ("creator", "business"):
        page.locator(f"#ltab-{door}").click()
        labels = page.evaluate(
            """() => [...document.querySelectorAll('#landing-screen .mk-eyebrow')]
                 .filter(el => el.getClientRects().length)
                 .map(el => el.innerText.trim())"""
        )
        dupes = {t for t in labels if labels.count(t) > 1}
        assert not dupes, f"the {door} door labels two sections {dupes} each"


def test_every_section_is_the_same_width(page, live_server):
    """One column, not five.

    The page was assembled over four phases and carried four container widths —
    1200 for the new sections, 1024, 768 and 672 for older ones. Nobody reads
    that as variety; it reads as edges that do not line up, which is what the
    owner saw. Measured on the content container rather than on the section,
    because the banded sections are full-bleed by design and hold their column
    in a child.
    """
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()

    for door in ("creator", "business"):
        page.locator(f"#ltab-{door}").click()
        widths = page.evaluate(
            """() => [...document.querySelectorAll('#landing-screen section')]
                 .filter(e => e.getClientRects().length)
                 .map(e => {
                     const inner = e.classList.contains('mk-wrap')
                         ? e : e.querySelector(':scope > .mk-wrap') || e;
                     return {w: Math.round(inner.getBoundingClientRect().width),
                             text: (e.innerText || '').trim().slice(0, 30)};
                 })"""
        )
        odd = [x for x in widths if x["w"] != widths[0]["w"]]
        assert not odd, (
            f"on the {door} door these sections are a different width from the "
            f"rest ({widths[0]['w']}px): "
            + "; ".join(f"{x['w']}px {x['text']!r}" for x in odd))
