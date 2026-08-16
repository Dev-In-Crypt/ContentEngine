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
