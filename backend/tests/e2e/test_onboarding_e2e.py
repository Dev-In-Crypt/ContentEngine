"""First-run setup: three questions and a post, on a real screen.

It was a modal over the app: four steps that asked for a niche, an AI key and
publishing credentials before showing anything — precisely the three things the
UX document says not to ask for. The key moves to the moment generation runs out
(phase 6) and the credentials to the first publish, which leaves three
questions worth asking and one thing worth showing.

A real screen rather than an overlay, and that is not cosmetic. The modal's
backdrop covered the app and silently ate the first click of every test that
forgot it — a hazard documented at length in two fixtures and worked around in
four files. A screen has nothing behind it to mis-click.

Two things here are guards rather than layout. Escape must NOT dismiss this
(it is a screen, and there is a visible way out), and the "where did I stop"
key is namespaced by account — the old one was global, so two accounts in one
browser shared a verdict about whether setup had been done.
"""
import json

import pytest
from playwright.sync_api import expect

from tests.e2e.nav import dismiss_onboarding, open_onboarding

pytestmark = pytest.mark.e2e

SCREEN = "#onboarding-screen"

#: A 1x1 PNG. The route re-encodes anything odd through Pillow before it gets
#: here, so the mime is always one the logo endpoint accepts.
PNG = ("data:image/png;base64,"
       "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
       "hKmMIQAAAABJRU5ErkJggg==")


def _state(page):
    return page.evaluate("localStorage.getItem('onboarding:' + S.user.id)")


def _account_type(page):
    return page.evaluate("S.user.account_type")


def _settled(page):
    """Wait for the boot to finish before asserting that setup did NOT appear.

    The screen starts hidden in the markup and `maybeStartOnboarding` runs after
    four awaited loads, so a bare `to_be_hidden()` passes on its first check —
    before the app has had the chance to show it. That is a test that passes for
    the wrong reason, and it did: the mutation that removes the "already done"
    check went straight through it.
    """
    page.wait_for_load_state("networkidle")


# ── screen 1: what do you run ───────────────────────────────────────────────

def test_a_brand_new_account_lands_on_the_first_question(page, signup):
    signup()
    expect(page.locator(SCREEN)).to_be_visible()
    expect(page.locator("#onb-s1")).to_be_visible()
    for choice in ("creator", "business", "agency"):
        expect(page.locator(f'[data-onb-type="{choice}"]')).to_be_visible()


def test_choosing_your_own_channel_moves_on_without_a_reload(page, signup):
    """creator↔agency needs no reboot — only crossing the business boundary
    changes which shell the app bootstraps into."""
    signup()
    page.locator('[data-onb-type="creator"]').click()
    expect(page.locator("#onb-s2")).to_be_visible()
    assert _account_type(page) == "creator"


def test_choosing_clients_accounts_records_an_agency(page, signup):
    """The signup form only ever offered two doors, so this is the first place
    in the product where somebody can say they run clients' accounts — and the
    agency shell has been waiting since 3.9."""
    signup()
    page.locator('[data-onb-type="agency"]').click()
    expect(page.locator("#onb-s2")).to_be_visible()
    assert _account_type(page) == "agency"
    assert page.evaluate("document.body.dataset.accountType") == "agency"


# ── screen 2: your website ──────────────────────────────────────────────────

def _to_brand(page, kind="creator"):
    page.locator(f'[data-onb-type="{kind}"]').click()
    expect(page.locator("#onb-s2")).to_be_visible()


def _extract(page, body, status=200):
    page.route("**/api/brand/extract", lambda r: r.fulfill(
        status=status, content_type="application/json", body=json.dumps(body)))


def _read(name="Crumb & Co", niche="Sourdough baking", audience="Home bakers",
          colors=("#8a4b2a",), logo=None):
    return {"source_url": "https://crumb.example", "name": name,
            "description": "A neighbourhood sourdough bakery.",
            "niche": niche, "target_audience": audience,
            "colors": list(colors), "logo_data_url": logo}


def test_the_website_field_comes_first(page, signup):
    """One field and a button. The manual form is behind "I don't have a
    website" — it is the fallback, not the first thing asked."""
    signup()
    _to_brand(page)
    expect(page.locator("#onb-site")).to_be_visible()
    expect(page.locator("#onb-niche")).to_be_hidden()


