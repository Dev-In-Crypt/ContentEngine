"""The Content-Security-Policy, directive by directive.

This is phase 1 of the CSP work: everything EXCEPT the script directive, which
stays permissive so that script behaviour is byte-for-byte what it is today. The
half that ships here is not a placeholder — `base-uri 'none'` closes `<base>`
injection, which turns every relative URL in the app into an attacker-controlled
one and nothing else covers it; `object-src`/`frame-src 'none'` close the plugin
and frame vectors; `form-action 'none'` is free because the app has no forms at
all; and locking `img-src`/`media-src`/`font-src`/`connect-src` to this origin
means an injected script that does run still cannot send anything anywhere.

Directives are compared as parsed sets, never as a substring of the whole
policy. Substring matching is precisely how a directive goes missing during an
edit and nobody notices: `"object-src 'none'" in policy` stays true while the
directive it was guarding drifts.

The header comes from the application rather than from Caddy, and that is a
deployment decision before it is a testing one. `render.yaml` runs the Dockerfile
behind Render's own TLS and `InstaContentEngine.pyw` is a desktop uvicorn —
neither has Caddy in front. A policy in the Caddyfile would protect the
docker-compose self-host and leave the other two with nothing.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from main import (
    CSP_DIRECTIVES,
    REVALIDATE_PATHS,
    SecurityHeadersMiddleware,
    app,
    docs_exempt_paths,
    security_policy,
)


def parse_csp(header: str) -> dict[str, set[str]]:
    """`"a 'self'; b 'none'"` → `{"a": {"'self'"}, "b": {"'none'"}}`."""
    out: dict[str, set[str]] = {}
    for part in header.split(";"):
        bits = part.split()
        if bits:
            out[bits[0]] = set(bits[1:])
    return out


@pytest.fixture
def client():
    return TestClient(app)


def policy_of(response) -> dict[str, set[str]]:
    header = response.headers.get("content-security-policy")
    assert header, "no Content-Security-Policy header at all"
    return parse_csp(header)


# ── the policy itself ───────────────────────────────────────────────────────

def test_the_app_shell_carries_a_policy(client):
    assert policy_of(client.get("/"))


@pytest.mark.parametrize("path", ["/", "/privacy", "/terms", "/health",
                                  "/static/index.html"])
def test_every_kind_of_response_carries_it(client, path):
    """The HTML, the legal pages, an API route, and the static mount. The mount
    is the one worth naming: `add_middleware` covering `app.mount(...)` is true
    but not obvious, and `/static/app.js` is about to hold the entire SPA."""
    assert policy_of(client.get(path))


def test_nothing_may_be_loaded_from_anywhere_else(client):
    """The app has no third-party origins at all — Tailwind and the fonts are
    vendored under /static/vendor. So every fetch directive is this origin, and
    an injected script has nowhere to send what it steals."""
    csp = policy_of(client.get("/"))
    assert csp["default-src"] == {"'self'"}
    assert csp["font-src"] == {"'self'"}
    # No `data:` either. It was here for one call — a fetch() of a FileReader
    # data URL — and that call is now an inline decode, so the directive closed
    # with it rather than staying open for convenience.
    assert csp["connect-src"] == {"'self'"}


def test_the_page_cannot_be_reframed_or_re_based(client):
    """`base-uri` is the one nothing else covers: a single injected `<base>`
    silently repoints every relative URL in the app, including the API calls."""
    csp = policy_of(client.get("/"))
    assert csp["base-uri"] == {"'none'"}
    assert csp["frame-ancestors"] == {"'none'"}
    assert csp["frame-src"] == {"'none'"}


def test_plugins_forms_workers_and_manifests_are_all_refused(client):
    """Free wins: the app has no <form>, no worker, no plugin and no manifest,
    so each of these costs nothing and closes a vector outright."""
    csp = policy_of(client.get("/"))
    for directive in ("object-src", "form-action", "worker-src", "manifest-src"):
        assert csp[directive] == {"'none'"}, directive


def test_images_may_come_from_a_data_url_or_a_blob(client):
    """A generated post arrives as `image_data_url`, and four places build blob:
    URLs for previews and downloads. Both are same-document by construction."""
    csp = policy_of(client.get("/"))
    assert csp["img-src"] == {"'self'", "data:", "blob:"}
    assert csp["media-src"] == {"'self'", "blob:"}


def test_no_dynamic_evaluation_is_permitted(client):
    """The whole exercise is viable only because the codebase — the vendored
    Tailwind included — contains no eval, no Function constructor and no string
    setTimeout. `'unsafe-eval'` would give all of that back."""
    assert "'unsafe-eval'" not in policy_of(client.get("/"))["script-src"]


def test_style_src_keeps_unsafe_inline_usable(client):
    """Tailwind Play builds its stylesheet at runtime from the classes in the
    DOM, so that text cannot be hashed and `'unsafe-inline'` has to stay.

    The trap this guards: adding a nonce or a hash to `style-src` makes the spec
    IGNORE `'unsafe-inline'` for that directive. Someone hardening the policy
    with a nonce would block Tailwind's injected <style> and every style=
    attribute at once, and the app would render as unstyled HTML.
    """
    style = policy_of(client.get("/"))["style-src"]
    assert "'unsafe-inline'" in style
    assert not [s for s in style if s.startswith(("'nonce-", "'sha256-", "'sha384-", "'sha512-"))]


def test_inline_script_is_refused(client):
    """The point of the whole exercise.

    `script-src 'self'` with no `'unsafe-inline'` means an injected `<script>`
    does not run and an injected `onerror=` does not compile — which is the
    attack the escaping helper in app.js has been carrying alone, with an API
    token in localStorage behind it.

    Reaching this was a no-op by construction: the handlers were gone, proved by
    a static count of exactly zero, before this line changed.
    """
    script = policy_of(client.get("/"))["script-src"]
    assert script == {"'self'"}, script


# ── the API docs, which are live off cloud ──────────────────────────────────

def _mini(app_mode: str) -> TestClient:
    """The middleware over a bare app, so the exemption can be tested both ways
    without re-importing main under a different mode."""
    mini = FastAPI()

    @mini.get("/docs")
    def docs() -> dict:
        return {}

    @mini.get("/elsewhere")
    def elsewhere() -> dict:
        return {}

    mini.add_middleware(SecurityHeadersMiddleware, policy=security_policy(),
                        exempt=docs_exempt_paths(app_mode),
                        revalidate=REVALIDATE_PATHS)
    return TestClient(mini)


def test_api_docs_are_not_broken_off_cloud():
    """Swagger is served only when app_mode != cloud (main._docs_urls), and it
    loads its bundle from a CDN with an inline bootstrap. A strict policy over
    it breaks the docs on every developer machine and in the desktop build —
    somewhere CI would never look."""
    assert "content-security-policy" not in _mini("local").get("/docs").headers


def test_the_exemption_is_only_for_the_docs():
    assert _mini("local").get("/elsewhere").headers["content-security-policy"]


def test_in_the_cloud_that_path_is_the_app_and_keeps_its_policy():
    """In cloud the three doc paths are unregistered, so /docs falls through to
    the SPA. Exempting it there would hand out the app shell with no policy."""
    assert docs_exempt_paths("cloud") == frozenset()
    assert _mini("cloud").get("/docs").headers["content-security-policy"]


# ── the bundle that is about to exist ───────────────────────────────────────

@pytest.mark.parametrize("path", ["/static/app.js", "/static/theme.js"])
def test_the_app_bundle_revalidates(client, path):
    """A new class of bug arrives with phase 2. StaticFiles sends an ETag and no
    Cache-Control, so a browser applies heuristic freshness — and a 271 KB
    app.js that has not changed in a month can be served from cache for days
    after a deploy, against a freshly fetched index.html. Impossible today,
    because the JS *is* the HTML.

    Set here, before the files exist, so phase 2 cannot introduce the window.
    `no-cache` means revalidate, not "do not store": with the ETag it costs a
    304.
    """
    assert path in REVALIDATE_PATHS
    assert client.get(path).headers.get("cache-control") == "no-cache"


@pytest.mark.parametrize("path", ["/", "/static/index.html", "/privacy", "/terms",
                                  "/verify?token=x"])
def test_every_document_revalidates(client, path):
    """The half the bundle rule assumed rather than checked.

    It reasoned about "a stale app.js against a freshly fetched index.html" —
    but index.html has no Cache-Control either, so the browser applies the same
    heuristic freshness to the document, and the document is the file that
    carries all the markup and names the bundles. Deploying a markup fix and
    watching the old markup keep rendering is how this was found.

    By content type rather than by a list of paths: the shell is served from `/`,
    from /static/index.html and from every SPA fallback route an emailed link
    uses, and a hand-kept list of those goes stale the first time somebody adds
    a route.
    """
    r = client.get(path)
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers.get("cache-control") == "no-cache"


def test_bytes_that_are_not_documents_stay_cacheable(client):
    """Scoped to documents on purpose. A blanket rule would put `no-cache` on
    the generated slides and the reel MP4s, which are immutable once written and
    are the heaviest thing this app serves — a revalidation round trip per image
    on every render of a feed."""
    r = client.get("/health")
    assert not r.headers["content-type"].startswith("text/html")
    assert "no-cache" not in r.headers.get("cache-control", "")


def test_ordinary_static_files_are_left_alone(client):
    """Only the two hand-written bundles revalidate. Doing it to the 407 KB
    Tailwind and the fonts would spend a round trip per page load to re-learn
    what has not changed since the image was built."""
    assert "no-cache" not in (client.get("/static/vendor/tailwind.js")
                              .headers.get("cache-control", ""))


# ── one source of truth ─────────────────────────────────────────────────────

def test_the_served_policy_is_the_declared_one(client):
    """The header is built from CSP_DIRECTIVES rather than typed twice."""
    assert policy_of(client.get("/")) == {k: set(v.split())
                                          for k, v in CSP_DIRECTIVES.items()}
