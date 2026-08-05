"""GitHub Releases fetcher — public repos, token optional.

A release is the clearest "something shipped" signal a company emits, so it's the
first source kind. Anonymous calls are capped at 60/hour PER IP: fine for a demo,
but on a shared server that one bucket is spent by every tenant together, so the
poller starts failing as soon as a few sources exist. Setting GITHUB_TOKEN raises
it to 5,000/hour. Still public repos only — the token is a rate-limit lever, not
an access grant.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx

from services.http_utils import describe_rate_limit, describe_request_error
from services.sources.base import (
    FetchedItem, SourceFetchError, SourceRateLimited, parse_iso,
)
from services.url_guard import BlockedURL, guarded_get

_API = "https://api.github.com"
_MAX_BYTES = 5 * 1024 * 1024        # 30 releases with full bodies

#: What GitHub actually allows in an owner or repo name. The segments are
#: interpolated straight into the API path below, and this request carries our
#: GITHUB_TOKEN — so a segment like '..' would aim an authenticated call at an
#: endpoint nobody asked for: '/repos/../user/releases' normalises to '/user',
#: which answers with the token owner's profile. Not SSRF (the host is fixed),
#: but the same class of "somebody else chose part of this URL".
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")


def _is_name(segment: str) -> bool:
    """A real owner/repo name. The charset alone is not enough: '.' and '..'
    match it (a period is legal in 'repo.js') and are exactly the two segments
    that turn a path into a traversal."""
    return bool(_SEGMENT_RE.fullmatch(segment)) and any(c.isalnum() for c in segment)


class GitHubReleasesFetcher:
    def __init__(self, ssl_verify: bool = True, token: Optional[str] = None) -> None:
        self._ssl_verify = ssl_verify
        # Read the app-level setting when the caller didn't pass one. Deliberately
        # not threaded through get_source_fetcher(): the token is server config,
        # not per-request state, and adding a parameter there would break every
        # test double that stubs the factory. Pass token="" to force anonymous.
        if token is None:
            from config import get_settings
            token = get_settings().github_token
        self._token = (token or "").strip()

    @staticmethod
    def _owner_repo(url: str) -> tuple[str, str]:
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) < 2:
            raise SourceFetchError(f"Not a GitHub repository URL: {url!r}")
        owner, repo = parts[0], parts[1]
        if not (_is_name(owner) and _is_name(repo)):
            raise SourceFetchError(f"Not a GitHub repository URL: {url!r}")
        return owner, repo

    async def fetch(self, url: str, since: Optional[datetime] = None) -> list[FetchedItem]:
        owner, repo = self._owner_repo(url)
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "ContentEngine"}
        if self._token:                       # a blank Bearer is worse than none: GitHub 401s
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = await guarded_get(
                f"{_API}/repos/{owner}/{repo}/releases",
                ssl_verify=self._ssl_verify, timeout=20.0, headers=headers,
                params={"per_page": 30}, max_bytes=_MAX_BYTES,
            )
            resp.raise_for_status()
            data = resp.json()
        except BlockedURL as e:
            raise SourceFetchError(str(e)) from e
        except httpx.HTTPStatusError as e:
            limited = describe_rate_limit(e.response)
            if limited:
                hint = ("" if self._token else
                        " Set GITHUB_TOKEN in backend/.env to raise the limit to 5,000/hour.")
                raise SourceRateLimited(f"GitHub: {limited}{hint}") from e
            raise SourceFetchError(
                f"GitHub returned {e.response.status_code} for {owner}/{repo}") from e
        except httpx.RequestError as e:
            raise SourceFetchError(describe_request_error(e, "GitHub")) from e

        items: list[FetchedItem] = []
        for rel in data if isinstance(data, list) else []:
            if not isinstance(rel, dict) or rel.get("draft"):
                continue
            published = parse_iso(rel.get("published_at") or rel.get("created_at"))
            if since and published and published < since:
                continue
            tag = str(rel.get("tag_name") or "").strip()
            items.append(FetchedItem(
                external_id=str(rel.get("id") or tag),
                kind="github_releases",
                title=(str(rel.get("name") or "").strip() or tag or "Release"),
                url=str(rel.get("html_url") or url),
                published_at=published,
                body=str(rel.get("body") or "").strip(),
                raw={"tag_name": tag, "prerelease": bool(rel.get("prerelease"))},
            ))
        return items
