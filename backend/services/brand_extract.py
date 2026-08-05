"""Read a website and work out what the brand looks like.

The UX rework asks for one field instead of eight: paste a link, and the
product fills in the name, the description, the colours and the logo for you.
This is the reading half. It is deliberately free of both the database and the
LLM — it takes a URL and returns what the page says about itself, so it can be
tested against a handful of HTML strings and reused by anything.

Everything here is inference from markup that sites are under no obligation to
get right, so every field is Optional and the caller is expected to show the
result for editing rather than save it silently.

The fetch goes through services/url_guard.py: this URL is typed by whoever is
looking at the screen, including, on the new landing page, someone who has not
signed up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from services.http_utils import describe_request_error
from services.url_guard import BlockedURL, guarded_get

_MAX_BYTES = 2 * 1024 * 1024
_MAX_DESCRIPTION = 500
_MAX_NAME = 120                    # matches ProfileUpdate.brand_name

#: Splits "Acme | Industrial Anvils" into its halves. The dash forms require
#: surrounding whitespace, without which every Coca-Cola loses its second half.
_TITLE_SPLIT_RE = re.compile(r"\s*[|·•]\s*|\s+[–—-]\s+|\s*::\s*|:\s+")

#: Title halves that name a page rather than a company.
_GENERIC_TITLE_PARTS = frozenset({
    "home", "homepage", "welcome", "index", "start", "main",
})

_ICON_RELS = frozenset({
    "icon", "shortcut", "apple-touch-icon", "apple-touch-icon-precomposed",
})
_APPLE_RELS = frozenset({"apple-touch-icon", "apple-touch-icon-precomposed"})

_HEX3_RE = re.compile(r"#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])\Z")
_HEX6_RE = re.compile(r"#[0-9a-fA-F]{6}\Z")
_RGB_RE = re.compile(
    r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", re.IGNORECASE)
_SIZE_RE = re.compile(r"(\d+)x(\d+)", re.IGNORECASE)


class BrandExtractError(Exception):
    """The site couldn't be read. Safe to show to whoever pasted the link."""


@dataclass
class BrandInfo:
    #: The URL we actually ended on — relative links resolve against this, not
    #: against what was typed, or a redirecting site points them at the old host.
    source_url: str
    name: Optional[str] = None
    description: Optional[str] = None
    theme_color: Optional[str] = None
    #: Absolute http(s) URLs, most promising first. Fetched in 1.5.
    icon_candidates: list[str] = field(default_factory=list)
    #: Filled in 1.5.
    colors: list[str] = field(default_factory=list)
    logo: Optional[tuple[bytes, str]] = None


def normalise_color(value: Optional[str]) -> Optional[str]:
    """A CSS colour as '#rrggbb' lowercase, or None if we can't be sure.

    The output feeds slide_accent_color, whose validator accepts exactly
    `#rrggbb` (models/schemas.py) — so anything this can't convert with
    certainty has to be dropped rather than passed along and rejected later.
    Named colours ('royalblue') would need a 148-entry table to gain one guess,
    and a site that cares about its colour states it in hex.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    if _HEX6_RE.match(raw):
        return raw.lower()
    short = _HEX3_RE.match(raw)
    if short:
        r, g, b = short.groups()
        return f"#{r}{r}{g}{g}{b}{b}".lower()
    rgb = _RGB_RE.match(raw)
    if rgb:
        parts = [int(p) for p in rgb.groups()]
        if all(0 <= p <= 255 for p in parts):
            return "#{:02x}{:02x}{:02x}".format(*parts)
    return None


def _meta(soup: BeautifulSoup, *, name: str = "", prop: str = "") -> Optional[str]:
    if name:
        tag = soup.find("meta", attrs={"name": re.compile(rf"\A{name}\Z", re.I)})
    else:
        tag = soup.find("meta", attrs={"property": re.compile(rf"\A{prop}\Z", re.I)})
    if tag is None:
        return None
    return (tag.get("content") or "").strip() or None


def _name_from_title(title: str) -> Optional[str]:
    """The brand half of a page title.

    Shorter-side rather than first-side: both "Acme | Anvils Since 1949" and
    "Anvils Since 1949 — Acme" are common, and the company name is the short
    one either way.
    """
    parts = [p.strip() for p in _TITLE_SPLIT_RE.split(title) if p.strip()]
    if not parts:
        return None
    useful = [p for p in parts if p.lower() not in _GENERIC_TITLE_PARTS] or parts
    return min(useful, key=len)


def _largest_declared_size(sizes: Optional[str]) -> int:
    """The biggest edge in a `sizes` attribute; 0 when it says nothing useful
    (including sizes="any", which is honest but not comparable)."""
    return max((int(m.group(1)) for m in _SIZE_RE.finditer(sizes or "")), default=0)


def _icon_candidates(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Icon URLs, most promising first.

    apple-touch-icon leads because it is the one link type that is reliably a
    large square raster; a bare <link rel=icon> is often a 16px .ico, and
    og:image is usually a social banner rather than a mark — useful for colour,
    poor as a logo, hence last among declared things.
    """
    ranked: list[tuple[int, int, str]] = []
    for link in soup.find_all("link", href=True):
        rels = {str(r).lower() for r in (link.get("rel") or [])}
        if not rels & _ICON_RELS:
            continue
        url = _absolute(base_url, link["href"])
        if url:
            ranked.append((0 if rels & _APPLE_RELS else 1,
                           -_largest_declared_size(link.get("sizes")), url))
    ranked.sort()

    ordered = [url for _rank, _size, url in ranked]
    og_image = _absolute(base_url, _meta(soup, prop="og:image") or "")
    if og_image:
        ordered.append(og_image)
    # Undeclared but nearly universal, so it is worth one try at the end.
    ordered.append(urljoin(base_url, "/favicon.ico"))

    return list(dict.fromkeys(ordered))


def _absolute(base_url: str, href: str) -> Optional[str]:
    """An absolute http(s) URL, or None.

    data: and other schemes are dropped here rather than downstream: the guard
    would refuse them, so keeping them would turn a readable page into a
    blocked fetch for no reason.
    """
    href = (href or "").strip()
    if not href:
        return None
    url = urljoin(base_url, href)
    return url if urlparse(url).scheme in ("http", "https") else None


async def extract_brand(url: str, *, ssl_verify: bool = True) -> BrandInfo:
    """Fetch a page and read what it says about the brand behind it."""
    try:
        resp = await guarded_get(
            url, ssl_verify=ssl_verify, timeout=20.0, max_bytes=_MAX_BYTES,
            headers={"User-Agent": "ContentEngine"},
        )
        resp.raise_for_status()
    except BlockedURL as e:
        raise BrandExtractError(str(e)) from e
    except httpx.HTTPStatusError as e:
        raise BrandExtractError(
            f"That site answered {e.response.status_code}.") from e
    except httpx.RequestError as e:
        raise BrandExtractError(describe_request_error(e, "That site")) from e

    final_url = str(resp.url)
    soup = BeautifulSoup(resp.text, "html.parser")

    name = _meta(soup, prop="og:site_name") or _meta(soup, prop="og:title")
    if not name and soup.title and soup.title.string:
        name = _name_from_title(soup.title.string.strip())

    description = (_meta(soup, prop="og:description")
                   or _meta(soup, name="description"))

    return BrandInfo(
        source_url=final_url,
        name=(name or "").strip()[:_MAX_NAME] or None,
        description=(description or "").strip()[:_MAX_DESCRIPTION] or None,
        theme_color=normalise_color(_meta(soup, name="theme-color")),
        icon_candidates=_icon_candidates(soup, final_url),
    )
