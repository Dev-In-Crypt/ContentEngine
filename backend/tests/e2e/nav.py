"""How a browser test reaches a screen.

Every navigating test used to carry its own one-line helper — seven of them,
three byte-identical copies of `_open_video_tab` among them. That is fine while
the navigation is stable and awful the moment it isn't: UX phase 3 collapses
fourteen top-level sections into four, moves half of them into a tabbed Settings
screen, and folds Photos and Video into Create. Spread across seven files that
is a sixty-seven-test sweep; here it is a few lines.

So the rule is: **a test says where it wants to be, never how to get there.**
Nothing outside this module should contain `[data-section=...]`.

The names below are the destinations as the product will describe them after
phase 3. Today several of them are still separate top-level buttons, and the
mapping says so — when the nav is rewritten, only the mapping moves.
"""
from playwright.sync_api import expect

#: First-run setup: the container, and the control that leaves it. Both change
#: shape in UX phase 5 (a modal becomes a full screen); nothing outside this
#: module should name either.
_ONBOARDING = "#onboarding-screen"
_ONBOARDING_DISMISS = "I'll do this later"

#: Destination → its nav button. Four of them, which is the whole point of
#: phase 3; everything else is reached through `open_settings` or `open_create`.
_SECTION_BUTTON = {
    "create": "create",
    "calendar": "calendar",
    "results": "results",
    "queue": "queue",
}

#: Every Settings tab. Sources and Rules were top-level Business buttons until
#: 3.8; that they are ordinary tabs now is exactly what this set records.
_SETTINGS_TAB = {"profiles", "connections", "keys", "sources", "rules", "team"}

#: Create mode → the panel that must be on screen once we arrive. Photo and
#: Video stopped being sections in 3.5; they are shapes of the same act.
_CREATE_VIEW = {
    "post": "#create-post-panel",
    "photo": "#create-photo-panel",
    "video": "#create-video-panel",
    "leads": "#create-leads-panel",
}


def _click(page, section: str) -> None:
    page.locator(f'[data-section="{section}"]').click()


def open_section(page, name: str) -> None:
    """Go to a top-level section by its destination name."""
    _click(page, _SECTION_BUTTON[name])


def open_settings(page, tab: str = "profiles") -> None:
    """Open Settings on a given tab, through the avatar menu the product uses."""
    assert tab in _SETTINGS_TAB, tab
    page.locator("#avatar-btn").click()
    page.locator("#avatar-menu").get_by_text("Settings").click()
    page.locator(f'#settings-tabs [data-settings-tab="{tab}"]').click()
    expect(page.locator("#view-settings")).to_be_visible()


def open_create(page, mode: str = "post") -> None:
    """Open Create in one of its three modes, and wait for it to be on screen.

    The wait is not politeness: the photo and video screens fetch their grids on
    entry, and a test that starts asserting before the container is visible races
    the first render.
    """
    _click(page, "create")
    page.locator(f'#create-modes [data-create-mode="{mode}"]').click()
    expect(page.locator(_CREATE_VIEW[mode])).to_be_visible()


#: Results tab -> the panel that must be on screen once we arrive. Three
#: separate top-level screens until 3.7; the Journal and Source analytics tabs
#: exist only for a Business account, which is why `open_results` is the only
#: honest way to reach them.
_RESULTS_VIEW = {
    "posts": "#results-posts",
    "sources": "#results-sources",
    "journal": "#results-journal",
}

#: Calendar view mode -> its panel. The feed grid stopped being a section in
#: 3.7: it is the same posts on a second pair of glasses, not a second place.
_CALENDAR_VIEW = {"calendar": "#calendar-panel", "profile": "#profile-panel"}


def open_results(page, tab: str = "posts") -> None:
    """Open Results on one of its tabs, and wait for the panel to be on screen."""
    _click(page, "results")
    page.locator(f'#results-tabs [data-results-tab="{tab}"]').click()
    expect(page.locator(_RESULTS_VIEW[tab])).to_be_visible()


