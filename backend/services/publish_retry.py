"""Whether a failed scheduled publish is worth attempting again, and when.

Retrying is not free: on X every attempt is billed, and a dead token fails the
same way at 12:05 as it did at 12:00 — all a retry buys there is an hour's delay
before the user learns anything. So this is deliberately asymmetric:

  * a failure we can positively identify as transient (the network dropped, the
    platform answered 5xx, we were rate limited) gets another go;
  * everything else — including anything we can't classify — is final.

Defaulting to "final" is the safe direction. The cost of not retrying a transient
error is one failed post the user can re-publish with a click; the cost of
retrying a permanent one is a loop that burns quota and hides the real problem.

Publishers raise plain PublisherError/InstagramError with the status embedded in
the message, so classification reads the exception chain first (exact) and falls
back to the message (approximate). Adding a flag at each of the ~15 raise sites
was the alternative — more code, and one missed site fails silently.
"""
from __future__ import annotations

import re

import httpx

#: Minutes to wait before each retry. Covers a brief network blip and a
#: platform's hourly rate-limit window without pushing the post so far past its
#: slot that it's no longer timely.
RETRY_DELAYS_MIN: tuple[int, ...] = (5, 15, 60)
MAX_RETRIES = len(RETRY_DELAYS_MIN)

#: Statuses that mean "this might work later".
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
#: A status code as the publishers format it: "X tweet failed: 503 …", "GitHub
#: returned 403 …". The code must follow a colon or a status word — a bare number
#: is not a status, and reading "500 followers" in a caption as a server error
#: would retry a post that was rejected on its content.
_STATUS_RE = re.compile(
    r"(?::\s*|\b(?:status|code|failed|returned|error|http)\b\W{0,4})([1-5]\d\d)\b",
    re.IGNORECASE,
)
#: How the publishers word a transport failure when no status exists at all.
_NETWORK_RE = re.compile(r"\b(?:network error|timed out|timeout|connection\s+\w+)\b", re.IGNORECASE)


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def is_retryable(exc: BaseException) -> bool:
    """True only for failures that plausibly succeed on a later attempt."""
    for err in _chain(exc):
        # Exact: the request never got an answer (connect/read/timeout/etc).
        if isinstance(err, httpx.RequestError):
            return True
        if isinstance(err, httpx.HTTPStatusError):
            return err.response.status_code in _RETRYABLE_STATUS
    # Approximate: the publishers put the status in the message text.
    text = str(exc)
    for match in _STATUS_RE.finditer(text):
        if int(match.group(1)) in _RETRYABLE_STATUS:
            return True
        return False        # a status was stated and it isn't retryable
    # Last resort, for a chain broken somewhere in the call stack: the publishers
    # word these consistently ("X network error: …", "Network error creating …").
    return bool(_NETWORK_RE.search(text))


def next_delay_minutes(attempts_made: int) -> int | None:
    """Minutes until the next attempt, or None when they're used up."""
    if attempts_made < 0 or attempts_made >= MAX_RETRIES:
        return None
    return RETRY_DELAYS_MIN[attempts_made]
