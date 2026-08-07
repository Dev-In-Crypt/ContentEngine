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

from tests.e2e.nav import open_configure

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


def test_a_topic_is_still_required(page, signed_in):
    """The only validation the wizard has ever had, and folding the rest away
    must not lose it."""
    signed_in()
    page.locator("#topic").fill("ab")
    page.locator("#step-1").get_by_text("Next").click()
    expect(page.locator("#step-1")).to_be_visible()
    expect(page.locator("#step-2")).to_be_hidden()


def test_the_row_stays_open_once_opened(page, signed_in):
    """Somebody who opened it is working in it — snapping shut on the next
    keystroke or network switch would be its own small betrayal."""
    signed_in()
    open_configure(page)
    page.locator("#net-toggle-x").click()
    expect(page.locator("#tone")).to_be_visible()
