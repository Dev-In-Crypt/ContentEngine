"""The first-run wizard, driven by a real browser.

Every assertion here corresponds to something that was broken in the code at some
point and was caught by hand rather than by a test: a step advancing when its
input was empty, a success line wiped by the re-render that advanced it, a
checklist claiming more than it knew.
"""
import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_settings

pytestmark = pytest.mark.e2e

WIZARD = "#onboarding-modal"


def _progress(page) -> str:
    """A one-shot read, for asserting the wizard has NOT moved."""
    return page.locator("#onb-progress").inner_text()


def _expect_step(page, n: int, total: int = 4) -> None:
    """Wait until the wizard is really on step `n`.

    The Continue handler is async: click() returns once the click is dispatched,
    not once the save it fires has come back. It disables #onb-primary while it
    waits, so clicking Continue again is naturally safe — but Skip is a separate
    button that stays enabled, so clicking it straight after races the request in
    flight. Waiting on the counter is what makes a sequence of steps
    deterministic instead of a bet on how fast the machine is.
    """
    expect(page.locator("#onb-progress")).to_have_text(f"Step {n} of {total}")


def test_the_wizard_greets_a_brand_new_creator(page, signup):
    signup()
    expect(page.locator(WIZARD)).to_be_visible()
    expect(page.locator("#onb-title")).to_contain_text("brand")
    assert _progress(page) == "Step 1 of 4"
    expect(page.locator("#onb-back")).to_be_hidden()


def test_a_business_account_gets_its_own_steps(page, signup):
    signup(account_type="business")
    expect(page.locator(WIZARD)).to_be_visible()
    expect(page.locator("#onb-title")).to_contain_text("AI model")
    assert _progress(page) == "Step 1 of 3"


def test_an_empty_niche_does_not_advance(page, signup):
    signup()
    page.locator("#onb-primary").click()
    expect(page.locator("#onb-result")).to_contain_text("Add your niche")
    assert _progress(page) == "Step 1 of 4"


def test_the_brand_step_saves_and_moves_on(page, signup):
    signup()
    page.locator("#onb-niche").fill("Artisan bakery")
    page.locator("#onb-primary").click()
    expect(page.locator("#onb-title")).to_contain_text("AI model")
    assert _progress(page) == "Step 2 of 4"
    # OpenRouter is preselected so a new user has one decision, not three.
    expect(page.locator("#wiz-ai-provider")).to_have_value("openrouter")
    assert page.locator("#wiz-ai-model").input_value() != ""


def test_the_ai_step_refuses_to_advance_without_a_key(page, signup):
    signup()
    page.locator("#onb-niche").fill("Artisan bakery")
    page.locator("#onb-primary").click()
    page.locator("#onb-primary").click()          # no key pasted
    expect(page.locator("#onb-result")).to_contain_text("Paste your API key")
    assert _progress(page) == "Step 2 of 4"


def test_the_publishing_step_swaps_fields_per_network(page, signup):
    signup()
    page.locator("#onb-niche").fill("Artisan bakery")
    page.locator("#onb-primary").click()
    _expect_step(page, 2)                         # the brand save has landed
    page.locator("#onb-skip").click()             # past the AI key
    expect(page.locator("#onb-title")).to_contain_text("place to post")
    expect(page.locator('[data-wiz-cred="x_api_key"]')).to_be_visible()
    page.locator("#onb-body").get_by_role("button", name="Instagram").click()
    expect(page.locator('[data-wiz-cred="instagram_access_token"]')).to_be_visible()
    expect(page.locator('[data-wiz-cred="x_api_key"]')).to_have_count(0)


def test_the_final_checklist_reports_only_what_is_true(page, signup):
    signup()
    page.locator("#onb-niche").fill("Artisan bakery")
    page.locator("#onb-primary").click()
    _expect_step(page, 2)                         # the brand save has landed
    page.locator("#onb-skip").click()
    _expect_step(page, 3)
    page.locator("#onb-skip").click()
    _expect_step(page, 4)
    body = page.locator("#onb-body").inner_text()
    assert "✅ Brand profile" in body
    assert "○ AI key saved" in body               # skipped, so not claimed


def test_closing_the_wizard_keeps_it_closed_but_setup_guide_reopens_it(page, signup):
    signup()
    page.get_by_text("Close setup").click()
    expect(page.locator(WIZARD)).to_be_hidden()

    page.reload()
    expect(page.locator(WIZARD)).to_be_hidden()   # dismissal is remembered

    open_settings(page, "connections")
    page.get_by_role("button", name="Setup guide").click()
    expect(page.locator(WIZARD)).to_be_visible()


def test_a_returning_account_with_a_profile_is_not_nagged(page, signup, live_server):
    signup()
    page.locator("#onb-niche").fill("Artisan bakery")
    page.locator("#onb-primary").click()
    page.evaluate("localStorage.removeItem('onboarding_done')")
    page.goto(live_server)
    # A brand is on file but no AI key, so it still has something to offer —
    # and it must open on the step that is actually missing.
    expect(page.locator(WIZARD)).to_be_visible()
    expect(page.locator("#onb-title")).to_contain_text("brand")


def test_a_step_outcome_survives_the_move_to_the_next_step(page, signup):
    """The advance re-renders the modal, and the re-render used to clear the very
    line that said the step worked — so every success was invisible. Driven
    through the page's own functions because the steps that produce a green
    message need a provider key or the network."""
    signup()
    page.evaluate("""() => {
        S.wiz.ids = ['brand', 'ai', 'publish', 'done'];
        S.wiz.step = 1;
        wizRender();
        wizSay('✅ gpt-5 works.', true);
        wizNext();
    }""")
    expect(page.locator("#onb-progress")).to_have_text("Step 3 of 4")
    expect(page.locator("#onb-result")).to_contain_text("gpt-5 works")
