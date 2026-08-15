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
    # A price WE charge. Narrowed in phase 14, and the narrowing is the point:
    # the first version banned "per month" and "$N/mo" outright, which also
    # forbids the true and useful sentence "on your own key this runs to a few
    # dollars a month". What must never appear is a price for THIS product,
    # because there is no billing code in it — no payment library, no webhook,
    # no plan table, no checkout route; README and ROADMAP say the same.
    (r"\bper seat\b", "a price we charge"),
    (r"\$\d+\s*/\s*(mo|month|seat)\b", "a price we charge"),
    (r"\b(pricing|choose (a )?plan|start (your )?subscription|upgrade to)\b",
     "a plan that does not exist"),
    (r"\bfree (trial|for \d+ days)\b", "a trial, which implies a paid tier"),
]

#: Kling's per-second figures are NOT from Kling. The catalogue says so beside
#: them: reseller quotes from the day they were written, which "drift, sometimes
#: by a lot", and it asks in writing that they be re-verified against the current
#: price sheet "before this ships anywhere a user makes a spending decision from
#: it". A home page is exactly that place. Text and image prices are the
#: vendors' own published rates and may be quoted; these may not.
_VIDEO_PRICES = [
    (r"\$0?\.\d+\s*(/|per)\s*(second|sec\b)", "a per-second video price"),
    (r"\$0\.75", "the quoted price of a 10-second clip"),
    (r"\$0\.375", "the quoted price of a 5-second clip"),
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
        # The WHOLE screen, once per door — not the door's own element.
        #
        # The second version of this read `#landing-{tab}` plus `#hero-field`,
        # three hand-picked containers, and so was blind to everything outside
        # them. A section added before the footer — which is where phase 14 put
        # the one about money — was invisible to every guard in this file. That
        # is worse than a false negative on one test: it is an escape hatch from
        # the fabrication check for any block placed outside the two doors.
        # Reading the screen while a door is open covers the hero, that door and
        # every shared section; two passes cover both doors.
        seen.append(page.locator("#landing-screen").inner_text())
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


def test_no_video_price_reaches_the_landing(page, live_server):
    """The one number in this product that must not be published.

    `services/ai/catalog.py` carries per-second Kling prices and, directly above
    them, a note that they are reseller quotes rather than the vendor's, that
    they drift, and that they must be re-verified "before this ships anywhere a
    user makes a spending decision from it". Someone will eventually move them
    here for completeness — "the page talks about cost, and we know this one".
    This is the sentence that stops them.

    Text and image prices are different: those are the vendors' own published
    per-million-token rates, and phase 14 quotes one of them on purpose.
    """
    page.goto(live_server)
    text = _landing_text(page)

    found = [(m.group(0), why) for pat, why in _VIDEO_PRICES
             for m in [re.search(pat, text, re.IGNORECASE)] if m]

    assert not found, "the landing quotes a video price the code says to re-verify:\n" + \
        "\n".join(f"  {frag!r} — {why}" for frag, why in found)


def test_the_quoted_price_matches_the_catalogue(page, live_server):
    """The one number on the page that lives somewhere else too.

    A price copied into markup is a second copy, and the second copy drifts —
    which is the exact bug phase 10 found on this same page, where the gate
    promised five free posts and the server gave two. So the landing's figure is
    tied to the table it came from, the way that gate is now tied to
    FREE_POST_LIMIT.

    These are the vendor's own published per-million-token rates, which is why
    they may be quoted at all; the video prices in the same catalogue may not,
    and have their own test.

    The binding is "whichever catalogue model the copy names", not
    `default_text_model`. The first version of this test used that setting and
    failed against a model the page had never mentioned — because it is a
    per-deployment env var: one value on this machine, another on the e2e
    server, another in production. Static markup cannot follow it, and a test
    that expects it to is testing the wrong thing.
    """
    from services.ai.catalog import PROVIDERS

    page.goto(live_server)
    text = _landing_text(page)
    rows = [m for p in PROVIDERS.values() for m in p["text_models"]]
    named = [m for m in rows if m["label"] in text]

    assert named, ("the page quotes a price but names no model from the catalogue, "
                   "so nothing ties the figure to anything")
    for row in named:
        for price, side in ((row["price_in"], "input"), (row["price_out"], "output")):
            shown = f"${price:g}"
            assert shown in text, (
                f"the page names {row['label']} but its {side} price is not the "
                f"catalogue's {shown}")


def test_spend_is_called_an_estimate_where_it_is_one(page, live_server):
    """"See what you spend" is literally true on OpenRouter only.

    That provider returns its own cost and the app stores it. For OpenAI,
    Anthropic and Google the tokens are measured but the dollars are computed
    here from a hand-kept table, and a model outside that table records $0.00.
    The product already says this on the spend screen; the home page must not
    make the larger promise the app declines to make.
    """
    page.goto(live_server)
    text = _landing_text(page).lower()

    assert "estimate" in text, "the page promises a spend figure without saying when it is one"


def test_the_compatibility_list_matches_the_code(page, live_server):
    """"Works with" is a claim, and the cheapest kind to get wrong.

    Buffer puts nine of these in its hero. Ours are marked up by role so the
    claim is checkable: a name offered as a place we PUBLISH to must be in
    `PUBLISHABLE_PLATFORMS`, and a name offered as a model provider must be a
    label in the catalogue.

    LinkedIn is the reason this exists. The product generates for it and refuses
    to publish to it — a hard gate in `posts.py`, not an oversight — so putting
    it in the publish list would be a promise the server declines twice.
    """
    from services.ai.catalog import PROVIDERS
    from services.publishing.factory import PUBLISHABLE_PLATFORMS

    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()

    publish = page.locator('[data-compat="publish"]')
    assert publish.count() > 0, "nothing is offered as a publishing destination"
    for i in range(publish.count()):
        name = publish.nth(i).inner_text().strip().lower()
        assert name in PUBLISHABLE_PLATFORMS, \
            f"the page offers publishing to {name!r}, which has no publisher"

    labels = {p["label"] for p in PROVIDERS.values()}
    models = page.locator('[data-compat="model"]')
    for i in range(models.count()):
        name = models.nth(i).inner_text().strip()
        assert name in labels, f"the page names {name!r}, which is not a wired provider"


def test_the_money_section_never_charges_you(page, live_server):
    """Talking about cost must not turn into taking payment.

    Phase 14 adds a section about money, and the shape of that mistake is not a
    lie in a sentence — it is a button. There is no checkout in this product, so
    anything that looks like one leads a person to a door that does not open.
    """
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()

    for word in ("buy", "checkout", "subscribe", "upgrade", "billing", "payment"):
        assert page.locator(
            f"#landing-screen button:has-text('{word}'), "
            f"#landing-screen a:has-text('{word}')").count() == 0, \
            f"the landing offers a control that says {word!r}"


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