def open_calendar(page, mode: str = "calendar") -> None:
    """Open the Calendar in one of its two view modes."""
    _click(page, "calendar")
    page.locator(f'#calendar-modes [data-calendar-mode="{mode}"]').click()
    expect(page.locator(_CALENDAR_VIEW[mode])).to_be_visible()


def open_configure(page) -> None:
    """Unfold Create's "Configure" row.

    Everything the composer asks beyond the topic itself lives in a collapsed
    <details> since 4.8. Playwright cannot fill an input inside a closed one,
    and — worse — an assertion that a field is hidden would pass for the wrong
    reason. So a test that touches those fields says so, once, here.
    """
    row = page.locator("#configure-row")
    if not row.evaluate("el => el.open"):
        page.locator("#configure-summary").click()
    expect(page.locator("#tone")).to_be_visible()


def compose(page, topic: str = "Sourdough starters") -> None:
    """Put a topic in and stand where Generate is.

    Today that is two moves — fill, then press "Next →" onto a second step.
    Phase 10 merges those steps into one screen, at which point this is the fill
    alone. That is exactly why it lives here: four test files carried their own
    copy of these three lines, so a change none of them are about would
    otherwise be a seventy-one-test sweep.

    The wait is on the Generate button rather than on `#step-2`, for the reason
    this module exists: a test says where it wants to be, and where it wants to
    be is "able to generate", not "on the second of two steps".
    """
    page.locator("#topic").fill(topic)
    page.get_by_role("button", name="Next →").click()
    expect(page.locator("#generate-btn")).to_be_visible()


def reach_preview(page) -> None:
    """Compose, generate, and land on the result screen.

    The caller routes `**/api/posts/generate` before calling this — what comes
    back is the test's subject, and this module has no business inventing it.
    """
    compose(page)
    page.locator("#generate-btn").click()
    expect(page.locator("#step-4")).to_be_visible()


def dismiss_onboarding(page) -> None:
    """Get first-run setup out of the way, then carry on.

    It has to be WAITED for rather than polled once: it opens a tick after load,
    so an immediate is_visible() says no and the dismissal never happens. A
    timeout is not fatal — whether setup appears at all is its own tests'
    business, not every other screen's.

    This lived in four copies (the signed_in fixture and three in the account
    file) because nav.py never held it. Phase 5 then replaced the modal with a
    real screen — and the change cost these two constants, which is the whole
    point of the module.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    box = page.locator(_ONBOARDING)
    try:
        # Let the boot finish before concluding that setup is not coming.
        # `maybeStartOnboarding` runs after four awaited loads, so on a busy CI
        # runner the screen can arrive later than a bare five-second poll allows.
        # The old version then returned quietly, and the screen opened a moment
        # afterwards over a test about something else — which is how a suite that
        # is green on a laptop fails in CI with "#onboarding-screen intercepts
        # pointer events" in a file that never mentions onboarding.
        page.wait_for_load_state("networkidle")
        box.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeout:
        return
    page.get_by_text(_ONBOARDING_DISMISS).click()
    box.wait_for(state="hidden")


def open_onboarding(page) -> None:
    """Re-enter first-run setup the way the product offers it."""
    page.locator("#avatar-btn").click()
    page.locator("#avatar-menu").get_by_text("Setup guide").click()
    expect(page.locator(_ONBOARDING)).to_be_visible()


def open_rules(page) -> None:
    """Open the brand-rules screen and wait for it to finish filling itself in.

    It loads its four fields from two requests after the click, and anything
    typed before the second lands gets overwritten by it — a real, if narrow,
    window for a user on a slow link, and a race a test must not run into.
    `limits` is fetched last, so its response is the signal the screen settled.
    Phase 3.4 splits rules and limits onto different tabs, which removes the
    second request and with it the reason this helper exists.
    """
    with page.expect_response("**/api/business/limits") as res:
        open_settings(page, "rules")
    assert res.value.ok
