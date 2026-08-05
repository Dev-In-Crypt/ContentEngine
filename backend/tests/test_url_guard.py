"""The SSRF guard.

Every test here patches `url_guard._resolve` — the DNS seam — so nothing touches
a real resolver. The HTTP half uses pytest_httpx, the house convention (see
tests/test_sources.py).

The two tests that matter most are the redirect one and the uniform-message one.
A guard that validates the URL once, before the request, passes every other test
in this file and is still wide open: the attacker just answers 302. And a guard
whose message names the reason turns `services/fact_check.py`'s
`str(e)[:200]` echo into a port scanner for our own network.
"""
import gzip

import httpx
import pytest
from pytest_httpx import HTTPXMock

from services import url_guard
from services.url_guard import BLOCKED_MESSAGE, BlockedURL, guarded_get

PUBLIC = "93.184.216.34"


def _resolves_to(monkeypatch, addr):
    monkeypatch.setattr(url_guard, "_resolve", lambda host: [addr])


def _public(monkeypatch):
    _resolves_to(monkeypatch, PUBLIC)


# ------------------------------------------------------------------ addresses

@pytest.mark.parametrize("addr", [
    "127.0.0.1",            # loopback — the app itself
    "10.0.0.1",             # private
    "172.17.0.2",           # docker bridge — where Postgres lives
    "192.168.1.1",          # private
    "169.254.169.254",      # cloud metadata (Hetzner serves it here)
    "0.0.0.0",              # unspecified
    "::1",                  # loopback v6
    "fc00::1",              # unique-local v6
    "fe80::1",              # link-local v6
    "224.0.0.1",            # multicast
    "::ffff:127.0.0.1",     # loopback wearing a v6 hat
])
async def test_a_forbidden_address_is_blocked(monkeypatch, addr):
    _resolves_to(monkeypatch, addr)
    with pytest.raises(BlockedURL):
        await guarded_get("https://example.com/")


async def test_a_public_address_is_allowed(httpx_mock: HTTPXMock, monkeypatch):
    _public(monkeypatch)
    httpx_mock.add_response(text="hello")
    resp = await guarded_get("https://example.com/")
    assert resp.text == "hello"


async def test_a_mapped_public_address_is_still_allowed(
        httpx_mock: HTTPXMock, monkeypatch):
    """Mutation guard, and not the one it looks like. Python reports the whole
    ::ffff:/96 range as is_reserved — including ::ffff:8.8.8.8 — so a guard that
    doesn't unwrap the embedded v4 refuses perfectly ordinary public sites
    whenever getaddrinfo answers in mapped form. That failure would surface as
    "some sites just don't work", which is far harder to trace than a block."""
    _resolves_to(monkeypatch, f"::ffff:{PUBLIC}")
    httpx_mock.add_response(text="reachable")
    assert (await guarded_get("https://example.com/")).text == "reachable"


async def test_one_forbidden_address_among_several_blocks_the_host(monkeypatch):
    """A host with both a public and a private A record must not be fetched —
    which of the two httpx picks is not ours to bet on."""
    monkeypatch.setattr(url_guard, "_resolve", lambda host: [PUBLIC, "10.0.0.5"])
    with pytest.raises(BlockedURL):
        await guarded_get("https://example.com/")


async def test_a_literal_private_ip_needs_no_dns(monkeypatch):
    def _boom(host):
        raise AssertionError("should not resolve a literal IP")
    monkeypatch.setattr(url_guard, "_resolve", _boom)
    with pytest.raises(BlockedURL):
        await guarded_get("http://169.254.169.254/latest/meta-data/")


async def test_an_unresolvable_host_is_blocked(monkeypatch):
    """Can't verify it, don't go there."""
    def _boom(host):
        raise OSError("Name or service not known")
    monkeypatch.setattr(url_guard, "_resolve", _boom)
    with pytest.raises(BlockedURL):
        await guarded_get("https://nowhere.invalid/")


# ------------------------------------------------------------------ scheme / port

