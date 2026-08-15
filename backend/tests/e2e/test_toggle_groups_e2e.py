"""The choose-one rows: format, image source, and the four inside Configure.

These buttons had no CSS rule at all. `initToggleGroup` appended six Tailwind
utilities to each one at start-up and then swapped six more on every click —
with a regular expression over `className`. That is the exact mechanism phase
10 introduced `.lt-btn` to retire, with a comment beside it explaining why: six
chances to disagree with the theme, in a file whose whole point is that colour
lives in one place. These were the last groups still doing it.

The visible half of the complaint was the selected state: a thin dark outline,
the weakest signal available, on a row of otherwise identical pills. A choice
you have to hunt for is not marked.
"""
import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_configure, open_section

pytestmark = pytest.mark.e2e

#: Every group `initToggleGroup` drives, with a value to switch to and the
#: state key it owns. Platform is here for completeness — its row is markup
#: only, kept hidden, and driven by `setNetwork`.
GROUPS = [
    ("#format-btns", "carousel_3", "format"),
    ("#source-btns", "ai_gen", "source"),
    ("#template-btns", "square", "templateStyle"),
]


def _bg(locator):
    return locator.evaluate("e => getComputedStyle(e).backgroundColor")


def _alpha(css_colour: str) -> float:
    """The alpha of an `rgb()`/`rgba()` string; 1 when it has no fourth part."""
    parts = [float(p) for p in css_colour.replace("rgba(", "").replace("rgb(", "")
             .rstrip(")").split(",")]
    return parts[3] if len(parts) > 3 else 1.0


def test_the_chosen_option_is_filled_not_outlined(page, signed_in):
    """Selection has to be visible without looking for it.

    Asserted as "the chosen one is painted, the others are not" rather than by
    class name: the claim is about what a reader can see, and the previous
    implementation satisfied every class-based assertion while marking the
    choice with a 2px outline nobody noticed.
    """
    signed_in()
    open_section(page, "create")

    chosen = page.locator('#format-btns [data-val="single"]')
    other = page.locator('#format-btns [data-val="carousel_3"]')
    expect(chosen).to_be_visible()

    assert _alpha(_bg(chosen)) > 0.5, (
        f"the chosen format is not filled: {_bg(chosen)}")
    assert _bg(chosen) != _bg(other), "chosen and unchosen look identical"


def test_the_mark_moves_when_the_choice_does(page, signed_in):
    signed_in()
    open_section(page, "create")
    single = page.locator('#format-btns [data-val="single"]')
    three = page.locator('#format-btns [data-val="carousel_3"]')
    filled, plain = _bg(single), _bg(three)

    three.click()

    expect(three).to_have_css("background-color", filled)
    expect(single).to_have_css("background-color", plain)


@pytest.mark.parametrize("selector,value,state_key", GROUPS)
def test_every_toggle_group_still_switches(page, signed_in, selector, value, state_key):
    """One function drives six rows. Restyling it is one edit and six blast
    radii, and the rows inside Configure are the ones nobody opens."""
    signed_in()
    open_section(page, "create")
    open_configure(page)

    page.locator(f'{selector} [data-val="{value}"]').click()

    assert page.evaluate(f"S.{state_key}") == value


def test_the_grounding_chip_does_not_wipe_the_image_source(page, signed_in):
    """Found while restyling these rows, by reading who `.src-btn` selects.

    The chip carries `src-btn` for its looks, which also enrols it in the image
    -source group — so `initToggleGroup` gives it a click handler that writes
    `S.source = btn.dataset.val`, and the chip has no `data-val`. Pressing it
    sets the image source to undefined and the next generation asks the server
    for a post with no source at all.

    Invisible until now because on the free tier the chip is disabled and the
    handler returns early. It bites exactly the accounts paying with their own
    key — the ones for whom the chip is a real choice.
    """
    signed_in()
    open_section(page, "create")
    chip = page.locator("#grounding-chip")
    expect(chip).to_be_enabled()
    before = page.evaluate("S.source")
    assert before, "fixture problem: no image source selected to begin with"

    chip.click()

    assert page.evaluate("S.source") == before


def test_the_grounding_chip_is_not_left_unstyled(page, signed_in):
    """It carries `src-btn` but belongs to no group — `initToggleGroup` never
    touches it, so it was drawn entirely by `renderGroundingChip`. A restyle
    that only teaches the groups a new class leaves this one bare, and it sits
    directly under the two rows being restyled where the difference shows.
    """
    signed_in()
    open_section(page, "create")

    chip = page.locator("#grounding-chip")
    expect(chip).to_be_visible()

    box = chip.evaluate("""e => {
      const s = getComputedStyle(e);
      return {pad: parseFloat(s.paddingLeft), radius: parseFloat(s.borderTopLeftRadius)};
    }""")
    assert box["pad"] >= 8, f"the grounding chip has no padding: {box}"
    assert box["radius"] >= 4, f"the grounding chip has no shape: {box}"
