"""The Queue as a week, and the two cards that belong beside it.

The Queue was a flat list ordered by nothing a person thinks in. What they
think in is the week: what goes out Monday, what Thursday is still empty. The
mockups draw it that way and put the content-mix rail next to it, which is the
right neighbour — "you are thin on community" is only useful while looking at
the week it is thin in.

Both the mix and the weekly plan already existed, on the Calendar. This moves
them; it does not build them. The Calendar keeps the month, because a month is
the only place a schedule further out than seven days is visible at all.

What the mockup draws and this does not: "＋ Slot" on an empty day. Nothing in
the product can create an empty scheduled slot, and a button that cannot is
worse than a day that simply has nothing in it.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from tests.e2e.nav import open_calendar, open_section

pytestmark = pytest.mark.e2e


def _monday(now=None):
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)


def _post(**over):
    fields = dict(id="p1", topic="Blueberry notes", platform="instagram",
                  status="scheduled", format="single", variant_group_id="g1",
                  created_at="2026-08-10T09:00:00+00:00", scheduled_at=None)
    fields.update(over)
    return fields


def _serve_posts(page, rows):
    page.route("**/api/posts?*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(rows)))


def test_a_scheduled_post_lands_on_its_day(page, signed_in):
    wed = _monday() + timedelta(days=2, hours=9)
    _serve_posts(page, [_post(scheduled_at=wed.isoformat())])
    signed_in()
    open_section(page, "queue")

    day = page.locator('#queue-week [data-weekday="2"]')
    expect(day).to_contain_text("Blueberry notes")


def test_an_unscheduled_draft_is_not_lost(page, signed_in):
    """Only scheduled posts have a day, and drafts are most of what the Queue
    holds. The split is by IDEA rather than by post: a group with one sibling
    still undated is half scheduled, which is the state most worth seeing, so
    the whole group waits below instead of appearing in a day it is only partly
    ready for."""
    _serve_posts(page, [_post(id="d1", topic="Cold brew ratios", status="draft")])
    signed_in()
    open_section(page, "queue")

    expect(page.locator("#queue-list")).to_contain_text("Cold brew ratios")


def test_the_content_mix_sits_beside_the_week(page, signed_in):
    """It was on the Calendar, where "you are thin on community" was advice
    about a month you were not looking at."""
    _serve_posts(page, [])
    signed_in()
    open_section(page, "queue")

    expect(page.locator("#pillar-bars")).to_be_visible()
    expect(page.locator("#plan-card")).to_be_visible()


def test_the_calendar_keeps_the_month(page, signed_in):
    """Moving the two cards must not take the Calendar's own job with them."""
    _serve_posts(page, [])
    signed_in()
    open_calendar(page)

    expect(page.locator("#cal-grid")).to_be_visible()
    expect(page.locator("#pillar-bars")).to_be_hidden()