@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://example.com/", "ftp://example.com/",
    "data:text/plain,hi",
])
async def test_a_non_http_scheme_is_blocked(monkeypatch, url):
    _public(monkeypatch)
    with pytest.raises(BlockedURL):
        await guarded_get(url)


async def test_an_unusual_port_is_blocked(httpx_mock: HTTPXMock, monkeypatch):
    """Closes most internal-service probing even if the address check somehow
    let a host through. The optional mock is there so dropping the port check
    fails this on the assertion instead of hanging on a real connection."""
    _public(monkeypatch)
    httpx_mock.add_response(url="http://example.com:8080/", text="internal",
                            is_optional=True)
    with pytest.raises(BlockedURL):
        await guarded_get("http://example.com:8080/")


async def test_the_default_ports_are_allowed(httpx_mock: HTTPXMock, monkeypatch):
    _public(monkeypatch)
    httpx_mock.add_response(text="ok")
    assert (await guarded_get("http://example.com:80/")).text == "ok"


# ------------------------------------------------------------------ redirects

async def test_a_redirect_to_a_forbidden_address_is_blocked(
        httpx_mock: HTTPXMock, monkeypatch):
    """THE test. A guard that checks only the URL it was handed passes
    everything else in this file and still fetches the metadata service,
    because the attacker controls the 302.

    Both responses are registered on purpose: a one-shot-check implementation
    reaches the second one and succeeds, so this fails on the assertion rather
    than on an unmocked request.
    """
    monkeypatch.setattr(url_guard, "_resolve",
                        lambda host: [PUBLIC] if host == "example.com"
                        else ["169.254.169.254"])
    httpx_mock.add_response(url="https://example.com/", status_code=302,
                            headers={"location": "http://metadata.internal/latest/"})
    # is_optional: a correct guard never requests this. It is registered so a
    # one-shot-check implementation reaches it and *succeeds*, making this test
    # fail on the assertion below rather than on an unmatched request.
    httpx_mock.add_response(url="http://metadata.internal/latest/",
                            text="ami-id\ninstance-id\n", is_optional=True)

    with pytest.raises(BlockedURL):
        await guarded_get("https://example.com/")


async def test_a_redirect_to_a_public_address_is_followed(
        httpx_mock: HTTPXMock, monkeypatch):
    _public(monkeypatch)
    httpx_mock.add_response(url="https://example.com/", status_code=301,
                            headers={"location": "https://example.com/moved"})
    httpx_mock.add_response(url="https://example.com/moved", text="arrived")

    resp = await guarded_get("https://example.com/")
    assert resp.text == "arrived"


async def test_a_relative_redirect_is_resolved_against_the_current_url(
        httpx_mock: HTTPXMock, monkeypatch):
    _public(monkeypatch)
    httpx_mock.add_response(url="https://example.com/a/b", status_code=302,
                            headers={"location": "../c"})
    httpx_mock.add_response(url="https://example.com/c", text="relative ok")

    assert (await guarded_get("https://example.com/a/b")).text == "relative ok"


async def test_a_redirect_loop_gives_up(httpx_mock: HTTPXMock, monkeypatch):
    _public(monkeypatch)
    httpx_mock.add_response(status_code=302,
                            headers={"location": "https://example.com/next"},
                            is_reusable=True)
    with pytest.raises(BlockedURL):
        await guarded_get("https://example.com/")


# ------------------------------------------------------------------ body cap

async def test_a_body_over_the_cap_is_blocked(httpx_mock: HTTPXMock, monkeypatch):
    _public(monkeypatch)
    httpx_mock.add_response(content=b"x" * 5000)
    with pytest.raises(BlockedURL):
        await guarded_get("https://example.com/", max_bytes=1000)


async def test_a_body_at_the_cap_is_allowed(httpx_mock: HTTPXMock, monkeypatch):
    _public(monkeypatch)
    httpx_mock.add_response(content=b"x" * 1000)
    resp = await guarded_get("https://example.com/", max_bytes=1000)
    assert len(resp.content) == 1000


# ------------------------------------------------------------------ the message

