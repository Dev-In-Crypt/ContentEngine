"""When a scheduled publish is worth trying again — and when it isn't.

Retrying a dead credential is worse than not retrying: it delays the bad news by
an hour and, on X, every attempt is billed. So the default is DON'T retry, and
only failures we can positively identify as transient earn another go.
"""
import httpx
import pytest

from services.publish_retry import (
    MAX_RETRIES, RETRY_DELAYS_MIN, is_retryable, next_delay_minutes,
)


def _raised_from(cause: Exception) -> Exception:
    """An error carrying `cause` in its chain, as the publishers raise them."""
    try:
        try:
            raise cause
        except Exception as e:
            raise RuntimeError(f"X tweet failed: {e}") from e
    except RuntimeError as outer:
        return outer


# ── transient: try again ─────────────────────────────────────────────────────

def test_network_error_is_retryable():
    assert is_retryable(_raised_from(httpx.ConnectError("connection reset"))) is True


def test_timeout_is_retryable():
    assert is_retryable(_raised_from(httpx.ReadTimeout("timed out"))) is True


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_status_codes_are_retryable(status):
    assert is_retryable(RuntimeError(f"X tweet failed: {status} upstream said no")) is True


# ── permanent: don't waste attempts ──────────────────────────────────────────

@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_status_codes_are_not_retryable(status):
    """A bad token or a rejected caption fails identically on every attempt."""
    assert is_retryable(RuntimeError(f"X tweet failed: {status} bad request")) is False


def test_an_unclassifiable_error_is_not_retried():
    """The safe default: an error we can't identify must not spin in a loop."""
    assert is_retryable(RuntimeError("something odd happened")) is False


def test_a_number_that_is_not_a_status_code_does_not_trigger_a_retry():
    """"500 followers" is not a server error. Mutation guard for a sloppy regex."""
    assert is_retryable(RuntimeError("Caption mentions 500 followers")) is False


# ── schedule ─────────────────────────────────────────────────────────────────

def test_delays_back_off():
    assert list(RETRY_DELAYS_MIN) == sorted(RETRY_DELAYS_MIN)
    assert RETRY_DELAYS_MIN[0] >= 1          # never hammer the API immediately
    assert MAX_RETRIES == len(RETRY_DELAYS_MIN)


def test_next_delay_follows_the_attempt_count():
    assert next_delay_minutes(0) == RETRY_DELAYS_MIN[0]
    assert next_delay_minutes(1) == RETRY_DELAYS_MIN[1]
    assert next_delay_minutes(MAX_RETRIES - 1) == RETRY_DELAYS_MIN[-1]


def test_no_delay_once_attempts_are_used_up():
    """None is the signal to stop and tell the user — not to retry forever."""
    assert next_delay_minutes(MAX_RETRIES) is None
    assert next_delay_minutes(MAX_RETRIES + 5) is None


def test_network_wording_survives_a_broken_exception_chain():
    """publisher_flow re-raises as PublishError; if a chain is ever lost, the
    message wording is the last thing keeping a transient failure retryable."""
    assert is_retryable(RuntimeError("X network error: connection reset")) is True
    assert is_retryable(RuntimeError("Network error creating media container")) is True


def test_a_permanent_failure_is_not_rescued_by_the_word_error():
    """The status wins over the wording — "401" must stay final even though the
    message also contains "error"."""
    assert is_retryable(RuntimeError("X tweet failed: 401 auth error")) is False
