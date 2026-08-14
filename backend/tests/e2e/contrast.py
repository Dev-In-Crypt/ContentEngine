"""Is this text actually readable, in both themes.

The product is monochrome by design and its greys are all driven by theme
variables — but the *semantic* colours were not. Warnings, errors and successes
were written as stock Tailwind (`text-yellow-300`, `bg-red-950`, …) back when
the only theme was dark, and the light theme's palette overrides never covered
them. The result is pale-yellow-on-near-white, which is what a person reported,
and a muddy brown banner in the dark theme, which is what they reported next.

"Looks wrong" is not testable; contrast is. WCAG's ratio is computed from the
element's own colour against the first ancestor that actually paints a
background — which is the part a naive test gets wrong, because most of these
elements are transparent and inherit the page.
"""
from playwright.sync_api import Locator, Page

#: WCAG 2.1 AA for body text. Large text is allowed 3.0; nothing here is large,
#: and holding small text to the stricter number is the point.
AA = 4.5

_RATIO = """(el) => {
  const chan = (v) => {
    v /= 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  };
  const lum = (c) => {
    const p = (c.match(/[\\d.]+/g) || []).map(Number);
    return 0.2126 * chan(p[0]) + 0.7152 * chan(p[1]) + 0.0722 * chan(p[2]);
  };
  // The first ancestor that paints something. Walking up matters: almost every
  // status line in this app is transparent and sits on the page or on a card.
  const painted = (node) => {
    for (let n = node; n; n = n.parentElement) {
      const c = getComputedStyle(n).backgroundColor;
      const p = (c.match(/[\\d.]+/g) || []).map(Number);
      if (p.length && (p.length < 4 || p[3] > 0.5)) return c;
    }
    return getComputedStyle(document.body).backgroundColor;
  };
  const a = lum(getComputedStyle(el).color);
  const b = lum(painted(el));
  const [hi, lo] = a > b ? [a, b] : [b, a];
  return (hi + 0.05) / (lo + 0.05);
}"""


def ratio(el: Locator) -> float:
    """Contrast between this element's text and whatever is behind it."""
    return el.evaluate(_RATIO)


def assert_readable(page: Page, el: Locator, what: str, minimum: float = AA) -> None:
    """The same text, in both themes, above the AA floor.

    Both themes on purpose: every one of these colours was legible in exactly
    one of them, which is how they survived this long.
    """
    for theme in ("light", "dark"):
        page.evaluate("t => applyTheme(t)", theme)
        page.wait_for_timeout(60)
        got = ratio(el)
        assert got >= minimum, f"{what} in the {theme} theme: {got:.2f}:1, want {minimum}"
