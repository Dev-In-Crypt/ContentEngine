"""Outbound HTTP to a host somebody else chose.

Two kinds of URL reach this app from outside: ones a user typed (a source to
watch, a site to read a brand off) and ones that arrived inside a third party's
response (an image URL from OpenRouter, a clip from Pexels). Neither is ours,
and a plain `httpx.get` on either lets the caller aim our server at our own
private network — the cloud metadata service, the Postgres container, the app's
own loopback port.

**This is a transport, not a validator, and that distinction is the whole
point.** Checking a URL before the request is useless: the attacker's public
host answers `302 Location: http://169.254.169.254/` and httpx, told to follow
redirects, goes there on its own. So the redirect chain is walked here, one hop
at a time, and every hop is re-checked.

Returns a real `httpx.Response` so call sites keep `.text` / `.content` /
`.json()` / `.raise_for_status()` exactly as they were — wiring the guard into
a fetcher is a one-line change to how the client is built, not a rewrite of how
the answer is read.

Not covered: DNS rebinding. Between the check and the connection an attacker
running their own zero-TTL nameserver can flip the answer. Closing it properly
means pinning the connection to the already-validated IP, which for HTTPS
breaks SNI and certificate validation and needs a custom transport. The
practical attacks — metadata, loopback, the docker subnet — are all closed by
checking each hop. Recorded as an accepted residual risk, not as done.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

#: Every rejection carries exactly this, and callers must not add to it.
#: services/fact_check.py puts `str(e)[:200]` straight into an API response and
#: the demo streams error text to an unauthenticated visitor, so a message that
#: told "connection refused" apart from "blocked address" would be a port
#: scanner for our own network with a friendly UI on top.
BLOCKED_MESSAGE = "That address can't be fetched."

MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALLOWED_PORTS = frozenset({80, 443})
_CHUNK = 64 * 1024

#: Rebuilding the response from decoded bytes means these two would lie about
#: it: the body is no longer gzipped and no longer that many bytes.
_DROP_HEADERS = frozenset({"content-encoding", "content-length"})


class BlockedURL(Exception):
    """This URL is not allowed to be fetched.

    `str(e)` is always BLOCKED_MESSAGE. The specific reason lives on `.reason`,
    for our logs only — see the note on BLOCKED_MESSAGE above.
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__(BLOCKED_MESSAGE)
        self.reason = reason


def _resolve(host: str) -> list[str]:
    """Every address `host` answers to. The seam tests monkeypatch."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _allow_private_default() -> bool:
    """Whether a self-hoster opted out of the guard (Settings.allow_private_urls).

    Resolved here rather than threaded through every fetcher: the policy has one
    owner, and adding a parameter to get_source_fetcher() would break the test
    doubles that stub it as `lambda kind, ssl_verify=True: ...`. Fails closed if
    settings can't be read at all.
    """
    try:
        from config import get_settings
        return bool(get_settings().allow_private_urls)
    except Exception:                       # noqa: BLE001 — no config, no exemption
        return False


def _is_forbidden(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return True                     # unparseable — can't vouch for it
    # ::ffff:127.0.0.1 is loopback wearing a v6 hat; judge the v4 inside.
    # ::ffff:127.0.0.1 is loopback wearing a v6 hat — and, less obviously, the
    # whole ::ffff:/96 range reads as is_reserved=True in Python's tables, so
    # WITHOUT this a mapped *public* address (::ffff:8.8.8.8) would be refused
    # too. Judge the v4 inside; getaddrinfo hands back mapped form on some hosts.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def _check(url: str, *, allow_private: bool) -> None:
    """Raise BlockedURL unless this exact URL is safe to request right now."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise BlockedURL(f"scheme {parsed.scheme!r} in {url!r}")
    host = parsed.hostname
    if not host:
        raise BlockedURL(f"no host in {url!r}")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise BlockedURL(f"bad port in {url!r}") from None

    if allow_private:
        return
    if port not in _ALLOWED_PORTS:
        raise BlockedURL(f"port {port} in {url!r}")

    try:
        ipaddress.ip_address(host)
        addresses = [host]              # already a literal — nothing to resolve
    except ValueError:
        try:
            # getaddrinfo blocks; on the event loop it would stall every other
            # request for the length of a DNS timeout.
            addresses = await asyncio.to_thread(_resolve, host)
        except OSError as e:
            raise BlockedURL(f"{host}: resolve failed ({e})") from None

    if not addresses:
        raise BlockedURL(f"{host}: no addresses")
    for addr in addresses:
        if _is_forbidden(addr):
            raise BlockedURL(f"{host} resolves to {addr}")


@asynccontextmanager
async def guarded_stream(
    method: str,
    url: str,
    *,
    ssl_verify: bool = True,
    timeout: float = 20.0,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    allow_private: Optional[bool] = None,
) -> AsyncIterator[httpx.Response]:
    """Walk the redirect chain, checking every hop, and yield the LIVE response
    at the end of it — still unread, so the caller can iterate chunks.

    This is the surface for downloads too big to hold in memory (a stock video
    is tens of megabytes and gets written straight to disk). Callers that just
    want the bytes should use guarded_request, which adds the size cap.

    `allow_private=None` reads Settings.allow_private_urls; pass a bool to
    decide explicitly (which is what the guard's own tests do).
    """
    if allow_private is None:
        allow_private = _allow_private_default()
    async with httpx.AsyncClient(
        timeout=timeout, verify=ssl_verify, follow_redirects=False,
        headers=headers or {},
    ) as client:
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            try:
                await _check(current, allow_private=allow_private)
            except BlockedURL as e:
                log.warning("Blocked outbound request: %s", e.reason)
                raise
            # params belong to the URL we were given; a redirect target carries
            # its own query string and must not have them bolted back on.
            async with client.stream(
                method, current, params=params if hop == 0 else None,
            ) as streamed:
                if streamed.is_redirect:
                    location = streamed.headers.get("location", "")
                    current = str(streamed.url.join(location))
                    continue
                yield streamed
                return

    reason = f"more than {MAX_REDIRECTS} redirects from {url!r}"
    log.warning("Blocked outbound request: %s", reason)
    raise BlockedURL(reason)


async def guarded_request(
    method: str,
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    **kwargs,
) -> httpx.Response:
    """Fetch `url`, checking the address at every redirect hop, and return the
    whole body as an ordinary httpx.Response.

    Raises BlockedURL for anything the guard refuses (and logs why). Ordinary
    HTTP failures are NOT raised: a 404 comes back as a 404 so the callers that
    already word those nicely keep doing so.
    """
    async with guarded_stream(method, url, **kwargs) as streamed:
        body = bytearray()
        async for chunk in streamed.aiter_bytes(chunk_size=_CHUNK):
            body.extend(chunk)
            if len(body) > max_bytes:
                # Stop reading here — a cap that only checks once the whole
                # body has arrived does not cap anything. Counting decoded
                # bytes (not wire bytes) is what makes it a defence against a
                # small payload that decompresses to gigabytes.
                reason = f"body over {max_bytes} bytes from {url!r}"
                log.warning("Blocked outbound request: %s", reason)
                raise BlockedURL(reason)

        safe = httpx.Headers([
            (k, v) for k, v in streamed.headers.multi_items()
            if k.lower() not in _DROP_HEADERS
        ])
        return httpx.Response(
            status_code=streamed.status_code, headers=safe,
            content=bytes(body), request=streamed.request,
        )


async def guarded_get(url: str, **kwargs) -> httpx.Response:
    return await guarded_request("GET", url, **kwargs)
