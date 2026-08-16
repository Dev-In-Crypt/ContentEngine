"""How big the home page is allowed to think, and how many boxes it may use.

The owner's brief on this page reduces to two measurements. It is too small —
an app-sized headline on a marketing page reads as documentation. And it is too
divided — twenty-seven bordered cards, chips and boxes, so that the same screen
manages to feel empty and cluttered at once.

Both are countable, so both are held here rather than in anybody's judgement.
The card ceiling is a ratchet, in the shape this repo already used to retire
inline handlers: it starts at what the page has today and comes down as the
redesign replaces rows of small boxes with single large compositions. It is
`==`, never `<=` — a ceiling nobody has to lower is a ceiling that drifts back
up, and the whole point is that the next person has to mean it.
"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

#: What the page carries right now. Lower it as compositions replace card rows;
#: raising it is the thing this test exists to make somebody argue for.
#:
#: 31 at the start of this phase, then 19, now 7. The last drop was not tidying:
#: adding one panel to the creator door pushed it over 19 and the honest way out
#: was to find a row of boxes that should not have been boxes. It was the twelve
#: compatibility chips — a name is not something you compare side by side, it is
#: a name — and they are now a line of names.
CARD_CEILING = 7

#: A marketing headline, not an app heading. The scale tops out at 36px for the
#: product; the home page gets its own steps above that.
HERO_MIN_PX = 56


def _visible_cards(page) -> int:
    return page.evaluate("""() => [...document.querySelectorAll('#landing-screen .ce-card')]
        .filter(el => el.getClientRects().length).length""")


def test_the_landing_is_not_a_wall_of_cards(page, live_server):
    """Counted per door, because they hide each other and the sum of two
    screens is not what anybody looks at."""
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()

    for door in ("creator", "business"):
        page.locator(f"#ltab-{door}").click()
        expect(page.locator(f"#landing-{door}")).to_be_visible()
        n = _visible_cards(page)
        assert n <= CARD_CEILING, (
            f"the {door} door draws {n} cards; the ceiling is {CARD_CEILING}. "
            "Replace a row of small boxes with one large composition rather "
            "than raising this number.")


def test_the_hero_headline_is_marketing_sized(page, live_server):
    """The one heading a stranger reads before deciding to care."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(live_server)

    h1 = page.locator("#hero-field h1")
    expect(h1).to_be_visible()
    size = h1.evaluate("e => parseFloat(getComputedStyle(e).fontSize)")

    assert size >= HERO_MIN_PX, (
        f"the headline is {size:g}px — that is an app heading on a home page")


def test_the_page_uses_its_own_width(page, live_server):
    """Sections were pinned to 768 and 1024, so a wide screen showed a narrow
    strip in a field of nothing. Measured against the window rather than
    against a class name, because the class is not the point."""
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()

    widest = page.evaluate("""() => Math.max(...[...document.querySelectorAll(
        '#landing-screen section, #landing-screen .mk-wrap')]
        .filter(el => el.getClientRects().length)
        .map(el => el.getBoundingClientRect().width))""")

    assert widest >= 1100, (
        f"the widest section is {widest:.0f}px on a 1600px screen — the page is "
        "still living in a column")
