"""The first screen: one question, one field.

Step 1 used to ask seven things — network, topic, niche, audience, tone, extra
instructions, caption length — plus an X block, before anything had been
written. Six of those have sensible answers already: the brand profile knows the
niche and the audience, and the rest have defaults that are right most of the
time. Asking them up front turns "write me a post" into a form.

So the question is the question, the field is the topic, and everything else
folds into one row that says what it currently holds. Nothing is removed and no
id changes — `setNetwork` toggles half of these elements by id, and the X block
still lives here rather than in Settings so the thread choice stays one click
away.

The collapsed row is also why `nav.open_configure` exists: Playwright cannot
fill an input inside a closed <details>, and an assertion about a hidden field
would otherwise pass for the wrong reason.
"""
import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_configure, open_create

pytestmark = pytest.mark.e2e


def test_the_first_screen_asks_one_thing(page, signed_in):
    signed_in()
    expect(page.locator("#topic")).to_be_visible()
    expect(page.locator("#configure-row")).to_be_visible()
    # ...and the rest is folded away rather than deleted.
    expect(page.locator("#niche")).to_be_hidden()
    expect(page.locator("#tone")).to_be_hidden()
    expect(page.locator("#net-toggle")).to_be_hidden()


def test_the_row_says_what_it_is_holding(page, signed_in):
    """A collapsed row that says only "Configure" makes people open it to find
    out whether it matters. Showing the values answers that without a click."""
    signed_in()
    expect(page.locator("#configure-summary")).to_contain_text("Instagram")
    expect(page.locator("#configure-summary")).to_contain_text("Professional")


def test_opening_the_row_reveals_everything_it_holds(page, signed_in):
    signed_in()
    open_configure(page)
    for field in ("#net-toggle", "#niche", "#audience", "#tone",
                  "#instructions", "#length-tier"):
        expect(page.locator(field)).to_be_visible()


def test_the_summary_follows_the_network(page, signed_in):
    """setNetwork already rewrites half of this row; the summary is the part a
    user sees without opening it, so it has to keep up."""
    signed_in()
    open_configure(page)
    page.locator("#net-toggle-x").click()
    expect(page.locator("#configure-summary")).to_contain_text("X")
    expect(page.locator("#configure-summary")).not_to_contain_text("Instagram")


def test_the_summary_follows_an_edit(page, signed_in):
    signed_in()
    open_configure(page)
    page.locator("#tone").select_option("casual")
    expect(page.locator("#configure-summary")).to_contain_text("Casual")


# ── one screen, not two (UX phase 10.2) ─────────────────────────────────────
#
# Format and image source used to live on a second step behind a "Next →". Every
# tool that does this for a living has converged on the same shape: one field,
# one button, and on the way there only the choices that change the SHAPE of the
# result. Tone, length, niche and the rest change its style, and style is what
# the collapsed row is for.

def test_the_composer_is_one_screen(page, signed_in):
    """Generate is reachable without pressing anything first."""
    signed_in()

    expect(page.locator("#generate-btn")).to_be_visible()
    assert page.get_by_role("button", name="Next →").count() == 0


def test_the_shape_of_the_post_is_on_the_screen(page, signed_in):
    """Format and image source stay out in the open while everything textual
    folds away. Choosing to upload your own photos is not a setting — it is a
    different intention, and two clicks deep is where intentions go to die."""
    signed_in()

    expect(page.locator("#format-group")).to_be_visible()
    expect(page.locator("#source-btns")).to_be_visible()
    expect(page.locator("#tone")).to_be_hidden()


def test_a_topic_that_is_too_short_never_reaches_the_server(page, signed_in):
    """The three-character floor used to sit on the "Next →" click. That button
    is gone, and `generatePost` only ever checked for emptiness — so without
    moving the rule, "ab" would have become a paid generation.
    """
    signed_in()
    calls = []
    page.route("**/api/posts/generate", lambda r: (calls.append(1), r.abort()))

    page.locator("#topic").fill("ab")
    page.locator("#generate-btn").click()

    expect(page.locator("#toast")).to_contain_text("at least 3 characters")
    expect(page.locator("#step-1")).to_be_visible()
    assert calls == []


def test_the_row_stays_open_once_opened(page, signed_in):
    """Somebody who opened it is working in it — snapping shut on the next
    keystroke or network switch would be its own small betrayal."""
    signed_in()
    open_configure(page)
    page.locator("#net-toggle-x").click()
    expect(page.locator("#tone")).to_be_visible()


# ── the delegated change/input listeners (CSP phase 4) ──────────────────────
#
# Click has always had a test on every screen; change and input did not, and
# the mutation pass proved it — deleting either dispatcher broke nothing. These
# two are the cheapest honest coverage: one observable effect each.

def test_typing_a_niche_updates_the_collapsed_summary(page, signed_in):
    """`input` reaches the registry. The Configure row shows its own values in
    the summary line so the row can stay closed, and that line is redrawn from
    a delegated input listener now rather than an inline oninput."""
    signed_in()
    open_create(page)
    open_configure(page)

    page.locator("#niche").fill("Sourdough")

    expect(page.locator("#configure-summary")).to_contain_text("Sourdough")


def test_changing_the_tone_updates_it_too(page, signed_in):
    """The same summary, reached from a <select> rather than a text field.

    Note what this does NOT prove: the Configure row carries data-input and
    data-change on one container, and a select fires both, so removing the
    change dispatcher leaves this green. The change path is proved instead by
    test_the_cost_estimate_scales_with_duration in the video library, where the
    element carries data-change alone — which is how the mutation pass found
    that this test was claiming more than it showed.
    """
    signed_in()
    open_create(page)
    open_configure(page)

    page.locator("#tone").select_option("casual")

    expect(page.locator("#configure-summary")).to_contain_text("Casual")
