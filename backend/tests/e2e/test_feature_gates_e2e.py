"""Features that arrive when they start to mean something (UX phase 8.5).

Three gates, one file, because they share a mechanism and one escape hatch.

  Sources — offered to a creator once they have five posts behind them, as a
  question rather than a hidden button. Sources are workspace-scoped and sit
  behind require_business (`Source.workspace_id`, `business.py`), so a creator
  cannot simply be shown the tab: the capability lives in the Business product,
  and the honest offer says so and lets them decide.

  Team — an agency screen about a second person, which means nothing to an
  agency running one brand alone. It appears on the second profile, and stays
  for anybody who has already sent an invitation.

  Show all features — the price of hiding anything at all. Somebody who saw a
  feature on a colleague's screen has to be able to reach it in the product
  instead of in a support conversation.
"""
import json

import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_section, open_settings

pytestmark = pytest.mark.e2e


def _milestones(page, reached=None):
    page.route("**/api/settings/milestones", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"milestones": reached or {}})))


def _records(page):
    """Collect the milestone names the SPA writes back."""
    seen = []

    def _handler(route, request):
        seen.append(request.url.rsplit("/", 1)[-1])
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"milestones": {}}))

    page.route("**/api/settings/milestones/*", _handler)
    return seen


def _profiles(page, *names):
    page.route("**/api/accounts", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "accounts": [{"id": f"a{i}", "name": n, "is_primary": i == 0,
                          "has_logo": False} for i, n in enumerate(names)],
            "active_account_id": "a0"})))


def _posts(page, how_many):
    """Serve /api/posts with a given number of rows, whatever the query."""
    from datetime import datetime, timezone

    from models.schemas import PostSummary

    rows = [PostSummary(id=f"p{i}", topic=f"Post {i}", format="single",
                        status="draft", platform="instagram",
                        created_at=datetime.now(timezone.utc)).model_dump(mode="json")
            for i in range(how_many)]
    page.route("**/api/posts?*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(rows)))


# ── "would you like topics to find themselves?" ─────────────────────────────

def test_a_creator_with_four_posts_is_not_offered_sources(page, signed_in):
    """Four posts in, nobody is short of ideas yet. The offer is meant to land
    when keeping the queue full has started to be work."""
    _milestones(page)
    _posts(page, 4)
    signed_in()
    open_section(page, "queue")

    expect(page.locator("#sources-offer")).to_be_hidden()


def test_a_creator_with_five_posts_is_offered_sources(page, signed_in):
    _milestones(page)
    _posts(page, 5)
    signed_in()
    open_section(page, "queue")

    expect(page.locator("#sources-offer")).to_be_visible()


def test_the_offer_is_recorded_so_it_is_made_once(page, signed_in):
    _milestones(page)
    _posts(page, 5)
    seen = _records(page)
    signed_in()
    open_section(page, "queue")
    expect(page.locator("#sources-offer")).to_be_visible()

    assert seen == ["sources_offered"]


def test_somebody_already_offered_is_not_offered_again(page, signed_in):
    _milestones(page, {"sources_offered": "2026-08-09T00:00:00+00:00"})
    _posts(page, 20)
    signed_in()
    open_section(page, "queue")

    expect(page.locator("#sources-offer")).to_be_hidden()


def test_a_business_account_is_not_offered_what_it_already_has(page, signed_in):
    """Sources are the first screen of the Business product. Offering them
    there is the product advertising itself to itself."""
    _milestones(page)
    _posts(page, 20)
    page.route("**/api/business/drafts*", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]"))
    signed_in(account_type="business")
    open_section(page, "queue")

    expect(page.locator("#sources-offer")).to_be_hidden()


# ── Team ────────────────────────────────────────────────────────────────────

def test_an_agency_with_one_brand_has_no_team_tab(page, signed_in):
    """A screen about a second person, for somebody working alone on one brand.
    Nothing about it is actionable yet."""
    _milestones(page)
    _profiles(page, "Acme")
    signed_in(account_type="agency")
    open_settings(page, "profiles")

    expect(page.locator('#settings-tabs [data-settings-tab="team"]')).to_be_hidden()


def test_a_second_brand_brings_the_team_tab(page, signed_in):
    _milestones(page)
    _profiles(page, "Acme", "Beta")
    signed_in(account_type="agency")
    open_settings(page, "profiles")

    expect(page.locator('#settings-tabs [data-settings-tab="team"]')).to_be_visible()


def test_the_team_tab_stays_after_the_second_brand_goes(page, signed_in):
    """A feature that appeared and then vanished makes people doubt what they
    saw. The milestone is what keeps it, not the current profile count."""
    _milestones(page, {"team_unlocked": "2026-08-09T00:00:00+00:00"})
    _profiles(page, "Acme")
    signed_in(account_type="agency")
    open_settings(page, "profiles")

    expect(page.locator('#settings-tabs [data-settings-tab="team"]')).to_be_visible()


def test_a_creator_never_gets_the_team_tab(page, signed_in):
    """Team is an agency screen. The milestone gate is an extra lock on the
    account-type one, never a way round it."""
    _milestones(page, {"team_unlocked": "2026-08-09T00:00:00+00:00"})
    _profiles(page, "Acme", "Beta")
    signed_in()
    open_settings(page, "profiles")

    expect(page.locator('#settings-tabs [data-settings-tab="team"]')).to_be_hidden()


def test_a_creator_with_two_brands_records_no_team_milestone(page, signed_in):
    """Found by a phase 8.4 test, not by this file: the gate was recording on
    the profile count alone, so a creator keeping two brands was written down as
    having reached a screen that never appeared for them — and would have been
    handed it on the day they became an agency."""
    _milestones(page)
    _profiles(page, "Acme", "Beta")
    seen = _records(page)
    signed_in()
    open_settings(page, "profiles")
    expect(page.locator('#settings-tabs [data-settings-tab="team"]')).to_be_hidden()

    assert seen == []


# ── the escape hatch ────────────────────────────────────────────────────────

def test_show_all_features_reveals_what_was_gated(page, signed_in):
    _milestones(page)
    _profiles(page, "Acme")
    page.route("**/api/settings/milestones-all", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(
            {"milestones": {"team_unlocked": "2026-08-09T00:00:00+00:00",
                            "journal_unlocked": "2026-08-09T00:00:00+00:00"}})))
    signed_in(account_type="agency")
    open_settings(page, "profiles")
    expect(page.locator('#settings-tabs [data-settings-tab="team"]')).to_be_hidden()

    page.locator("#avatar-btn").click()
    page.locator("#show-all-features").click()

    expect(page.locator('#settings-tabs [data-settings-tab="team"]')).to_be_visible()


def test_show_all_features_closes_the_menu_it_was_clicked_in(page, signed_in):
    """Every other row in that menu does. One that does not leaves the menu
    sitting over the feature it just revealed."""
    _milestones(page)
    _profiles(page, "Acme")
    page.route("**/api/settings/milestones-all", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"milestones": {}})))
    signed_in(account_type="agency")

    page.locator("#avatar-btn").click()
    page.locator("#show-all-features").click()

    expect(page.locator("#avatar-menu")).to_be_hidden()
