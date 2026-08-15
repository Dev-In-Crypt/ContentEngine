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