def test_reading_a_site_fills_the_fields_as_a_proposal(page, signup):
    """Never saved silently: every field is a guess from markup nobody is
    obliged to get right, so it arrives editable."""
    signup()
    _to_brand(page)
    _extract(page, _read(colors=("#8a4b2a", "#f0e6d2")))
    page.locator("#onb-site").fill("https://crumb.example")
    page.locator("#onb-read-site").click()

    expect(page.locator("#onb-niche")).to_have_value("Sourdough baking")
    expect(page.locator("#onb-audience")).to_have_value("Home bakers")
    expect(page.locator("#onb-brand")).to_have_value("Crumb & Co")
    expect(page.locator('#onb-colors [data-onb-color]').first).to_be_visible()


def test_a_site_we_could_not_guess_says_so_without_calling_it_a_failure(page, signup):
    """The ordinary case for a brand-new account: the niche is guessed by an LLM
    and a tenant with no model gets "" back. The name and the colours DID
    arrive, so this is a half-success rather than an error — and the description
    seeds the niche instead of being thrown away."""
    signup()
    _to_brand(page)
    _extract(page, _read(niche="", audience=""))
    page.locator("#onb-site").fill("https://crumb.example")
    page.locator("#onb-read-site").click()

    expect(page.locator("#onb-brand-status")).to_contain_text("couldn't guess")
    expect(page.locator("#onb-brand")).to_have_value("Crumb & Co")
    expect(page.locator("#onb-niche")).not_to_have_value("")


def test_a_site_we_cannot_read_keeps_you_on_the_website_screen(page, signup):
    """The SSRF guard's refusal, a timeout, a dead host. The message is the
    server's, the fields stay, and nothing advances."""
    signup()
    _to_brand(page)
    _extract(page, {"detail": "Enter a public http(s) URL"}, status=400)
    page.locator("#onb-site").fill("https://nope.example")
    page.locator("#onb-read-site").click()

    expect(page.locator("#onb-s2")).to_be_visible()
    expect(page.locator("#onb-brand-status")).to_contain_text("public http(s) URL")


def test_no_website_reveals_the_manual_form(page, signup):
    signup()
    _to_brand(page)
    expect(page.locator("#onb-niche")).to_be_hidden()
    page.locator("#onb-no-site").click()
    expect(page.locator("#onb-niche")).to_be_visible()
    expect(page.locator("#onb-site")).to_be_hidden()


def test_the_colour_and_the_logo_are_saved_alongside_the_profile(page, signup):
    signup()
    _to_brand(page)
    _extract(page, _read(logo=PNG))
    calls = []
    page.on("request", lambda r: calls.append(r.url) if r.method in ("PUT", "POST") else None)

    page.locator("#onb-site").fill("https://crumb.example")
    page.locator("#onb-read-site").click()
    expect(page.locator("#onb-niche")).to_have_value("Sourdough baking")
    page.locator("#onb-continue-brand").click()

    expect(page.locator("#onb-s3")).to_be_visible()
    assert any("/api/settings/profile" in u for u in calls), calls
    assert any("/api/settings/slide-style" in u for u in calls), calls
    assert any("/logo" in u for u in calls), calls


def test_a_logo_that_will_not_save_does_not_trap_you(page, signup):
    """The profile is blocking — the first post is written from it. The colour
    and the logo are not: losing a favicon must not strand somebody in setup.

    The request is ABORTED rather than answered with a 500, deliberately: a 500
    is trivially non-blocking because nothing inspects res.ok, so it would prove
    nothing. An abort makes apiFetch throw, which is the failure the catch
    around the upload exists for."""
    signup()
    _to_brand(page)
    _extract(page, _read(audience="", logo=PNG))
    page.route("**/logo", lambda r: r.abort())

    page.locator("#onb-site").fill("https://crumb.example")
    page.locator("#onb-read-site").click()
    expect(page.locator("#onb-niche")).to_have_value("Sourdough baking")
    page.locator("#onb-continue-brand").click()

    expect(page.locator("#onb-s3")).to_be_visible()


def test_a_profile_that_will_not_save_keeps_you_put(page, signup):
    """The other half of the same rule: the post at the end is written from the
    profile, so advancing without it would promise something we cannot do."""
    signup()
    _to_brand(page)
    page.locator("#onb-no-site").click()
    page.locator("#onb-niche").fill("Sourdough baking")
    page.route("**/api/settings/profile", lambda r: r.fulfill(
        status=500, content_type="application/json", body=json.dumps({"detail": "nope"})))

    page.locator("#onb-continue-brand").click()
    expect(page.locator("#onb-s2")).to_be_visible()


# ── screen 2: the manual fallback ───────────────────────────────────────────

