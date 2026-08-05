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

import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

from services.http_utils import describe_request_error
from services.lead_builder import _loads
from services.url_guard import BlockedURL, guarded_get

log = logging.getLogger(__name__)

_MAX_BYTES = 2 * 1024 * 1024
_MAX_DESCRIPTION = 500
_MAX_NAME = 120                    # matches ProfileUpdate.brand_name

_MAX_LOGO_BYTES = 5 * 1024 * 1024
#: Candidates are ranked guesses, and every one is a request to a host we have
#: not vetted, made while somebody waits for a page to answer.
_MAX_ICON_TRIES = 4

#: Formats logo_store already accepts (logo_store.EXTENSIONS, duplicated in
#: settings.py) — anything else Pillow can read is converted to PNG.
_PASSTHROUGH_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg",
                        "WEBP": "image/webp"}

#: The palette is sampled at this size. Small enough to count every pixel,
#: large enough that a mark occupying a few percent of the square survives.
_SAMPLE_EDGE = 128
_OPAQUE_ENOUGH = 128
_NEAR_WHITE = 240
_NEAR_BLACK = 15
#: Below this spread between the strongest and weakest channel a colour reads
#: as grey, and grey says nothing about a brand.
_MIN_SATURATION = 25

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


# --------------------------------------------------------------- logo & colour

def dominant_colors(data: bytes, *, limit: int = 3) -> list[str]:
    """The brand colours in an image, most used first.

    Returns [] for anything unreadable — a logo we can't parse is a missing
    nicety, never a failed extraction.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            sample = img.convert("RGBA").resize(
                (_SAMPLE_EDGE, _SAMPLE_EDGE),
                # NEAREST, not the default: a smooth resample invents blended
                # pixels along every edge, and those blends are exactly the
                # muddy in-between colours this function is meant to avoid.
                Image.Resampling.NEAREST)
            pixels = list(sample.getdata())
    except (UnidentifiedImageError, OSError, ValueError) as e:
        log.debug("Unreadable logo bytes: %s", e)
        return []

    # Bucket by the top 4 bits per channel so the hundreds of almost-equal
    # blues in an anti-aliased logo count as one colour, then report the exact
    # shade that occurred most inside the winning bucket rather than the
    # rounded bucket centre — a brand's blue, not our approximation of it.
    buckets: dict[tuple[int, int, int], Counter] = {}
    for r, g, b, a in pixels:
        if a < _OPAQUE_ENOUGH:
            continue
        if r > _NEAR_WHITE and g > _NEAR_WHITE and b > _NEAR_WHITE:
            continue
        if r < _NEAR_BLACK and g < _NEAR_BLACK and b < _NEAR_BLACK:
            continue
        if max(r, g, b) - min(r, g, b) < _MIN_SATURATION:
            continue
        buckets.setdefault((r >> 4, g >> 4, b >> 4), Counter())[(r, g, b)] += 1

    ranked = sorted(buckets.values(), key=lambda c: -sum(c.values()))
    return ["#{:02x}{:02x}{:02x}".format(*c.most_common(1)[0][0])
            for c in ranked[:limit]]


async def _fetch_icon(url: str, *, ssl_verify: bool) -> Optional[tuple[bytes, str]]:
    """One icon candidate as (bytes, mime), or None if it isn't usable.

    SVG is skipped rather than rasterised: Pillow can't, and cairosvg is a
    native dependency in the Docker image for one edge case. An SVG-only site
    gets no logo and keeps whatever theme-color it declared.
    """
    if urlparse(url).path.lower().endswith(".svg"):
        return None
    try:
        resp = await guarded_get(url, ssl_verify=ssl_verify, timeout=20.0,
                                 max_bytes=_MAX_LOGO_BYTES,
                                 headers={"User-Agent": "ContentEngine"})
        resp.raise_for_status()
    except (BlockedURL, httpx.HTTPError) as e:
        log.debug("Icon %s unavailable: %s", url, e)
        return None

    data = resp.content
    try:
        # Pillow is the only honest test of whether bytes are an image: plenty
        # of sites answer 200 with an HTML "not found" page and a cheerful
        # image/png header.
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or ""
            if fmt in _PASSTHROUGH_FORMATS:
                return data, _PASSTHROUGH_FORMATS[fmt]
            buf = io.BytesIO()
            img.convert("RGBA").save(buf, format="PNG")
    except (UnidentifiedImageError, OSError, ValueError) as e:
        log.debug("Icon %s is not a readable image: %s", url, e)
        return None
    return buf.getvalue(), "image/png"


async def fetch_brand_logo(info: BrandInfo, *,
                           ssl_verify: bool = True) -> BrandInfo:
    """Fill in `logo` and `colors` on a BrandInfo, in place.

    Kept apart from extract_brand deliberately. Reading the page is one request
    and always worth making; this is up to four more, to hosts named by that
    page, and a caller that only wants a name shouldn't pay for them.
    """
    for url in info.icon_candidates[:_MAX_ICON_TRIES]:
        found = await _fetch_icon(url, ssl_verify=ssl_verify)
        if found is not None:
            info.logo = found
            info.colors = dominant_colors(found[0])
            break

    # A colour the brand states outright beats one quantised out of its pixels:
    # the most common colour in a logo is often its outline, not its identity.
    if info.theme_color:
        info.colors = [info.theme_color] + [c for c in info.colors
                                            if c != info.theme_color]
    return info


# ------------------------------------------------------------------- the niche

NICHE_SYSTEM_PROMPT = """\
You are filling in a social-media marketing profile for a company, from nothing
but its own website blurb.

