"""Terms and Privacy — the two pages served beside the app.

They are the pages somebody opens because they are being careful, and they had
been left behind by every visual phase since they were written: their own
palette hard-coded in each file, `color-scheme: dark` forced whatever the
visitor chose, a different type family. Clicking "Terms" from a light home page
landed on what looked like a different company.

Held here: they follow the theme, their text clears the same contrast floor as
everything else, they carry the product's typeface, and they lead back. The
reading column is deliberately NOT the home page's — a legal document at 1200
points is unreadable — so width is not asserted, only that it stays a column.
"""
import pytest
from playwright.sync_api import expect

from tests.e2e.contrast import AA, ratio

pytestmark = pytest.mark.e2e

PAGES = ["/terms", "/privacy"]


def _lum(page, selector: str) -> float:
    return page.eval_on_selector(
        selector,
        r"""el => {
            const p = (getComputedStyle(el).backgroundColor.match(/[\d.]+/g) || [])
                        .map(Number);
            return (0.2126*p[0] + 0.7152*p[1] + 0.0722*p[2]) / 255;
        }""")


@pytest.mark.parametrize("path", PAGES)
def test_the_page_follows_the_theme(page, live_server, path):
    """Both directions. The old version was dark in both, which is not "a dark
    page" — it is a page that ignores the person reading it."""
    page.goto(live_server + path)

    page.evaluate("() => { localStorage.setItem('theme', 'light');"
                  " document.documentElement.setAttribute('data-theme', 'light'); }")
    page.wait_for_timeout(60)
    light = _lum(page, "body")

    page.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
    page.wait_for_timeout(60)
    dark = _lum(page, "body")

    assert light > 0.8, f"{path} is not light in the light theme ({light:.2f})"
    assert dark < 0.2, f"{path} is not dark in the dark theme ({dark:.2f})"


@pytest.mark.parametrize("path", PAGES)
def test_the_theme_is_set_before_the_page_paints(page, live_server, path):
    """theme.js has to be in the head of these pages too. Without it a visitor
    on dark opens a white page and then watches it turn — on the page they
    opened because they were already suspicious."""
    page.goto(live_server + path)
    assert page.evaluate(
        "() => !!document.querySelector('head script[src*=\"theme.js\"]')"), \
        f"{path} does not load theme.js in its head"


@pytest.mark.parametrize("path", PAGES)
@pytest.mark.parametrize("what,selector", [
    ("the body text", "p"),
    ("the small print", ".muted"),
    ("a link", "a"),
])
def test_the_legal_text_is_readable(page, live_server, path, what, selector):
    """The same ratio and the same floor as the rest of the product, but the
    theme is flipped by attribute rather than through `applyTheme` — that
    function lives in app.js and these pages, correctly, do not load the
    application to show a document."""
    page.goto(live_server + path)
    el = page.locator(selector).first
    expect(el).to_be_visible()

    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)",
                      theme)
        page.wait_for_timeout(60)
        got = ratio(el)
        assert got >= AA, (
            f"{what} on {path} in the {theme} theme: {got:.2f}:1, want {AA}")


@pytest.mark.parametrize("path", PAGES)
def test_the_page_uses_the_products_typeface(page, live_server, path):
    """Half of "this looks like another site" was the font, not the colour."""
    page.goto(live_server + path)
    family = page.eval_on_selector("body", "el => getComputedStyle(el).fontFamily")
    assert "Barlow" in family, f"{path} is set in {family!r}"


@pytest.mark.parametrize("path", PAGES)
def test_the_page_leads_back(page, live_server, path):
    page.goto(live_server + path)
    back = page.locator("a[href='/']")
    expect(back.first).to_be_visible()


@pytest.mark.parametrize("path", PAGES)
def test_the_page_keeps_no_colours_of_its_own(page, live_server, path):
    """The failure this whole change is about, stated so it cannot come back:
    a colour written into the page instead of taken from a token is a colour
    that will still be the 2024 palette in 2027."""
    import urllib.request
    with urllib.request.urlopen(live_server + path) as r:   # noqa: S310 — local fixture
        html = r.read().decode()
    assert "<style>" not in html, (
        f"{path} carries its own stylesheet again; the shared one is "
        "/static/legal.css")
    assert "#161826" not in html, f"{path} still hard-codes the old ground colour"


@pytest.mark.parametrize("path", PAGES)
def test_the_page_fits_a_phone(page, live_server, path):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(live_server + path)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1, f"{path} scrolls {overflow}px sideways on a phone"
