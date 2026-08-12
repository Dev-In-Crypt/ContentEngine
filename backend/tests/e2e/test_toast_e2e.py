"""The toast is on screen, not merely in the DOM.

Every other assertion this suite makes about a toast reads its text — and
`to_contain_text()` matches a hidden element as happily as a visible one. That
is how #toast spent the product's whole life inside `<section id="step-4">`,
the composer's result screen, which is `display:none` on every other screen:
139 `toast()` calls wrote their message into a box nobody could see, and five
green browser assertions said the messages had arrived.

So the assertion here is `to_be_visible()`, and it is made on a screen that is
NOT the composer. The email-verification link is the right one to make it on:
it is the flow where the invisible toast actually cost something — the visitor
clicks the link in their mail, lands signed out, and is told nothing at all.
"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_a_toast_is_visible_on_a_screen_that_is_not_the_composer(page, live_server):
    """/verify?token=… is handled at boot, long before any post exists, and it
    answers with a toast. Signed out, so the landing is what's on screen — the
    z-40 overlay the toast has to sit above."""
    page.goto(f"{live_server}/verify?token=deliberately-invalid")
    toast = page.locator("#toast")
    expect(toast).to_be_visible()
    expect(toast).to_contain_text("Invalid or expired link")


def test_the_toast_is_not_covered_by_the_screen_it_appears_over(page, live_server):
    """Visible is not the same as reachable by the eye: `display` can be fine
    while a full-screen overlay paints on top. The landing is `fixed inset-0
    z-40` and the auth screens are z-50, so the browser is asked the only
    question that settles it — what is actually painted at the toast's own
    coordinates.
    """
    page.goto(f"{live_server}/verify?token=deliberately-invalid")
    expect(page.locator("#toast")).to_be_visible()
    on_top = page.evaluate(
        """() => {
            const t = document.getElementById('toast');
            const r = t.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2,
                                                 r.top + r.height / 2);
            return !!hit && (hit === t || t.contains(hit));
        }"""
    )
    assert on_top, "something is painted over #toast where its text is"
