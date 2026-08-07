"""The Team screen, in a real browser.

Small feature, sharp edge. This is the first thing in the product to depend on
account_type == 'agency' being a value the SPA actually emits, and the gating
CSS is built so that creator is the DEFAULT rather than a positive match. There
is one rule hiding .creator-only, and it keys off "business". The symmetric
edit — adding the same rule for agency — blanks the whole application for an
entire class of account, silently, with every test still green except one.

That one is test_an_agency_account_gets_the_creator_shell in the Business file,
written in 3.0 for this exact day. What lives here is the rest: that the tab
appears for an agency and for nobody else, that the screen says out loud it
grants no access, and that the invite form behaves.

The API is stubbed. What the routes do with a row is settled in
tests/test_team_api.py against a real database; repeating it through a browser
would test the same guard twice and the rendering not at all.
"""
import json

import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_settings

pytestmark = pytest.mark.e2e


def _invitation(**over):
    fields = dict(id="i1", email="colleague@example.com", status="pending",
                  created_at="2026-08-01T00:00:00+00:00", accepted_at=None)
    fields.update(over)
    return fields


def _serve(page, rows):
    page.route("**/api/team/invitations", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(rows)))


# ── who sees it ─────────────────────────────────────────────────────────────

def test_an_agency_gets_the_team_tab(page, signed_in):
    signed_in(account_type="agency")
    _serve(page, [])
    open_settings(page, "profiles")
    expect(page.locator('#settings-tabs [data-settings-tab="team"]')).to_be_visible()


@pytest.mark.parametrize("account_type", ["creator", "business"])
def test_nobody_else_gets_the_team_tab(page, signed_in, account_type):
    """Opened on another tab first, deliberately: the strip lives inside the
    Settings screen, so asserting from the app shell would pass for everyone
    including an agency and prove nothing."""
    signed_in(account_type=account_type)
    open_settings(page, "profiles")
    expect(page.locator("#settings-tabs")).to_be_visible()
    expect(page.locator('#settings-tabs [data-settings-tab="team"]')).to_be_hidden()


def test_the_agency_shell_is_still_the_creator_shell(page, signed_in):
    """The risk stated as a test, from the other side. Emitting a third
    account-type value must not cost an agency the application: they keep the
    Create wizard and the Calendar, and gain only the Team tab."""
    signed_in(account_type="agency")
    expect(page.locator('[data-section="create"]')).to_be_visible()
    expect(page.locator('[data-section="calendar"]')).to_be_visible()
    expect(page.locator("#create-post-panel")).to_be_visible()


# ── the screen ──────────────────────────────────────────────────────────────

def test_the_screen_says_an_invitation_grants_nothing(page, signed_in):
    """Not decoration. The feature ships before the access it implies, so the
    one thing the screen must not do is imply the access. Delete this sentence
    and the first person who accepts files a bug we would deserve."""
    signed_in(account_type="agency")
    _serve(page, [])
    open_settings(page, "team")
    expect(page.locator('[data-settings-tab="team"] p').first).to_contain_text(
        "does not give them access")


def test_an_empty_team_says_so(page, signed_in):
    signed_in(account_type="agency")
    _serve(page, [])
    open_settings(page, "team")
    expect(page.locator("#team-list")).to_contain_text("Nobody invited yet")


def test_an_invitation_is_listed_with_its_status(page, signed_in):
    signed_in(account_type="agency")
    _serve(page, [_invitation(), _invitation(id="i2", email="joined@example.com",
                                             status="accepted")])
    open_settings(page, "team")
    expect(page.locator("#team-list")).to_contain_text("colleague@example.com")
    expect(page.locator("#team-list")).to_contain_text("joined@example.com")
    # Only a pending invitation can be revoked; the others have nothing to undo.
    expect(page.locator('#team-list [data-act="revoke"]')).to_have_count(1)


def test_an_address_without_an_at_never_reaches_the_server(page, signed_in):
    """The server checks this too. Sending it anyway costs a round-trip to be
    told what the field already knew, and the answer belongs beside the field."""
    signed_in(account_type="agency")
    _serve(page, [])
    posts = []
    page.on("request", lambda r: posts.append(r.url)
            if r.method == "POST" and "team/invitations" in r.url else None)

    open_settings(page, "team")
    page.locator("#team-email").fill("not-an-address")
    page.locator("#team-invite-btn").click()

    expect(page.locator("#team-status")).to_contain_text("valid email")
    assert posts == []


def test_a_sent_invitation_clears_the_field_and_reloads_the_list(page, signed_in):
    signed_in(account_type="agency")
    state = {"rows": []}

    def _list(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(state["rows"]))

    def _create(route):
        state["rows"] = [_invitation(email="fresh@example.com")]
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(state["rows"][0]))

    page.route("**/api/team/invitations",
               lambda route: _create(route) if route.request.method == "POST"
               else _list(route))

    open_settings(page, "team")
    page.locator("#team-email").fill("fresh@example.com")
    page.locator("#team-invite-btn").click()

    expect(page.locator("#team-status")).to_contain_text("Invitation sent")
    expect(page.locator("#team-email")).to_have_value("")
    expect(page.locator("#team-list")).to_contain_text("fresh@example.com")


def test_a_refused_invitation_shows_the_servers_reason(page, signed_in):
    signed_in(account_type="agency")
    _serve(page, [])
    page.route("**/api/team/invitations",
               lambda route: route.fulfill(
                   status=409, content_type="application/json",
                   body=json.dumps({"detail": "That address already has a pending invitation."}))
               if route.request.method == "POST"
               else route.fulfill(status=200, content_type="application/json", body="[]"))

    open_settings(page, "team")
    page.locator("#team-email").fill("dup@example.com")
    page.locator("#team-invite-btn").click()
    expect(page.locator("#team-status")).to_contain_text("already has a pending invitation")
