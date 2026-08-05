"""Reading a brand off a website (phase 1.4).

HTML goes in as inline strings through pytest_httpx — the house convention
(tests/test_sources.py does the same, and there is no fixtures directory).
tests/conftest.py already points DNS at a public address, so the guard these
calls run through is satisfied without each test saying so.
"""
import pytest
from pytest_httpx import HTTPXMock

from services import url_guard
from services.brand_extract import (
    BrandExtractError, extract_brand, normalise_color,
)
from services.url_guard import BLOCKED_MESSAGE

URL = "https://acme.example/"


def _html(head: str, body: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def _page(httpx_mock: HTTPXMock, head: str, **kw):
    httpx_mock.add_response(text=_html(head), headers={"content-type": "text/html"}, **kw)


# ------------------------------------------------------------------ name

async def test_og_site_name_wins(httpx_mock: HTTPXMock):
    """The only field a site states outright. Everything else is inference."""
    _page(httpx_mock, """
      <title>Acme | Industrial Anvils Since 1949</title>
      <meta property="og:site_name" content="Acme">
    """)
    assert (await extract_brand(URL)).name == "Acme"


async def test_title_falls_back_to_its_shorter_half(httpx_mock: HTTPXMock):
    """'Acme | Industrial Anvils Since 1949' should offer 'Acme', not the whole
    string — the user sees this in a field they'd otherwise retype every time.
    Shorter-side rather than first-side, because both orderings are common."""
    _page(httpx_mock, "<title>Acme | Industrial Anvils Since 1949</title>")
    assert (await extract_brand(URL)).name == "Acme"


async def test_title_split_works_in_either_order(httpx_mock: HTTPXMock):
    _page(httpx_mock, "<title>Industrial Anvils Since 1949 — Acme</title>")
    assert (await extract_brand(URL)).name == "Acme"


async def test_a_hyphenated_name_is_not_split(httpx_mock: HTTPXMock):
    """Mutation guard: split on a bare '-' and every Coca-Cola becomes 'Coca'.
    The dash separators require whitespace around them for exactly this."""
    _page(httpx_mock, "<title>Coca-Cola</title>")
    assert (await extract_brand(URL)).name == "Coca-Cola"


async def test_a_generic_half_is_skipped(httpx_mock: HTTPXMock):
    """'Home | Acme' — shorter is 'Home', which names nothing."""
    _page(httpx_mock, "<title>Home | Acme</title>")
    assert (await extract_brand(URL)).name == "Acme"


async def test_a_page_with_no_name_at_all(httpx_mock: HTTPXMock):
    _page(httpx_mock, "")
    assert (await extract_brand(URL)).name is None


# ------------------------------------------------------------------ description

async def test_og_description_wins_over_meta_description(httpx_mock: HTTPXMock):
    _page(httpx_mock, """
      <meta name="description" content="The plain one.">
      <meta property="og:description" content="The social one.">
    """)
    assert (await extract_brand(URL)).description == "The social one."


async def test_meta_description_is_the_fallback(httpx_mock: HTTPXMock):
    _page(httpx_mock, '<meta name="description" content="The plain one.">')
    assert (await extract_brand(URL)).description == "The plain one."


async def test_a_very_long_description_is_capped(httpx_mock: HTTPXMock):
    _page(httpx_mock, f'<meta name="description" content="{"x" * 2000}">')
    assert len((await extract_brand(URL)).description) <= 500


# ------------------------------------------------------------------ colour

@pytest.mark.parametrize("raw,expected", [
    ("#0A2540", "#0a2540"),
    ("#fff", "#ffffff"),
    ("  #FFF  ", "#ffffff"),
    ("rgb(10, 37, 64)", "#0a2540"),
    ("rgba(10,37,64,0.5)", "#0a2540"),
    ("royalblue", None),          # named colours are not worth a lookup table
    ("#gggggg", None),
    ("", None),
    (None, None),
])
def test_colour_normalisation(raw, expected):
    assert normalise_color(raw) == expected


async def test_theme_color_is_read_and_normalised(httpx_mock: HTTPXMock):
    _page(httpx_mock, '<meta name="theme-color" content="#0A2540">')
    assert (await extract_brand(URL)).theme_color == "#0a2540"


async def test_an_unusable_theme_color_is_dropped_not_kept_raw(httpx_mock: HTTPXMock):
    """Mutation guard: pass it through unnormalised and it reaches a profile
    field whose validator demands exactly #rrggbb (models/schemas.py)."""
    _page(httpx_mock, '<meta name="theme-color" content="royalblue">')
    assert (await extract_brand(URL)).theme_color is None


# ------------------------------------------------------------------ icons

async def test_apple_touch_icon_outranks_a_favicon(httpx_mock: HTTPXMock):
    """It's the one that's reliably a real, large, square PNG."""
    _page(httpx_mock, """
      <link rel="icon" href="/favicon-16.png" sizes="16x16">
      <link rel="apple-touch-icon" href="/touch.png">
    """)
    assert (await extract_brand(URL)).icon_candidates[0] == "https://acme.example/touch.png"


async def test_the_largest_declared_icon_wins_among_equals(httpx_mock: HTTPXMock):
    _page(httpx_mock, """
      <link rel="icon" href="/small.png" sizes="16x16">
      <link rel="icon" href="/big.png" sizes="192x192">
      <link rel="icon" href="/mid.png" sizes="32x32">
    """)
    assert (await extract_brand(URL)).icon_candidates[0] == "https://acme.example/big.png"


async def test_shortcut_icon_spelling_is_recognised(httpx_mock: HTTPXMock):
    """rel="shortcut icon" parses as ['shortcut', 'icon'] — still an icon."""
    _page(httpx_mock, '<link rel="shortcut icon" href="/fav.ico">')
    assert "https://acme.example/fav.ico" in (await extract_brand(URL)).icon_candidates


async def test_favicon_ico_is_always_offered_last(httpx_mock: HTTPXMock):
    """Sites that declare nothing still usually serve /favicon.ico."""
    _page(httpx_mock, "")
    assert (await extract_brand(URL)).icon_candidates == ["https://acme.example/favicon.ico"]


async def test_og_image_is_a_candidate_after_declared_icons(httpx_mock: HTTPXMock):
    _page(httpx_mock, """
      <link rel="apple-touch-icon" href="/touch.png">
      <meta property="og:image" content="/social.png">
    """)
    cands = (await extract_brand(URL)).icon_candidates
    assert cands.index("https://acme.example/touch.png") < \
           cands.index("https://acme.example/social.png")


async def test_a_data_uri_icon_is_dropped(httpx_mock: HTTPXMock):
    """Mutation guard: keep it and 1.5 hands the guard a scheme it must refuse,
    turning a perfectly good page into a blocked fetch."""
    _page(httpx_mock, '<link rel="icon" href="data:image/png;base64,iVBORw0KGgo=">')
    assert (await extract_brand(URL)).icon_candidates == \
           ["https://acme.example/favicon.ico"]


async def test_candidates_are_deduplicated(httpx_mock: HTTPXMock):
    _page(httpx_mock, """
      <link rel="icon" href="/fav.png">
      <link rel="shortcut icon" href="/fav.png">
    """)
    cands = (await extract_brand(URL)).icon_candidates
    assert cands.count("https://acme.example/fav.png") == 1


async def test_relative_icons_resolve_against_the_url_after_redirects(
        httpx_mock: HTTPXMock):
    """Mutation guard: resolve against the URL we asked for instead of the one
    we landed on and every relative path on a redirecting site points at the
    wrong host."""
    httpx_mock.add_response(url="https://acme.example/", status_code=301,
                            headers={"location": "https://www.acme.example/en/"})
    httpx_mock.add_response(url="https://www.acme.example/en/",
                            text=_html('<link rel="icon" href="logo.png">'),
                            headers={"content-type": "text/html"})
    info = await extract_brand(URL)
    assert info.icon_candidates[0] == "https://www.acme.example/en/logo.png"
    assert info.source_url == "https://www.acme.example/en/"


# ------------------------------------------------------------------ failures

async def test_a_blocked_address_says_only_what_the_guard_says(monkeypatch):
    """The reason must not travel any further here than anywhere else."""
    monkeypatch.setattr(url_guard, "_resolve", lambda host: ["169.254.169.254"])
    with pytest.raises(BrandExtractError) as err:
        await extract_brand("https://acme.example/")
    assert str(err.value) == BLOCKED_MESSAGE


async def test_an_http_error_is_reported_plainly(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=404, text="nope")
    with pytest.raises(BrandExtractError) as err:
        await extract_brand(URL)
    assert "404" in str(err.value)


async def test_a_page_that_is_not_html_still_yields_something(httpx_mock: HTTPXMock):
    """A JSON endpoint pasted by mistake shouldn't explode — it just has
    nothing to give."""
    httpx_mock.add_response(json={"hello": "world"})
    info = await extract_brand(URL)
    assert info.name is None and info.description is None
