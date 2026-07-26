"""Connection health + token-expiry rules.

The expiry side is deliberately hedged: Meta gives no read-only way to check an
Instagram token's remaining life, so unless the user pressed "Renew" the date is
an estimate counted from when they saved it. Losing the `expires_estimated` flag
would turn a guess into a claim.
"""
from datetime import datetime, timedelta, timezone

from services.connection_health import (
    EXPIRY_WARN_DAYS, IG_TOKEN_LIFETIME_DAYS, became_broken, days_left,
    estimate_expiry, is_expiring_soon, make_record,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def test_estimate_is_sixty_days_from_saving():
    assert estimate_expiry(NOW) == NOW + timedelta(days=IG_TOKEN_LIFETIME_DAYS)


def test_estimate_accepts_a_naive_timestamp():
    """SQLite hands back naive datetimes; treating them as UTC beats crashing."""
    assert estimate_expiry(NOW.replace(tzinfo=None)) == NOW + timedelta(days=60)


def test_days_left_is_none_when_unknown():
    assert days_left(None, NOW) is None
    assert is_expiring_soon(None, NOW) is False


def test_expiry_warning_window():
    assert is_expiring_soon(NOW + timedelta(days=EXPIRY_WARN_DAYS - 1), NOW) is True
    assert is_expiring_soon(NOW + timedelta(days=EXPIRY_WARN_DAYS + 5), NOW) is False


def test_an_expired_token_still_counts_as_expiring():
    """Past the date is the most urgent case — it must not fall out of the window."""
    assert is_expiring_soon(NOW - timedelta(days=3), NOW) is True
    assert days_left(NOW - timedelta(days=3), NOW) < 0


def test_record_marks_an_estimate_as_an_estimate():
    rec = make_record(ok=True, handle="acme", expires_at=NOW, estimated=True)
    assert rec["expires_estimated"] is True
    rec = make_record(ok=True, handle="acme", expires_at=NOW, estimated=False)
    assert rec["expires_estimated"] is False


def test_record_without_an_expiry_is_not_flagged_as_an_estimate():
    """X tokens (OAuth 1.0a) don't expire — "estimated" would be meaningless."""
    assert make_record(ok=True)["expires_estimated"] is False


def test_record_is_json_safe_and_truncates_a_huge_error():
    rec = make_record(ok=False, error="x" * 5000, checked_at=NOW)
    assert len(rec["error"]) <= 400
    assert rec["checked_at"] == NOW.isoformat()
    assert rec["expires_at"] is None


# ── when to email ────────────────────────────────────────────────────────────

def test_notify_on_the_transition_into_failure():
    assert became_broken({"ok": True}, {"ok": False}) is True


def test_do_not_notify_while_it_stays_broken():
    """Otherwise a dead token emails daily until the user filters us to spam —
    and then misses the next real one."""
    assert became_broken({"ok": False}, {"ok": False}) is False


def test_do_not_notify_on_success():
    assert became_broken({"ok": False}, {"ok": True}) is False
    assert became_broken({"ok": True}, {"ok": True}) is False


def test_a_first_check_that_fails_is_worth_an_email():
    assert became_broken(None, {"ok": False}) is True
    assert became_broken(None, {"ok": True}) is False
