"""What the home page is not allowed to say, and how wide it is allowed to be.

Four competitors' home pages were read before this phase — buffer.com,
predis.ai, sproutsocial.com, hootsuite.com. All four fill the middle of the page
the same way: a user count and a wall of client logos directly under the hero
("241,945 creators", "6.4M+ users", six brand logos), then customer numbers with
a name attached ("Honda: 251% increase in community engagement"), then reviews
with faces and star ratings, then a price table.

This product has no customers, no reviews and no billing. Every one of those
devices is proof that gets earned, and drawing it is a lie on the first screen a
stranger sees — not softened by "example", "sample" or a lighter grey.

So the phase that fills this page has to be prevented from filling it the easy
way. That is what this file is: the decision, made executable, for the first
conversation about conversion in which somebody suggests "let's just add some
logos".

The page is allowed to say what the PRODUCT does — sizes, counts, what it
publishes to, how long it takes. Those are checkable inside the product. What it
may not do is describe people who do not exist.
"""
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


#: Claims about other people. Each pattern is a shape the four competitors use.
#: Deliberately about the CLAIM, not about a word: "trusted" alone is fine in
#: "your keys stay encrypted", and it is "trusted by" that sells a customer.
_FABRICATIONS = [
    (r"\btrusted by\b", "a customer claim"),
    (r"\bloved by\b", "a customer claim"),
    (r"\bjoin \d", "a user count"),
    (r"\b\d[\d,. ]*(k|m|\+)?\s*(happy\s+)?(users|creators|customers|brands|marketers|teams)\b",
     "a user count"),
    (r"\b\d(\.\d)?\s*/\s*5\b", "a rating"),
    (r"★|⭐", "a star rating"),
    (r"\btestimonial", "a testimonial"),
    (r"\bcase stud", "a case study"),
    (r"\bwhat our (users|customers)", "a testimonial"),
    (r"\bg2\b", "a review-site badge"),
    (r"\bas seen (in|on)\b", "a press claim"),
    (r"\bper (seat|month|user)\b", "a price"),
    (r"\$\d+\s*/\s*(mo|month|seat)", "a price"),
]


def _landing_text(page) -> str:
    """Both doors, read one at a time.

    `inner_text` returns only what is VISIBLE, and the two doors hide each
    other. The first version of this clicked through both tabs and then read
    once — so it measured the business door twice and never saw the creator
    one. It passed against a landing carrying "Trusted by 10,000 creators ·
    4.9/5 on G2", which is how it was found: the guard was mutation-tested, not
    trusted for being green.
    """
    expect(page.locator("#landing-screen")).to_be_visible()
    seen = []
    for tab in ("creator", "business"):
        page.locator(f"#ltab-{tab}").click()
        expect(page.locator(f"#landing-{tab}")).to_be_visible()
        seen.append(page.locator(f"#landing-{tab}").inner_text())
    # The hero and the footer sit outside both doors and are always on screen.
    seen.append(page.locator("#hero-field").inner_text())
    return "\n".join(seen)


def test_the_landing_claims_no_customers_it_does_not_have(page, live_server):
    """The main guard of the phase.

    It fails the moment somebody adds the thing every competitor has, which is
    the moment it is meant to fail. If the day comes that these claims are true,
    this test is deleted in the same commit that makes them true — and that
    deletion is the record of when the product earned them.
    """
    page.goto(live_server)
    text = _landing_text(page)

    found = [(m.group(0), why) for pat, why in _FABRICATIONS
             for m in [re.search(pat, text, re.IGNORECASE)] if m]

    assert not found, "the landing makes claims the product has not earned:\n" + \
        "\n".join(f"  {frag!r} — {why}" for frag, why in found)


def test_the_result_says_what_it_is(page, live_server):
    """The page's one real advantage, made legible.

    Every competitor shows a screenshot of their own interface. This page hands
    a stranger a finished post — and said nothing about it, so the strongest
    thing on the site read as a decorative preview. The line under it counts
    what actually came back: no adjectives, nothing about other people, only
    what is in the response and therefore checkable.

    It must not be there before a run. A page that says "14 hashtags, 1080×1350"
    over an empty box is describing something that has not happened.
    """
    from tests.e2e import test_landing_e2e as L

    page.route("**/api/demo/post", lambda r: r.fulfill(
        status=200, content_type="text/event-stream",
        body=L._sse({"type": "complete", "post": L._post(
            hashtags=["#a", "#b", "#c", "#d", "#e"])})))
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()
    expect(page.locator("#hero-facts")).to_be_hidden()

    page.locator("#hero-input").fill("Sourdough starters")
    page.locator("#hero-run").click()

    facts = page.locator("#hero-facts")
    expect(facts).to_be_visible()
    expect(facts).to_contain_text("5")          # the hashtags actually returned
    expect(facts).to_contain_text("1080")       # the size the engine renders at


def test_every_showcase_is_drawn_from_the_product(page, live_server):
    """The showcase blocks are the product's own markup, not pictures of it.

    Three ways to show a product on a home page were weighed. Captured PNGs are
    what everyone else does, and they go stale on the first interface change —
    phase 12 moved half of the Generate screen — need a second set for the dark
    theme, and put binaries in a repository with no build step. Drawn mock-ups
    are always handsome and always lying: they show an idea of the product.

    So these blocks are built from the same classes and the same tokens as the
    running interface. Both themes come free, nothing goes stale by style, and
    the day somebody replaces one with a screenshot this fails.

    It is still a facsimile — the content in it is written, not generated — and
    that is exactly why it may only show what the product actually does.
    """
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()

    blocks = page.locator("[data-showcase]")
    # Three on the creator door, one on the business one. Counted rather than
    # left open, so a block cannot be quietly dropped when a screen changes.
    expect(blocks).to_have_count(4)

    for i in range(4):
        block = blocks.nth(i)
        name = block.get_attribute("data-showcase")
        assert block.locator("img").count() == 0, \
            f"the {name!r} showcase uses a picture instead of the product's markup"
        # A class from the product's own families, so it cannot quietly become
        # a hand-drawn approximation that merely looks similar.
        assert block.locator(".ce-card, .seg-btn, .ce-input, .ce-btn, .ce-btn-ghost").count() > 0, \
            f"the {name!r} showcase shares nothing with the interface it depicts"
        # And nothing in it may be pressable. A facsimile carrying a live-looking
        # control answers a press with silence, which is a worse first
        # impression than the empty page this phase set out to fix.
        assert block.locator("button, a, input, select, textarea").count() == 0, \
            f"the {name!r} showcase offers a control that does nothing"


def test_the_landing_fits_a_phone(page, live_server):
    """The signed-in shell is measured at 390px by test_narrow_e2e; the landing
    is the half of the product a stranger sees first and had no such guard. A
    showcase block is exactly the kind of thing that refuses to fold."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()

    for tab in ("creator", "business"):
        page.locator(f"#ltab-{tab}").click()
        page.wait_for_timeout(150)
        overflow = page.evaluate(
            "Math.max(0, document.documentElement.scrollWidth - window.innerWidth)")
        assert overflow <= 1, f"the {tab} door is {overflow}px wider than the screen"
