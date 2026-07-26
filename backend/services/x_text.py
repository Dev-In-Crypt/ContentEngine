"""Keeping X posts inside the character budget without mangling them.

A tweet has a hard limit, and models routinely overshoot it. Cutting at the limit
mid-word (`text[:250]`) is what we used to do — it produces "…for filter cof" and
can slice a hashtag in half. Instead:

  1. ask the model to shorten while preserving the meaning, then
  2. if it still overshoots, cut on a word boundary and add an ellipsis.

Step 2 alone is the safety net; step 1 is what keeps the text readable. Pure
functions here so the rules are testable without touching a provider.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Optional

from models.schemas import TWEET_CHAR_LIMIT

#: Signature of the "make this shorter" helper (CaptionGenerator.shorten_text).
Shortener = Callable[[str, int], Awaitable[str]]

_ELLIPSIS = "…"

#: X wraps every link in a t.co of exactly this length, however long the original.
URL_WEIGHT = 23
_URL_RE = re.compile(r"https?://\S+")
#: End of a sentence: terminator followed by whitespace or the end of the text.
_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")
#: A sentence cut must keep at least this share of the budget, or "Hi. " + 240
#: characters would collapse to "Hi." — a finished sentence and a deleted post.
_MIN_SENTENCE_KEEP = 0.6


def tweet_length(text: str) -> int:
    """Length as X counts it: every URL costs 23 characters, not its real length.

    Counting the raw string made a post carrying a long source link look ~70
    characters over budget, so it was trimmed when X would have accepted it whole.
    """
    return len(_URL_RE.sub("#" * URL_WEIGHT, text or ""))


def fit_tweet(text: str, limit: int = TWEET_CHAR_LIMIT) -> str:
    """Return `text` guaranteed to fit `limit`, reading as a finished thought.

    Three tiers, best first: end on a complete sentence (returned WITHOUT an
    ellipsis — it isn't truncated, it's finished); else cut back to the last whole
    word and mark it with an ellipsis; else, for a single word longer than the
    limit, cut inside the token because no boundary exists. Length is measured the
    way X measures it, so a link costs 23 rather than its real size.
    """
    text = (text or "").strip()
    if tweet_length(text) <= limit:
        return text

    for match in reversed(list(_SENTENCE_END.finditer(text))):
        candidate = text[: match.end()].rstrip()
        if tweet_length(candidate) <= limit:
            return candidate if len(candidate) >= limit * _MIN_SENTENCE_KEEP else _fit_word(
                text, limit)
    return _fit_word(text, limit)


def _fit_word(text: str, limit: int) -> str:
    """Cut back to the last whole word that fits, and mark it as cut short."""
    words = text.split(" ")
    while len(words) > 1:
        words.pop()
        candidate = " ".join(words).rstrip(" ,.;:—-")
        if candidate and tweet_length(candidate + _ELLIPSIS) <= limit:
            return candidate + _ELLIPSIS
    return text[: limit - len(_ELLIPSIS)] + _ELLIPSIS   # one unbroken giant token


def append_tags(text: str, tags: str, limit: Optional[int] = TWEET_CHAR_LIMIT) -> str:
    """Attach the hashtags to a tweet, shortening the BODY if they don't fit.

    The hashtags are the one part that must survive intact — a cut that lands
    inside "#FitnessOver40" publishes a different tag. So when the pair overflows,
    the text gives way, not the tags. `limit=None` is the X Premium long post,
    where no cap applies.
    """
    text = (text or "").strip()
    tags = (tags or "").strip()
    if not tags:
        return text
    if not text:
        return tags
    if limit is None or tweet_length(text) + 2 + tweet_length(tags) <= limit:
        return f"{text}\n\n{tags}"
    return f"{fit_tweet(text, limit - tweet_length(tags) - 2)}\n\n{tags}"


def clamp_count(parts: list[str], lo: int, hi: int) -> list[str]:
    """Bound how many tweets a thread has.

    Trims a too-long thread to `hi`. Deliberately does NOT pad a short one up to
    `lo`: inventing filler tweets to hit a number is exactly what breaks the
    "each tweet continues the previous, reads as one piece" requirement. A model
    that answers a narrow topic in fewer tweets is right, not wrong.
    """
    cleaned = [p.strip() for p in parts if p and p.strip()]
    return cleaned[:hi] if hi and len(cleaned) > hi else cleaned


async def enforce_parts(
    parts: list[str],
    shorten: Optional[Shortener] = None,
    limit: int = TWEET_CHAR_LIMIT,
) -> list[str]:
    """Bring every part inside `limit`, preferring a model rewrite over a cut.

    Parts already within budget are left exactly as they are, so a well-behaved
    model costs nothing extra.
    """
    out: list[str] = []
    for part in parts:
        part = (part or "").strip()
        if tweet_length(part) <= limit:
            out.append(part)
            continue
        if shorten is not None:
            try:
                rewritten = (await shorten(part, limit) or "").strip()
                if rewritten:
                    part = rewritten
            except Exception:
                # A failed rewrite must not fail the whole post — fall through to
                # the deterministic cut below.
                pass
        out.append(fit_tweet(part, limit))
    return out


def looks_truncated(text: str) -> bool:
    """True if a tweet appears to end mid-thought — used by tests and as a signal
    that the prompt (not the cutter) needs work."""
    stripped = (text or "").rstrip()
    return stripped.endswith(_ELLIPSIS) or stripped.endswith("...")
