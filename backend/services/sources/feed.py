"""RSS/Atom feed fetcher — a company blog or news feed.

Fetched with httpx (so we control TLS/verify and timeouts), then parsed by
feedparser, which tolerates the many ways feeds are malformed in the wild.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx

from services.http_utils import describe_request_error
from services.sources.base import FetchedItem, SourceFetchError, strip_html
from services.url_guard import BlockedURL, guarded_get

#: Feeds carry full post bodies, so they run larger than a changelog page.
_MAX_BYTES = 5 * 1024 * 1024


class FeedFetcher:
    def __init__(self, ssl_verify: bool = True) -> None:
        self._ssl_verify = ssl_verify

    async def fetch(self, url: str, since: Optional[datetime] = None) -> list[FetchedItem]:
        try:
            resp = await guarded_get(
                url, ssl_verify=self._ssl_verify, timeout=20.0,
                headers={"User-Agent": "ContentEngine"}, max_bytes=_MAX_BYTES,
            )
            resp.raise_for_status()
            content = resp.content
        except BlockedURL as e:
            raise SourceFetchError(str(e)) from e
        except httpx.HTTPStatusError as e:
            raise SourceFetchError(f"Feed returned {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise SourceFetchError(describe_request_error(e, "Feed")) from e

        parsed = feedparser.parse(content)
        items: list[FetchedItem] = []
        for entry in getattr(parsed, "entries", []):
            published = _entry_datetime(entry)
            if since and published and published < since:
                continue
            title = (entry.get("title") or "").strip()
            link = entry.get("link") or url
            if not title:
                continue
            items.append(FetchedItem(
                external_id=str(entry.get("id") or link or title),
                kind="rss",
                title=title,
                url=link,
                published_at=published,
                body=strip_html(entry.get("summary") or ""),
                raw={},
            ))
        return items


def _entry_datetime(entry) -> Optional[datetime]:
    """feedparser exposes a parsed time.struct_time (UTC) on published/updated."""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime(*st[:6], tzinfo=timezone.utc)
    return None
