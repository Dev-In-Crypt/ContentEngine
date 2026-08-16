"""Text on the new bands, measured in both themes.

Phase 15 gave the home page four grounds instead of one — panel, brand tint,
and a near-black close that deliberately does not follow the theme. Every one
of them changes what the text on it is being read against, and the plan named
this as the thing to check rather than to assume: a colour that works on the
page background is not thereby a colour that works on a band.

The dark band is the sharp case. It is the same near-black in both themes, so
in the light theme every inherited token — the ink, the muted grey, the button
fill — is the wrong end of the scale. That mistake is invisible in the dark
theme, which is exactly why measuring only one theme is measuring nothing.

Uses the same walker as the rest of the product's contrast tests: the ratio is
computed against the first ancestor that actually paints, because most of this
text is transparent and inherits whatever band it is sitting in.
"""
import pytest
from playwright.sync_api import expect

from tests.e2e.contrast import assert_readable

pytestmark = pytest.mark.e2e


#: One representative piece of running text per band, plus the two places a
#: token could be inherited from the wrong theme. Keyed by what to say when it
#: fails, because "assert 3.9 >= 4.5" tells nobody which band went wrong.
SAMPLES = [
    ("the showcase prose", "#showcases [data-showcase-copy] p"),
    ("the gallery caption", "#gallery p"),
    ("the money section's body text", ".mk-band-tint p"),
    ("the closing band's heading", ".mk-band-dark h2"),
    ("the closing band's second line", ".mk-band-dark .mk-quiet"),
    ("the footer's links", "footer a"),
    ("a section eyebrow", "#showcases > div:first-child"),
    ("the showcase micro-label", "[data-showcase='editor'] .ce-card div"),
    ("the how-it-works eyebrow", "#how-it-works > div:first-child"),
    ("the footer's copyright", "footer .mt-10"),
]


@pytest.mark.parametrize("what,selector", SAMPLES, ids=[s[1] for s in SAMPLES])
def test_text_on_the_new_bands_is_readable(page, live_server, what, selector):
    page.goto(live_server)
    expect(page.locator("#landing-screen")).to_be_visible()

    el = page.locator(selector).first
    expect(el).to_have_count(1)
    assert_readable(page, el, what)


def test_the_closing_button_is_visible_on_its_own_band(page, live_server):
    """Not text, so the ratio walker does not apply — but the same failure.

    `.ce-btn` fills with --accent, which is near-black in the light theme. On a
    band that is near-black in both themes that is a black button on a black
    ground: present in the DOM, gone from the page. Measured as the distance
    between the two fills rather than as a colour name, so a later palette
    change is checked rather than trusted.
    """
    page.goto(live_server)
    band = page.locator(".mk-band-dark")
    expect(band).to_have_count(1)

    for theme in ("light", "dark"):
        page.evaluate("t => applyTheme(t)", theme)
        page.wait_for_timeout(60)
        gap = page.evaluate(
            r"""() => {
                const b = document.querySelector('.mk-band-dark');
                const btn = b.querySelector('.ce-btn');
                const lum = el => {
                    const p = (getComputedStyle(el).backgroundColor
                              .match(/[\d.]+/g) || []).map(Number);
                    return (0.2126*p[0] + 0.7152*p[1] + 0.0722*p[2]) / 255;
                };
                return Math.abs(lum(btn) - lum(b));
            }"""
        )
        assert gap > 0.4, (
            f"in the {theme} theme the closing button and its band differ by "
            f"{gap:.2f} in brightness — the button has disappeared into it")
