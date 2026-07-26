"""Source fetchers — pulling recent items from a public link.

The Business module watches public sources (release notes, blog feeds, changelog
pages) for things worth posting about. A fetcher turns one URL into a list of
normalised FetchedItem records; the event selector then decides which are
newsworthy. Mirrors the Protocol + concrete + factory shape of services/video.

MVP set (gen-1, enough for the demo): github_releases, rss, generic_page. Other
kinds raise until there's real demand. Public links only — no OAuth, no tokens
(MVP discipline, doc §13).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol


class SourceFetchError(Exception):
    """A source couldn't be reached or parsed into items."""


class SourceRateLimited(SourceFetchError):
    """The source is fine, we're just over its request quota — try again later.

    A subclass so every existing `except SourceFetchError` keeps working; callers
    that care can tell "come back in an hour" apart from "this URL is dead", which
    otherwise both surface as an unreachable source the user is invited to delete.
    """


@dataclass
class FetchedItem:
    """One thing that happened at a source (a release, a post, a section)."""
    external_id: str                       # stable per-item key for dedup across polls
    kind: str                              # the source kind that produced it
    title: str
    url: str
    published_at: Optional[datetime]       # None when the source has no per-item date
    body: str = ""
    raw: dict = field(default_factory=dict)  # JSON-safe extras (kept minimal)


class SourceFetcher(Protocol):
    async def fetch(self, url: str, since: Optional[datetime] = None) -> list[FetchedItem]:
        """Return recent items from the source, filtered to on/after `since`."""
        ...


def get_source_fetcher(kind: str, *, ssl_verify: bool = True) -> SourceFetcher:
    """Build the fetcher for a source kind. Lazy imports keep the graph narrow —
    adding a source type never widens what the rest of the app imports."""
    kind = (kind or "").lower()
    if kind == "github_releases":
        from services.sources.github import GitHubReleasesFetcher
        return GitHubReleasesFetcher(ssl_verify=ssl_verify)
    if kind == "rss":
        from services.sources.feed import FeedFetcher
        return FeedFetcher(ssl_verify=ssl_verify)
    if kind == "generic_page":
        from services.sources.page import GenericPageFetcher
        return GenericPageFetcher(ssl_verify=ssl_verify)
    raise SourceFetchError(f"Unsupported source type: {kind!r}")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (accepting a trailing 'Z') to an aware datetime.

    Returns None on anything unparseable — a missing date must never crash a fetch;
    the item just carries published_at=None and skips the recency filter.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(html: str) -> str:
    """Flatten HTML to readable text — good enough to feed an LLM as context."""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html or "")).strip()
