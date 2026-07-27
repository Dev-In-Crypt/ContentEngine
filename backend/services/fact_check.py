"""Opt-in fact-checking for creator posts.

The Business module checks a draft against the company page it was written from.
A creator post rarely has that: it was generated from a topic, and any "sources"
on it are the citations a web-grounded model chose to report — URLs and titles,
never the text behind them. So this module's job is to produce something real to
check against: fetch the cited pages, and let the author paste their own source.

The rule that keeps this honest is negative. With no usable source we return
`no_source` and never call the model. A model asked to grade its own output from
memory will happily confirm everything it just invented, and a green tick that
means nothing is worse than no tick at all — the author would trust it.

Verification itself is `services/claim_check.verify_claims`, unchanged and
unsoftened: a claim counts as confirmed only when its evidence literally appears
in the source we fetched.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from services.claim_check import verify_claims
from services.sources.base import get_source_fetcher
from services.sources.detect import detect_source_type

log = logging.getLogger(__name__)

# A post cites a handful of pages at most, and each fetch costs a request and
# prompt tokens. Three is enough to check a caption against.
MAX_SOURCE_URLS = 3
MAX_SOURCE_CHARS = 20000
# Below this there is nothing a check could bind evidence to; "see our site" is
# not a source. Deliberately low: a one-line changelog entry ("Pricing update — we
# cut prices by 20% in June.") is a perfectly good thing to check a caption
# against, and a threshold that rejected it would make the feature useless on
# exactly the sources it handles best.
MIN_SOURCE_CHARS = 40


def _is_fetchable(url: str) -> bool:
    """http(s) only. These URLs are model output, so file:// and friends must not
    reach a fetcher just because a citation said so."""
    try:
        return urlparse(url or "").scheme in ("http", "https")
    except ValueError:
        return False


def has_material(text: str) -> bool:
    return len((text or "").strip()) >= MIN_SOURCE_CHARS


async def gather_source_text(
    urls, *, pasted: str = "", ssl_verify: bool = True,
) -> tuple[str, list[dict]]:
    """Build the text to check against. Returns (text, [{url, ok, error}]).

    A fetch failure is reported rather than swallowed: checking against fewer
    sources than the author thinks would mark perfectly good claims unconfirmed,
    and they would have no way to know why.
    """
    parts: list[str] = []
    if (pasted or "").strip():
        parts.append(pasted.strip())

    used: list[dict] = []
    seen: set[str] = set()
    for url in urls or []:
        if len(used) >= MAX_SOURCE_URLS:
            break
        if not _is_fetchable(url) or url in seen:
            continue
        seen.add(url)
        try:
            fetcher = get_source_fetcher(detect_source_type(url), ssl_verify=ssl_verify)
            items = await fetcher.fetch(url)
            text = "\n\n".join(
                f"{i.title}\n{i.body}".strip() for i in items if (i.title or i.body))
            if text.strip():
                parts.append(text)
            used.append({"url": url, "ok": True, "error": ""})
        except Exception as e:
            log.info("Fact-check could not read %s: %s", url, e)
            used.append({"url": url, "ok": False, "error": str(e)[:200]})

    return "\n\n---\n\n".join(parts)[:MAX_SOURCE_CHARS], used


async def verify_post(
    text_provider, *, draft_text: str, source_urls, pasted: str = "",
    text_model: str = "", ssl_verify: bool = True,
) -> dict:
    """Check one creator post. Never raises — the outcome is the return value.

    status: "checked" | "no_source" | "error".
    """
    source_text, used = await gather_source_text(
        source_urls, pasted=pasted, ssl_verify=ssl_verify)

    stamp = datetime.now(timezone.utc).isoformat()
    if not has_material(source_text):
        return {"status": "no_source", "claims": [], "sources_used": used,
                "checked_at": stamp}

    try:
        claims = await verify_claims(
            text_provider, draft_text=draft_text, source_text=source_text,
            text_model=text_model)
    except Exception as e:
        log.warning("Creator fact-check failed: %s", e)
        return {"status": "error", "claims": [], "sources_used": used,
                "checked_at": stamp, "error": str(e)[:200]}

    return {"status": "checked", "claims": claims, "sources_used": used,
            "checked_at": stamp}
