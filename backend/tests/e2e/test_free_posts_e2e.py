"""The wall moves: an account with free posts left can generate without a key.

Until UX phase 6.4 `guardGenerateKeys` refused every cloud account that had not
pasted an API key, which made the product's answer to "show me what you do" a
form. The server has been handing out a small allowance since 6.2; this is the
half that lets a person reach it.

Three states, and the third is the one worth naming: `free: null` means the
subject does not apply — the desktop owner, an account paying with its own key,
a deployment with no application key — and is NOT the same as `remaining: 0`.
Collapsing the two would show "0 free posts left" to somebody who never had any,
and hand the old refusal to somebody who has three.

The e2e server holds no keys at all, so `/api/usage` is stubbed: that endpoint
carries the allowance, and what the browser does with each answer is the whole
subject here.
"""
import json

import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_configure  # noqa: F401  (kept for parity with the composer file)

pytestmark = pytest.mark.e2e


def _usage(free) -> dict:
    """A /api/usage body, with whatever the allowance block should say."""
    return {"today": {"cost": 0.0, "tokens": 0, "calls": 0},
            "month": {"cost": 0.0, "tokens": 0, "calls": 0},
            "by_model": [], "free": free}


def _serve_usage(page, free) -> None:
    page.route("**/api/usage", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(_usage(free))))


def _compose(page, topic: str = "Sourdough starters") -> None:
    page.locator("#topic").fill(topic)
    page.get_by_role("button", name="Next →").click()
    expect(page.locator("#step-2")).to_be_visible()


# ── the counter ─────────────────────────────────────────────────────────────

def test_the_free_posts_left_are_shown_next_to_the_button(page, signed_in):
    _serve_usage(page, {"remaining": 3, "limit": 5})
    signed_in()
    _compose(page)

    expect(page.locator("#free-left")).to_be_visible()
    expect(page.locator("#free-left")).to_contain_text("3 of 5 free posts left")


def test_an_account_with_its_own_key_is_told_nothing_about_free_posts(page, signed_in):
    """`free` is null for them, and a count of somebody else's allowance next to
    their button would be noise at best and wrong at worst."""
    _serve_usage(page, None)
    signed_in()
    _compose(page)

    expect(page.locator("#free-left")).to_be_hidden()


def test_running_out_says_so_where_the_count_used_to_be(page, signed_in):
    _serve_usage(page, {"remaining": 0, "limit": 5})
    signed_in()
    _compose(page)

    expect(page.locator("#free-left")).to_be_visible()
    expect(page.locator("#free-left")).to_contain_text("Free posts used up")


# ── the wall ────────────────────────────────────────────────────────────────

def test_an_account_with_free_posts_generates_without_a_key(page, signed_in):
    """The point of the phase. The account has pasted nothing, and the click
    reaches the server instead of a modal asking for a key."""
    _serve_usage(page, {"remaining": 3, "limit": 5})
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "posts/generate" in r.url else None)
    page.route("**/api/posts/generate", lambda r: r.fulfill(
        status=200, content_type="text/event-stream",
        body='data: {"type": "error", "message": "stub"}\n\n'))

    signed_in()
    _compose(page)
    page.locator("#generate-btn").click()

    expect(page.locator("#need-key-modal")).to_be_hidden()
    expect(page.locator("#step-3")).to_be_visible()
    assert calls, "the generation never reached the server"


def test_the_wall_arrives_when_the_allowance_is_gone(page, signed_in):
    """Not at the door — here, where the sentence is finally worth reading."""
    _serve_usage(page, {"remaining": 0, "limit": 5})
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "posts/generate" in r.url else None)

    signed_in()
    _compose(page)
    page.locator("#generate-btn").click()

    expect(page.locator("#need-key-modal")).to_be_visible()
    expect(page.locator("#need-key-title")).to_contain_text("free posts are used up")
    assert calls == []


def test_a_deployment_with_nothing_free_still_asks_for_a_key_the_old_way(page, signed_in):
    """`free: null` with no key of their own is the self-hosted case, and the
    refusal that has always been correct there must not be replaced by one that
    talks about an allowance nobody offered."""
    _serve_usage(page, None)
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "posts/generate" in r.url else None)

    signed_in()
    _compose(page)
    page.locator("#generate-btn").click()

    expect(page.locator("#need-key-modal")).to_be_visible()
    expect(page.locator("#need-key-title")).to_contain_text("Set up your AI model")
    assert calls == []


def test_the_count_is_read_fresh_at_the_click(page, signed_in):
    """The header polls on a timer, so between two ticks the last post can be
    spent in another tab. Being let through on a stale count means a 409 from
    the server instead of the modal that says what to do about it.

    The stub starts generous and turns empty before the click, which is exactly
    that window.
    """
    _serve_usage(page, {"remaining": 1, "limit": 5})
    signed_in()
    _compose(page)
    expect(page.locator("#free-left")).to_contain_text("1 of 5")

    _serve_usage(page, {"remaining": 0, "limit": 5})     # spent elsewhere
    page.locator("#generate-btn").click()

    expect(page.locator("#need-key-modal")).to_be_visible()
    expect(page.locator("#need-key-title")).to_contain_text("free posts are used up")