Return ONLY a JSON object, no markdown:
{"niche": "...", "target_audience": "..."}

RULES:
- "niche": what the company actually sells or does, as a marketer would file it.
  Three to six words, lowercase, no company name, no slogan.
- "target_audience": who buys it. Three to six words, plain and concrete.
- Work ONLY from the text given. If it says too little to tell, use "" for that
  field rather than a guess that sounds plausible.
"""

_MAX_PROFILE_FIELD = 120           # matches User.niche / User.target_audience


@dataclass
class NicheGuess:
    niche: str = ""
    target_audience: str = ""


def _one_line(value: object) -> str:
    """A model that answers with a list or a number gets nothing through."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:_MAX_PROFILE_FIELD]


async def guess_niche(text_provider, *, name: Optional[str],
                      description: Optional[str],
                      text_model: str = "") -> NicheGuess:
    """Guess the profile fields a website doesn't state outright.

    Separate from extract_brand on purpose: the whole page read stays testable
    without an LLM in sight, and this stays one call with one job.

    Never raises. Unlike claim_check, whose second failure has to block a post,
    a niche is a pre-filled form field — the user is looking at one, and
    throwing away the name, the colours and the logo we already have because
    the optional extra didn't work would be the wrong trade every time.
    """
    name, description = (name or "").strip(), (description or "").strip()
    if not name and not description:
        # Nothing to reason from, and the call is somebody's money.
        return NicheGuess()

    user = f"COMPANY: {name or '(not stated)'}\nWEBSITE SAYS: {description or '(nothing)'}"

    async def _call() -> Optional[dict]:
        raw, _cit = await text_provider.generate_text(
            model=text_model, system_prompt=NICHE_SYSTEM_PROMPT,
            user_prompt=user, max_tokens=200)
        data = _loads(raw)
        return data if isinstance(data, dict) else None

    try:
        data = await _call()
        if data is None:
            log.warning("Niche guess came back unparseable; retrying once")
            data = await _call()
    except Exception as e:  # noqa: BLE001 — a dead key must not cost the rest
        log.warning("Niche guess unavailable: %s", e)
        return NicheGuess()

    if data is None:
        return NicheGuess()
    return NicheGuess(niche=_one_line(data.get("niche")),
                      target_audience=_one_line(data.get("target_audience")))
