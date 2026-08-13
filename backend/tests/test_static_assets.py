"""The shape of the served front end, asserted rather than assumed.

CSP phase 2 moved the two inline scripts out of index.html. That is the change
that makes `script-src 'self'` reachable at all, and it is also the kind of
change somebody undoes by accident — pasting a small script back into the HTML
is the natural thing to do, and it will work perfectly right up until the phase
that removes 'unsafe-inline', at which point the whole file stops running.

So the shape is a test. These assertions read the files on disk, which makes
them total: they cover the file, not the screens a browser test happens to
visit.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
INDEX = STATIC / "index.html"

#: Every `<script …>` … `</script>` pair, with whatever sits between the tags.
#: The body is what matters: `<script src=…></script>` is a legitimate empty
#: pair, and a pattern that only looked at the character after `>` would flag it.
_SCRIPT_PAIR = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.I | re.S)


def _html(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("page", ["index.html", "privacy.html", "terms.html"])
def test_no_inline_script_blocks(page):
    """Every script the pages run is fetched from this origin, so `'self'`
    alone can authorise all of it — no hashes to regenerate on every edit, and
    no `'unsafe-inline'`."""
    bodies = [b for b in _SCRIPT_PAIR.findall(_html(page)) if b.strip()]
    assert not bodies, f"{page} has {len(bodies)} inline <script> block(s)"


def test_the_bundles_exist_and_are_not_empty():
    assert (STATIC / "app.js").stat().st_size > 100_000     # the SPA, ~271 KB
    assert (STATIC / "theme.js").stat().st_size > 50        # the bootstrap


def test_the_theme_runs_before_anything_that_paints():
    """It sets data-theme, and it has to do so before first paint or the dark
    layout flashes light. A classic <script src> in <head> blocks the parser,
    so being early in the head is what guarantees it — and being ABOVE the
    407 KB Tailwind makes it strictly faster than the inline version was, which
    sat below it."""
    head = _html("index.html").split("</head>", 1)[0]
    theme = head.index('src="/static/theme.js"')
    tailwind = head.index('src="/static/vendor/tailwind.js"')
    assert theme < tailwind


def test_the_app_bundle_is_a_classic_script_at_the_end_of_the_body():
    """Three ways to get this wrong, two of them silent.

    `async`/`defer` would run it after parsing, and the file ends with two
    top-level getElementById(...).addEventListener(...) calls — those would
    become `null.addEventListener`. That one at least throws.

    `type="module"` is the quiet one: module scope is not global scope, so
    `const API` and all 136 functions the markup calls would stop existing on
    `window`, and every remaining inline handler would die at once — with no
    exception anywhere, just an app where nothing responds.
    """
    html = _html("index.html")
    tag = re.search(r'<script[^>]*src="/static/app\.js"[^>]*>', html)
    assert tag, "index.html does not load app.js"
    assert "defer" not in tag.group(0)
    assert "async" not in tag.group(0)
    assert "module" not in tag.group(0)
    # After the markup it drives, as the inline block was.
    assert html.index(tag.group(0)) > html.index('id="view-settings"')


def _toast_tag():
    from bs4 import BeautifulSoup
    el = BeautifulSoup(_html("index.html"), "html.parser").find(id="toast")
    assert el is not None, "index.html has no #toast"
    return el


def test_the_toast_hangs_off_the_body_and_nothing_else():
    """It is the app's only general-purpose way of saying anything, and it was
    nested inside `<section id="step-4">` — the composer's result screen, which
    is `display:none` on every other screen. So all 139 `toast()` calls wrote
    into a box with a hidden ancestor: the text was set, `hidden` was removed,
    and the element still measured 0x0.

    A direct child of <body> has no ancestor anybody can hide, which is the only
    version of this that cannot come back.
    """
    parent = _toast_tag().parent
    assert parent.name == "body", (
        f"#toast sits inside <{parent.name} id={parent.get('id')!r}>, whose "
        "display is not this element's to control")


def test_the_toast_layer_survives_its_own_restyling():
    """`toast()` assigns `el.className` wholesale on every call, so a z-index
    carried as a class is gone the first time a toast fires. The layer therefore
    lives in the inline style, which a className assignment does not touch.

    Above 50 specifically: the landing is `fixed inset-0 z-40` and the auth,
    forgot and reset screens are z-50 — and those are precisely the screens a
    link from an email lands on.
    """
    style = (_toast_tag().get("style") or "").replace(" ", "")
    m = re.search(r"z-index:(\d+)", style)
    assert m, "#toast carries no inline z-index; a class would be wiped by toast()"
    assert int(m.group(1)) > 50, f"z-index {m.group(1)} is under the auth screens"


def test_nothing_fetches_a_data_url():
    """`connect-src` is `'self'` with no `data:`, and this is what let it be.

    One call used to `fetch()` a FileReader data URL to turn it into a Blob.
    Keeping a directive permanently open for five lines of convenience is a bad
    trade; the decode is inline now.
    """
    assert "fetch(read.logo_data_url)" not in (STATIC / "app.js").read_text(encoding="utf-8")


def test_every_fullscreen_screen_is_covered_by_the_inert_list():
    """The list in app.js has to match the markup, or the screen somebody adds
    next covers the app visually and leaves it tabbable underneath.

    Derived from the markup rather than restated: anything `fixed inset-0` that
    starts `hidden` is a screen laid over the product, which is exactly the
    shape that needs the shell made inert. Modals are `fixed inset-0` too and
    are deliberately out — they are opened from inside the app, over a shell
    that should stay where it is; the ones here replace it.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(_html("index.html"), "html.parser")
    overlays = {
        el.get("id") for el in soup.find_all(attrs={"class": True})
        if el.get("id")
        and "fixed" in el["class"] and "inset-0" in el["class"]
        and "hidden" in el["class"]
        and el.get("id", "").endswith("-screen")
    }
    listed = set(re.findall(r"'([a-z-]+-screen)'",
                            (STATIC / "app.js").read_text(encoding="utf-8")
                            .split("const FULLSCREEN_SCREENS")[1].split("]")[0]))
    assert overlays == listed, (
        f"markup has {sorted(overlays)}, app.js lists {sorted(listed)}")


def test_the_server_is_started_behind_a_proxy_it_trusts():
    """Every "per address" limit in this app depends on one flag in the Dockerfile.

    Without --proxy-headers uvicorn reports the peer that opened the socket, and
    behind the reverse proxy that is the proxy: on the live deployment every
    request arrived from 172.18.0.5. So the landing's hourly limit, the sign-up
    limit and the free-post count per address were all one shared limit for the
    entire internet — one busy visitor spending everyone else's allowance, with
    nothing in any log to suggest it.

    Nothing in the Python can detect this: the app is handed an address and
    believes it. The only place it can be asserted is the command that starts the
    server.
    """
    cmd = (Path(__file__).resolve().parent.parent.parent / "Dockerfile").read_text(
        encoding="utf-8")
    assert "--proxy-headers" in cmd
    assert "--forwarded-allow-ips" in cmd
