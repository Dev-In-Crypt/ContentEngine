"""Generic page fetcher — a changelog or pricing page with no feed.

Many companies publish "what's new" as a plain HTML page. There's no per-item
date or id, so this splits the page on its headings (h1–h3) and treats each
section as an item, keyed by a hash of its heading + url.

The hard part isn't splitting, it's telling an update from the furniture around
it. Taking every heading in the document made nav labels into news ("The company
is sharing a changelog to communicate updates", drafted from a menu item), and a
nav entry titled "Pricing" even scores *worthy* downstream. So we filter here,
structurally: chrome elements are removed, the main content region wins when the
page marks one, and the residue is judged on what a heading *is* (a bare link,
the page's own title, a repeat) — never on how short its text is. Terse real
entries like "Bug fixes" must survive.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from services.http_utils import describe_request_error
from services.sources.base import FetchedItem, SourceFetchError

_MAX_ITEMS = 30
_MAX_BODY = 600
_HEADINGS = ("h1", "h2", "h3")

#: Elements that are never content, wherever they appear.
_CHROME_TAGS = ("nav", "aside", "script", "style", "noscript", "form", "svg")
_CHROME_ROLES = ("navigation", "banner", "contentinfo", "search")
#: <header>/<footer> are chrome at page level but legitimate inside an entry —
#: <article><header><h2>Real title</h2></header> is standard blog-card markup.
_NESTED_CONTENT = ("article", "main", "section")


def _strip_chrome(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(_CHROME_TAGS):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": lambda v: v in _CHROME_ROLES}):
        tag.decompose()
    for tag in soup.find_all(("header", "footer")):
        if not tag.find_parent(_NESTED_CONTENT):
            tag.decompose()


def _content_root(soup: BeautifulSoup):
    """The region holding the updates: <main> when the page marks one AND it
    actually contains headings, else the whole document. The fallback matters —
    plenty of real changelogs are an unmarked <div> soup, and losing those would
    be a worse failure than the furniture we're removing."""
    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if main is not None and main.find(_HEADINGS):
        return main
    return soup.body or soup


def _is_page_title(heading, title: str, page_url: str, first: bool) -> bool:
    """True for the page announcing its own name — <h1>Changelog</h1> on
    /changelog, or on /changelog/windows. Narrow on purpose: only the first
    heading, only an h1, and only when it echoes a segment of the URL path."""
    if not first or heading.name != "h1":
        return False
    slug = title.lower().replace(" ", "-")
    return slug in {p.lower() for p in urlparse(page_url).path.split("/") if p}


def _is_nav_link(heading, body: str) -> bool:
    """A heading that is nothing but a link, with no section under it, points
    somewhere else — that is navigation, not an event."""
    return not body and heading.find("a") is not None


#: Phrases that only ever head a list of other posts. Structure can't catch these:
#: "All changelog posts" on unkey.com carries the whole post list as its body and
#: links nothing itself. Matched whole, so a real entry titled "Archive rebuilt in
#: Rust" is untouched.
_LINK_LIST_LABELS = frozenset({
    "all changelog posts", "all posts", "all updates", "view all", "view all posts",
    "see all posts", "more posts", "recent posts", "latest posts", "archive",
    "read more", "browse posts",
})


class GenericPageFetcher:
    def __init__(self, ssl_verify: bool = True) -> None:
        self._ssl_verify = ssl_verify

    async def fetch(self, url: str, since: Optional[datetime] = None) -> list[FetchedItem]:
        try:
            async with httpx.AsyncClient(
                timeout=20.0, verify=self._ssl_verify, follow_redirects=True,
                headers={"User-Agent": "ContentEngine"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text
        except httpx.HTTPStatusError as e:
            raise SourceFetchError(f"Page returned {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise SourceFetchError(describe_request_error(e, "Page")) from e

        soup = BeautifulSoup(html, "html.parser")
        _strip_chrome(soup)

        items: list[FetchedItem] = []
        seen: set[str] = set()
        for n, heading in enumerate(_content_root(soup).find_all(_HEADINGS)):
            title = " ".join(heading.get_text().split())
            if len(title) < 4:                       # skip empty/decorative headings
                continue
            key = title.lower()
            if key in seen:                          # a mirrored list would double every lead
                continue
            body = _section_text(heading)
            # An empty body means the next heading follows immediately: this one
            # groups them ("New features", "Updates"), it isn't an entry. EMPTY,
            # not short — a terse real entry like "Bug fixes" still has text.
            if (not body
                    or key in _LINK_LIST_LABELS
                    or _is_nav_link(heading, body)
                    or _is_page_title(heading, title, url, n == 0)):
                continue
            seen.add(key)
            anchor = heading.get("id")
            item_url = f"{url}#{anchor}" if anchor else url
            items.append(FetchedItem(
                external_id=hashlib.sha1(f"{item_url}:{title}".encode()).hexdigest()[:16],
                kind="generic_page",
                title=title,
                url=item_url,
                published_at=None,                   # generic pages have no per-item date
                body=body,
                raw={},
            ))
            if len(items) >= _MAX_ITEMS:
                break
        return items


def _siblings_text(node) -> str:
    """Text of the siblings after `node`, up to the next heading of any level."""
    parts: list[str] = []
    for sib in node.find_next_siblings():
        if getattr(sib, "name", None) in _HEADINGS:
            break
        text = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else ""
        if text:
            parts.append(text)
        if sum(len(p) for p in parts) >= _MAX_BODY:
            break
    return " ".join(parts)[:_MAX_BODY].strip()


def _section_text(heading) -> str:
    """The entry's text.

    Normally that's the siblings after the heading. But blog-card markup wraps the
    title — <article><header><h2>…</h2></header><p>body</p></article> — so the
    heading has no siblings at all and the body sits one level up. Climbing on an
    empty result is what keeps those real entries from looking like empty section
    labels, which we drop.
    """
    text = _siblings_text(heading)
    node = heading
    while not text:
        node = node.parent
        if node is None or getattr(node, "name", None) in (_NESTED_CONTENT + ("body", "[document]")):
            break
        text = _siblings_text(node)
    return text