async def test_every_rejection_says_exactly_the_same_thing(monkeypatch):
    """Mutation guard: put the reason in the message and this fails. Callers
    echo the text to users (fact_check.py:86 puts str(e) in the API response,
    the demo streams it), so a message that told "refused" from "blocked"
    apart would report which internal ports are open."""
    _resolves_to(monkeypatch, "10.0.0.1")
    with pytest.raises(BlockedURL) as private_err:
        await guarded_get("https://example.com/")

    def _boom(host):
        raise OSError("Name or service not known")
    monkeypatch.setattr(url_guard, "_resolve", _boom)
    with pytest.raises(BlockedURL) as dns_err:
        await guarded_get("https://nowhere.invalid/")

    _public(monkeypatch)
    with pytest.raises(BlockedURL) as scheme_err:
        await guarded_get("file:///etc/passwd")

    messages = {str(private_err.value), str(dns_err.value), str(scheme_err.value)}
    assert messages == {BLOCKED_MESSAGE}


async def test_the_real_reason_is_kept_for_the_log(monkeypatch):
    """Uniform to the caller, specific to us — otherwise nobody can debug it."""
    _resolves_to(monkeypatch, "10.0.0.1")
    with pytest.raises(BlockedURL) as err:
        await guarded_get("https://example.com/")
    assert "10.0.0.1" in err.value.reason


# ------------------------------------------------------------------ response shape

async def test_the_response_behaves_like_a_normal_httpx_response(
        httpx_mock: HTTPXMock, monkeypatch):
    """Call sites keep .text / .json() / .raise_for_status() untouched — that's
    what makes wiring the guard in a one-line change per fetcher."""
    _public(monkeypatch)
    httpx_mock.add_response(json={"hello": "world"})
    resp = await guarded_get("https://example.com/")
    assert resp.json() == {"hello": "world"}
    assert resp.status_code == 200
    resp.raise_for_status()


async def test_an_http_error_status_is_returned_not_raised(
        httpx_mock: HTTPXMock, monkeypatch):
    """A 404 on a public host is not a guard concern — the fetchers already
    turn it into their own message, and losing that would be a regression."""
    _public(monkeypatch)
    httpx_mock.add_response(status_code=404, text="nope")
    resp = await guarded_get("https://example.com/")
    assert resp.status_code == 404
    with pytest.raises(httpx.HTTPStatusError):
        resp.raise_for_status()


async def test_a_gzipped_body_decodes_exactly_once(
        httpx_mock: HTTPXMock, monkeypatch):
    """Mutation guard: carry content-encoding onto the rebuilt response and
    httpx tries to gunzip already-gunzipped bytes — .text explodes on every
    compressed page, which is most of them.

    The body goes in via `stream=` rather than `content=`: pytest_httpx's
    `content=` path feeds the decoder twice and dies inside httpx, which would
    fail this test for a reason that has nothing to do with the guard.
    """
    _public(monkeypatch)
    httpx_mock.add_response(
        stream=httpx.ByteStream(gzip.compress(b"<html>hi</html>")),
        headers={"content-encoding": "gzip", "content-type": "text/html"})
    resp = await guarded_get("https://example.com/")
    assert resp.text == "<html>hi</html>"
    assert "content-encoding" not in resp.headers


async def test_the_final_url_is_the_one_after_redirects(
        httpx_mock: HTTPXMock, monkeypatch):
    """brand_extract resolves relative icon paths against this."""
    _public(monkeypatch)
    httpx_mock.add_response(url="https://example.com/", status_code=302,
                            headers={"location": "https://example.com/en/"})
    httpx_mock.add_response(url="https://example.com/en/", text="ok")
    resp = await guarded_get("https://example.com/")
    assert str(resp.url) == "https://example.com/en/"


# ------------------------------------------------------------------ escape hatch

async def test_allow_private_lets_a_self_hoster_through(
        httpx_mock: HTTPXMock, monkeypatch):
    _resolves_to(monkeypatch, "10.0.0.5")
    httpx_mock.add_response(text="internal wiki")
    resp = await guarded_get("http://wiki.internal:8080/", allow_private=True)
    assert resp.text == "internal wiki"