def test_the_brand_screen_will_not_continue_without_a_niche(page, signup):
    """The one rule the old wizard had that is worth keeping: the post at the
    end is written from this, and a blank profile produces a blank post."""
    signup()
    _to_brand(page)
    page.locator("#onb-no-site").click()
    page.locator("#onb-continue-brand").click()

    expect(page.locator("#onb-s2")).to_be_visible()
    expect(page.locator("#onb-brand-status")).to_contain_text("niche")


def test_a_saved_brand_moves_on_to_the_network(page, signup):
    signup()
    _to_brand(page)
    page.locator("#onb-no-site").click()
    page.locator("#onb-niche").fill("Sourdough baking")
    page.locator("#onb-audience").fill("Home bakers")
    page.locator("#onb-continue-brand").click()

    expect(page.locator("#onb-s3")).to_be_visible()
    assert page.evaluate("S.profile && S.profile.niche") == "Sourdough baking"


# ── screen 3: one network ───────────────────────────────────────────────────

def test_picking_a_network_moves_on(page, signup):
    signup()
    _to_brand(page)
    page.locator("#onb-no-site").click()
    page.locator("#onb-niche").fill("Sourdough baking")
    page.locator("#onb-continue-brand").click()
    page.locator('[data-onb-net="x"]').click()

    expect(page.locator("#onb-s4")).to_be_visible()
    assert page.evaluate("S.platform") == "x"


def test_skipping_the_network_still_moves_on(page, signup):
    """"I'll skip" has to work, or it is not a skip. The default network is the
    one the composer already has."""
    signup()
    _to_brand(page)
    page.locator("#onb-no-site").click()
    page.locator("#onb-niche").fill("Sourdough baking")
    page.locator("#onb-continue-brand").click()
    page.locator("#onb-skip-net").click()

    expect(page.locator("#onb-s4")).to_be_visible()


# ── leaving, and coming back ────────────────────────────────────────────────

def test_leaving_setup_puts_you_in_the_app(page, signup):
    signup()
    page.locator("#onb-later").click()
    expect(page.locator(SCREEN)).to_be_hidden()
    expect(page.locator("#view-create")).to_be_visible()
    assert _state(page) == "done"


def test_escape_does_not_dismiss_the_setup_screen(page, signup):
    """A screen is not a modal. Escape closing it would leave a half-configured
    account in the app with no sign that anything was skipped — and the reason
    it would happen is habit: every other overlay in this file is registered
    with the Escape handler."""
    signup()
    expect(page.locator(SCREEN)).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(SCREEN)).to_be_visible()


def test_an_interrupted_setup_resumes_where_it_stopped(page, signup):
    """Written on ENTRY to each screen, so a crash or a refresh resumes where the
    user actually was rather than where they last succeeded."""
    signup()
    page.locator('[data-onb-type="creator"]').click()
    expect(page.locator("#onb-s2")).to_be_visible()
    assert _state(page) == "2"

    page.reload()
    expect(page.locator(SCREEN)).to_be_visible()
    expect(page.locator("#onb-s2")).to_be_visible()


def test_a_finished_setup_is_not_asked_again(page, signup):
    signup()
    page.locator("#onb-later").click()
    page.reload()
    _settled(page)
    expect(page.locator(SCREEN)).to_be_hidden()


def test_the_setup_guide_reopens_it_from_the_start(page, signed_in):
    """Dismissed is not deleted: the avatar menu offers it again, and a second
    pass starts at the beginning rather than at whatever screen was last seen."""
    signed_in()
    open_onboarding(page)
    expect(page.locator("#onb-s1")).to_be_visible()


def test_a_second_account_in_the_same_browser_gets_its_own_setup(page, signup):
    """The old flag was global, so signing into a second account in the same
    browser skipped setup entirely — the app decided you had already done it
    because somebody else had."""
    signup()
    page.locator("#onb-later").click()
    expect(page.locator(SCREEN)).to_be_hidden()

    page.evaluate("logout()")
    signup()
    expect(page.locator(SCREEN)).to_be_visible()


def test_an_account_that_finished_the_old_wizard_is_not_asked_again(page, signup):
    """Every existing user carries the old global flag. Without honouring it,
    this release nags all of them once."""
    signup()
    page.locator("#onb-later").click()
    page.evaluate("""() => {
      localStorage.removeItem('onboarding:' + S.user.id);
      localStorage.setItem('onboarding_done', '1');
    }""")

    page.reload()
    _settled(page)
    expect(page.locator(SCREEN)).to_be_hidden()
