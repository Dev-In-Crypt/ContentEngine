"""Reading a brand off a website (phases 1.4 and 1.5).

HTML goes in as inline strings through pytest_httpx — the house convention
(tests/test_sources.py does the same, and there is no fixtures directory).
Images are generated with Pillow for the same reason.
tests/conftest.py already points DNS at a public address, so the guard these
calls run through is satisfied without each test saying so.
"""
import io

import pytest
from PIL import Image
from pytest_httpx import HTTPXMock

from services import brand_extract, url_guard
from services.brand_extract import (
    BrandExtractError, BrandInfo, dominant_colors, extract_brand,
    fetch_brand_logo, guess_niche, normalise_color,
)
from services.url_guard import BLOCKED_MESSAGE

URL = "https://acme.example/"
ICON = "https://acme.example/logo.png"


def _html(head: str, body: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def _page(httpx_mock: HTTPXMock, head: str, **kw):
    httpx_mock.add_response(text=_html(head), headers={"content-type": "text/html"}, **kw)


def _image(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _mark(background, foreground, *, span: int = 8) -> Image.Image:
    """A 32×32 field of `background` with a `foreground` stripe down the left —
    the shape of a real logo: a lot of backdrop, a little brand colour."""
    img = Image.new("RGBA", (32, 32), background)
    img.paste(foreground, (0, 0, span, 32))
    return img


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


async def test_og_title_is_split_like_any_other_page_title(httpx_mock: HTTPXMock):
    """Found by the live check against stripe.com after 1.7 shipped: Stripe
    sets no og:site_name, and its og:title carries the same "Page | Brand"
    shape a <title> does. Taking it whole handed the user
    "Online-Bezahldienst und Zahlungsdienstleister | Stripe" to delete by hand
    — the exact typing this endpoint exists to remove."""
    _page(httpx_mock, '<meta property="og:title" '
                      'content="Online payments and financial services | Stripe">')
    assert (await extract_brand(URL)).name == "Stripe"


async def test_og_site_name_is_taken_whole_even_with_a_separator(
        httpx_mock: HTTPXMock):
    """The asymmetry is the point. og:site_name is a direct answer to "what is
    this site called" — whatever is in it was chosen as the answer, separator
    and all. og:title and <title> are page titles, and the convention there is
    "Page | Brand"; guessing at those is what the heuristic is for."""
    _page(httpx_mock, '<meta property="og:site_name" content="Ben & Jerry | Co">')
    assert (await extract_brand(URL)).name == "Ben & Jerry | Co"


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


# ================================================================== phase 1.5
# ------------------------------------------------------------------ palette

def test_a_white_logo_does_not_offer_white(httpx_mock: HTTPXMock):
    """The commonest pixel in a logo is almost always its backdrop. Offering
    white as the brand colour would make the accent invisible on the slide it
    is painted on."""
    colors = dominant_colors(_image(_mark((255, 255, 255, 255), (10, 37, 64, 255))))
    assert colors == ["#0a2540"]


def test_transparent_pixels_are_not_counted():
    """Mutation guard: a logo is usually a small mark on a transparent square,
    and the transparent part is often left some arbitrary colour by whatever
    exported it. Count it and you return a colour nobody can see — here green
    outnumbers red three to one and must still lose."""
    colors = dominant_colors(_image(_mark((20, 200, 20, 0), (200, 30, 30, 255))))
    assert colors == ["#c81e1e"]


def test_near_black_and_grey_are_skipped():
    """Neither says anything about a brand, and both read as "no colour chosen"
    when painted on a slide."""
    assert dominant_colors(_image(Image.new("RGBA", (32, 32), (128, 130, 132, 255)))) == []
    assert dominant_colors(_image(Image.new("RGBA", (32, 32), (4, 4, 6, 255)))) == []


def test_colors_come_back_most_used_first():
    img = _mark((10, 37, 64, 255), (200, 30, 30, 255), span=8)   # 3:1 blue:red
    assert dominant_colors(img and _image(img)) == ["#0a2540", "#c81e1e"]


def test_near_identical_shades_collapse_into_one():
    """Mutation guard: an anti-aliased or JPEG-compressed logo has hundreds of
    almost-equal blues. Count them as distinct and the list is three shades of
    the same colour instead of three colours."""
    img = Image.new("RGBA", (32, 32), (10, 37, 64, 255))
    img.paste((11, 38, 65, 255), (0, 0, 16, 32))
    assert len(dominant_colors(img and _image(img))) == 1


def test_the_list_is_capped():
    img = Image.new("RGBA", (32, 32))
    for i, c in enumerate([(200, 30, 30), (30, 200, 30), (30, 30, 200),
                           (200, 200, 30), (200, 30, 200), (30, 200, 200)]):
        img.paste(c + (255,), (0, i * 5, 32, i * 5 + 5))
    assert len(dominant_colors(img and _image(img), limit=3)) == 3


def test_unreadable_bytes_yield_no_colours():
    assert dominant_colors(b"not an image at all") == []


# ------------------------------------------------------------------ logo fetch

async def test_the_first_icon_that_decodes_wins(httpx_mock: HTTPXMock):
    """Candidates are ranked guesses, not promises — a declared apple-touch-icon
    that 404s must not cost us the favicon behind it."""
    httpx_mock.add_response(url=ICON, status_code=404)
    httpx_mock.add_response(url="https://acme.example/favicon.ico",
                            content=_image(_mark((255, 255, 255, 255), (10, 37, 64, 255))),
                            headers={"content-type": "image/png"})
    info = await fetch_brand_logo(BrandInfo(
        source_url=URL, icon_candidates=[ICON, "https://acme.example/favicon.ico"]))
    assert info.logo is not None and info.logo[1] == "image/png"
    assert info.colors == ["#0a2540"]


async def test_an_html_error_page_served_as_an_icon_is_skipped(httpx_mock: HTTPXMock):
    """Plenty of sites answer 200 with their own 'not found' page. Pillow is the
    only honest test of whether bytes are an image."""
    httpx_mock.add_response(url=ICON, text="<html>Not found</html>",
                            headers={"content-type": "image/png"})
    info = await fetch_brand_logo(BrandInfo(source_url=URL, icon_candidates=[ICON]))
    assert info.logo is None


async def test_an_svg_icon_is_not_even_fetched(httpx_mock: HTTPXMock):
    """Pillow doesn't rasterise SVG and cairosvg is a native dependency in the
    Docker image for one edge case. Asserting only "no logo" would prove
    nothing — Pillow rejects SVG bytes anyway — so this asserts the request
    never happened, which is the whole value of recognising the extension: one
    fewer round-trip to an unvetted host while a page waits to render. An
    SVG-only site keeps whatever theme-color it declared."""
    svg = "https://acme.example/logo.svg"
    httpx_mock.add_response(url=svg, text="<svg xmlns='http://www.w3.org/2000/svg'/>",
                            headers={"content-type": "image/svg+xml"}, is_optional=True)
    info = await fetch_brand_logo(BrandInfo(source_url=URL, icon_candidates=[svg]))
    assert info.logo is None
    assert httpx_mock.get_requests() == []


async def test_an_ico_is_converted_to_png(httpx_mock: HTTPXMock):
    """logo_store's allow-list is png/webp/jpeg (logo_store.py, duplicated in
    settings.py). Converting here means that list doesn't have to grow for the
    one format every site still serves."""
    httpx_mock.add_response(
        url=ICON, content=_image(_mark((255, 255, 255, 255), (10, 37, 64, 255)), "ICO"),
        headers={"content-type": "image/x-icon"})
    info = await fetch_brand_logo(BrandInfo(source_url=URL, icon_candidates=[ICON]))
    assert info.logo is not None
    assert info.logo[1] == "image/png"
    assert Image.open(io.BytesIO(info.logo[0])).format == "PNG"


async def test_a_jpeg_is_kept_as_it_is(httpx_mock: HTTPXMock):
    """Mutation guard: re-encoding every logo would turn a 40 KB JPEG into a
    megabyte of PNG for no gain."""
    httpx_mock.add_response(
        url=ICON, content=_image(_mark((255, 255, 255), (10, 37, 64), span=8).convert("RGB"),
                                 "JPEG"),
        headers={"content-type": "image/jpeg"})
    info = await fetch_brand_logo(BrandInfo(source_url=URL, icon_candidates=[ICON]))
    assert info.logo is not None and info.logo[1] == "image/jpeg"
    assert Image.open(io.BytesIO(info.logo[0])).format == "JPEG"


async def test_the_logo_download_goes_through_the_guard(monkeypatch,
                                                        httpx_mock: HTTPXMock):
    """The icon URL comes off a page we have just been handed — the host is
    chosen by whoever wrote that page, which is the whole criterion in
    url_guard. The working response is registered so an unguarded
    implementation gets a real PNG and fails on the assertion below."""
    monkeypatch.setattr(url_guard, "_resolve", lambda host: ["169.254.169.254"])
    httpx_mock.add_response(
        url=ICON, content=_image(_mark((255, 255, 255, 255), (10, 37, 64, 255))),
        headers={"content-type": "image/png"}, is_optional=True)
    info = await fetch_brand_logo(BrandInfo(source_url=URL, icon_candidates=[ICON]))
    assert info.logo is None and info.colors == []


async def test_an_oversized_logo_is_refused(monkeypatch, httpx_mock: HTTPXMock):
    monkeypatch.setattr(brand_extract, "_MAX_LOGO_BYTES", 32)
    httpx_mock.add_response(
        url=ICON, content=_image(_mark((255, 255, 255, 255), (10, 37, 64, 255))),
        headers={"content-type": "image/png"}, is_optional=True)
    info = await fetch_brand_logo(BrandInfo(source_url=URL, icon_candidates=[ICON]))
    assert info.logo is None


async def test_only_a_few_candidates_are_tried(httpx_mock: HTTPXMock):
    """Mutation guard: a page can declare a dozen icons, and every one is a
    request to a host we have not vetted, made while somebody waits for a
    landing page to answer."""
    urls = [f"https://acme.example/i{i}.png" for i in range(10)]
    for u in urls:
        httpx_mock.add_response(url=u, status_code=404, is_optional=True)
    await fetch_brand_logo(BrandInfo(source_url=URL, icon_candidates=urls))
    assert len(httpx_mock.get_requests()) <= 4


async def test_no_candidates_is_not_an_error():
    info = await fetch_brand_logo(BrandInfo(source_url=URL))
    assert info.logo is None and info.colors == []


# ------------------------------------------------------------------ theme-color

async def test_the_declared_theme_color_leads(httpx_mock: HTTPXMock):
    """A colour the brand states outright beats one we quantised out of its
    pixels — the logo's most common colour is often its outline, not its
    identity."""
    httpx_mock.add_response(
        url=ICON, content=_image(_mark((255, 255, 255, 255), (200, 30, 30, 255))),
        headers={"content-type": "image/png"})
    info = await fetch_brand_logo(BrandInfo(
        source_url=URL, theme_color="#0a2540", icon_candidates=[ICON]))
    assert info.colors[0] == "#0a2540"
    assert "#c81e1e" in info.colors


async def test_the_theme_color_is_not_listed_twice(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=ICON, content=_image(_mark((255, 255, 255, 255), (10, 37, 64, 255))),
        headers={"content-type": "image/png"})
    info = await fetch_brand_logo(BrandInfo(
        source_url=URL, theme_color="#0a2540", icon_candidates=[ICON]))
    assert info.colors.count("#0a2540") == 1


async def test_a_theme_color_survives_a_site_with_no_usable_logo():
    info = await fetch_brand_logo(BrandInfo(source_url=URL, theme_color="#0a2540"))
    assert info.colors == ["#0a2540"]


# ================================================================== phase 1.6
# guess_niche — the one LLM call in this module. Shape follows
# services/claim_check.py: parse, retry once, give up.

class StubProvider:
    """Same shape as tests/test_claim_check.py's — the house convention for a
    text provider that returns a canned answer."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def generate_text(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return (reply, [])


_GUESS = '{"niche": "industrial tooling", "target_audience": "factory buyers"}'


async def test_a_clean_answer_is_parsed():
    guess = await guess_niche(StubProvider(_GUESS), name="Acme",
                              description="We make anvils.")
    assert guess.niche == "industrial tooling"
    assert guess.target_audience == "factory buyers"


async def test_a_fenced_answer_is_parsed():
    """Models wrap JSON in ```json fences constantly; _loads already strips
    them, and this pins that guess_niche goes through it."""
    guess = await guess_niche(StubProvider(f"```json\n{_GUESS}\n```"),
                              name="Acme", description="We make anvils.")
    assert guess.niche == "industrial tooling"


async def test_broken_json_is_repaired():
    guess = await guess_niche(StubProvider('{"niche": "anvils", '),
                              name="Acme", description="We make anvils.")
    assert guess.niche == "anvils"


async def test_an_unusable_answer_is_retried_once():
    provider = StubProvider("sorry, I can't help with that", _GUESS)
    guess = await guess_niche(provider, name="Acme", description="We make anvils.")
    assert len(provider.calls) == 2
    assert guess.niche == "industrial tooling"


async def test_two_unusable_answers_give_an_empty_guess_not_an_error():
    """Mutation guard, and the one real deviation from claim_check's shape:
    that module RAISES on a second failure, because an unverified claim must
    block the post. A niche is a pre-filled form field. Raising here would
    throw away the name, the colours and the logo we already have because the
    optional extra didn't work."""
    provider = StubProvider("nope", "still nope")
    guess = await guess_niche(provider, name="Acme", description="We make anvils.")
    assert len(provider.calls) == 2
    assert guess.niche == "" and guess.target_audience == ""


async def test_a_provider_failure_gives_an_empty_guess():
    """Same reasoning: a dead key or a 429 must not cost the user the rest of
    the extraction."""
    guess = await guess_niche(StubProvider(RuntimeError("429 slow down")),
                              name="Acme", description="We make anvils.")
    assert guess.niche == ""


async def test_nothing_to_go_on_costs_nothing():
    """Mutation guard: a page with neither name nor description gives the model
    literally nothing to work from, and the call is somebody's money."""
    provider = StubProvider(_GUESS)
    guess = await guess_niche(provider, name=None, description=None)
    assert provider.calls == []
    assert guess.niche == ""


async def test_the_name_and_description_reach_the_model():
    provider = StubProvider(_GUESS)
    await guess_niche(provider, name="Acme", description="We make anvils.")
    prompt = provider.calls[0]["user_prompt"]
    assert "Acme" in prompt and "We make anvils." in prompt


async def test_long_answers_are_capped_to_the_column():
    """niche and target_audience are String(120) (models/database.py). A model
    that writes a paragraph must not fail the INSERT two layers later."""
    long = '{{"niche": "{}", "target_audience": "{}"}}'.format("x" * 500, "y" * 500)
    guess = await guess_niche(StubProvider(long), name="Acme", description="Anvils.")
    assert len(guess.niche) <= 120 and len(guess.target_audience) <= 120


async def test_non_string_values_are_ignored():
    guess = await guess_niche(StubProvider('{"niche": ["a", "b"], "target_audience": 7}'),
                              name="Acme", description="Anvils.")
    assert guess.niche == "" and guess.target_audience == ""


async def test_a_list_reply_is_not_mistaken_for_an_answer():
    guess = await guess_niche(StubProvider('["industrial tooling"]', '["still a list"]'),
                              name="Acme", description="Anvils.")
    assert guess.niche == ""


async def test_the_page_is_asked_for_in_a_language_the_product_speaks(httpx_mock: HTTPXMock):
    """Without Accept-Language a site negotiates on the server's IP address.

    Found by running onboarding against stripe.com from prod: the description
    came back as "Stripe ist eine Finanzdienstleistungsplattform…" because the
    box is in a German datacentre. The product is in English and so, presumably,
    is the person reading it — and the description does not just sit there, it
    seeds the niche field, which goes into the brand voice and from there into
    the language of generated posts.

    Invisible to every other test in this file, which serves its own English
    HTML and never negotiates anything.
    """
    _page(httpx_mock, "<title>Acme</title>")

    await extract_brand(URL)

    sent = httpx_mock.get_requests()[0].headers
    assert sent.get("accept-language", "").lower().startswith("en")
