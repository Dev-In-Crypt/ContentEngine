"""How big the words are, measured rather than judged.

The owner's words were "the fonts are small". Measured on the running site:
369 pieces of text on the Generate screen, 296 of them between 12 and 14 pixels,
eighteen of them below 12, and nothing anywhere larger than 18. Two sizes two
pixels apart carrying eighty per cent of an interface is not a hierarchy, and
that — not the number of things on screen — is what reads as cluttered.

Two claims, because "small" and "flat" are different failures:

  * nothing is below the floor, so no text is uncomfortable to read;
  * something on each screen is well above it, so the eye is told where to start.

Both walk the rendered page rather than the source. A size that arrives from a
Tailwind utility, an arbitrary value, an inline style or a stylesheet override
is the same size to a reader, and this measures what the browser computed.
"""
import json

import pytest

from tests.e2e.nav import open_section, open_settings

pytestmark = pytest.mark.e2e

#: The floor. Below this the product had a brand label, a queue badge, an
#: allowance meter and a dozen captions — none of them unimportant enough to
#: deserve it.
FLOOR_PX = 13

#: What "there is something to read first" means. The largest type in the whole
#: product was 18px, one step above body text.
HEADLINE_PX = 24


def _serve(page):
    """Enough state that the chrome renders everything it can.

    The allowance meter and the brand card are the two smallest things in the
    shell and both are hidden by default — a fixture that leaves them out would
    pass this file while the 10px text it was written for sits on screen. Same
    trap as an empty screen never overflowing.
    """
    page.route("**/api/usage", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "today": {"cost": 1.24, "tokens": 900, "calls": 12},
            "month": {"cost": 3.18, "tokens": 9000, "calls": 120},
            "by_model": [{"model": "anthropic/claude-sonnet", "cost": 2.29, "calls": 80}],
            "free": {"remaining": 1, "limit": 2}})))
    page.route("**/api/accounts", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "accounts": [{"id": "b1", "name": "Northbeam", "is_primary": True},
                         {"id": "b2", "name": "Halden", "is_primary": False}],
            "active_account_id": "b1"})))
    page.route("**/api/posts*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps([
            {"id": "p1", "topic": "Grinder settings that actually matter",
             "format": "single", "status": "draft", "platform": "instagram",
             "variant_group_id": "g1", "created_at": "2026-08-12T09:00:00+00:00"}])))


#: Every element that owns visible text, with what the browser gives it.
#: `getClientRects()` rather than a class check: an element inside a collapsed
#: `<details>` or behind `hidden` has no boxes, and measuring text nobody can
#: see would fail this file for the wrong reason.
_MEASURE = """
() => {
  const out = [];
  document.querySelectorAll('body *').forEach(el => {
    if (!el.getClientRects().length) return;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden') return;
    const owns = [...el.childNodes].some(
      n => n.nodeType === 3 && n.textContent.trim());
    if (!owns) return;
    out.push({
      size: parseFloat(cs.fontSize),
      tag: el.tagName.toLowerCase(),
      cls: String(el.className || '').slice(0, 70),
      text: el.textContent.trim().slice(0, 45),
    });
  });
  return out;
}
"""


def _report(rows) -> str:
    """Smallest first, with enough of each element to find it in the markup."""
    return "\n".join(
        f"  {m['size']:g}px  <{m['tag']} class={m['cls']!r}>  {m['text']!r}"
        for m in sorted(rows, key=lambda m: m["size"]))


@pytest.mark.parametrize("where", ["create", "queue", "results"])
def test_nothing_is_smaller_than_thirteen_pixels(page, signed_in, where):
    """The floor, on the screens the rail reaches.

    The rail is on all of them, and the rail held the three smallest things in
    the product — so any one of these screens catches a regression there.
    """
    _serve(page)
    signed_in()
    open_section(page, where)

    small = [m for m in page.evaluate(_MEASURE) if m["size"] < FLOOR_PX]

    assert not small, f"text below the floor on {where!r}:\n{_report(small)}"


def test_the_settings_screen_has_no_small_print_either(page, signed_in):
    """Settings is where the captions live — "updates when you save", the
    threshold note, the logo hint. It is also where nobody looks for a design
    regression."""
    _serve(page)
    signed_in()
    open_settings(page, "profiles")

    small = [m for m in page.evaluate(_MEASURE) if m["size"] < FLOOR_PX]

    assert not small, f"text below the floor in Settings:\n{_report(small)}"


@pytest.mark.parametrize("door", ["creator", "business"])
def test_the_landing_has_no_small_print_either(page, live_server, door):
    """The floor applies to the page a stranger sees first.

    This file was written for the signed-in shell and stopped there, so the home
    page kept its own rules — and phase 13 promptly put an 11.2px plate on it,
    inside a block whose entire purpose was to cure the page looking thin. The
    rule was measured and enforced two commits earlier; the gap was that nothing
    measured here.
    """
    page.goto(live_server)
    page.locator(f"#ltab-{door}").click()
    page.wait_for_timeout(200)

    small = [m for m in page.evaluate(_MEASURE) if m["size"] < FLOOR_PX]

    assert not small, f"text below the floor on the {door} door:\n{_report(small)}"


def test_the_screen_has_something_to_read_first(page, signed_in):
    """The other half of the complaint.

    A floor alone would be satisfied by setting every word on the page to the
    same comfortable size, which is the flatness that was there to begin with.
    This asks the opposite question: is anything on the screen big enough to be
    where the eye starts?
    """
    _serve(page)
    signed_in()
    open_section(page, "create")

    biggest = max(page.evaluate(_MEASURE), key=lambda m: m["size"])

    assert biggest["size"] >= HEADLINE_PX, (
        f"the largest text on Generate is {biggest['size']:g}px "
        f"({biggest['text']!r}) — nothing announces itself")
