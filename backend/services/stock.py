import httpx
import logging
import random
from dataclasses import dataclass
from typing import Optional

from services.http_utils import describe_request_error
from services.url_guard import BlockedURL, guarded_get

#: The CDN link comes out of a search response, not from us — guarded for the
#: same reason as everything else in services/url_guard.py's docstring. Same
#: ceiling as a user's own photo upload (api/routes/media.py).
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

log = logging.getLogger(__name__)


@dataclass
class StockPhotoResult:
    id: str
    url: str          # full-size download URL
    thumb_url: str
    alt: Optional[str]
    source: str       # "unsplash" or "pexels"
    author_name: Optional[str] = None
    author_profile_url: Optional[str] = None
    source_link: Optional[str] = None   # link to the photo page on the source site

    def as_attribution(self) -> dict:
        """Serializable attribution dict for the UI."""
        return {
            "source": self.source,
            "author_name": self.author_name,
            "author_profile_url": self.author_profile_url,
            "source_link": self.source_link,
        }


class StockError(Exception):
    pass


class UnsplashClient:
    BASE_URL = "https://api.unsplash.com"

    def __init__(self, access_key: str, ssl_verify: bool = True):
        self.access_key = access_key
        self._ssl_verify = ssl_verify
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={"Authorization": f"Client-ID {self.access_key}"},
                timeout=30.0,
                verify=self._ssl_verify,
            )
        return self._client

    async def search_photos(
        self, query: str, per_page: int = 5, orientation: str = "squarish"
    ) -> list[StockPhotoResult]:
        client = self._get_client()
        try:
            response = await client.get(
                "/search/photos",
                params={"query": query, "per_page": per_page, "orientation": orientation},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise StockError(f"Unsplash search failed: {e.response.status_code}") from e

        data = response.json()
        results = []
        for item in data.get("results", []):
            user = item.get("user") or {}
            results.append(StockPhotoResult(
                id=item["id"],
                url=item["urls"]["regular"],
                thumb_url=item["urls"]["thumb"],
                alt=item.get("alt_description"),
                source="unsplash",
                author_name=user.get("name"),
                author_profile_url=(user.get("links") or {}).get("html"),
                source_link=(item.get("links") or {}).get("html"),
            ))
        if not results:
            log.warning("Unsplash returned 0 results for query=%r (total=%s)",
                        query, data.get("total", "?"))
        return results

    async def download_photo(self, photo_id: str, size: str = "regular") -> bytes:
        client = self._get_client()
        # Fetch photo metadata to get download URL
        try:
            resp = await client.get(f"/photos/{photo_id}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise StockError(f"Unsplash photo fetch failed: {e.response.status_code}") from e

        download_url = resp.json()["urls"][size]
        try:
            img = await guarded_get(download_url, ssl_verify=self._ssl_verify,
                                    timeout=60.0, max_bytes=_MAX_IMAGE_BYTES)
            img.raise_for_status()
        except BlockedURL as e:
            raise StockError(str(e)) from e
        except httpx.RequestError as e:
            raise StockError(describe_request_error(e, "Unsplash download")) from e
        return img.content

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


class PexelsClient:
    BASE_URL = "https://api.pexels.com/v1"

    def __init__(self, api_key: str, ssl_verify: bool = True):
        self.api_key = api_key
        self._ssl_verify = ssl_verify
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={"Authorization": self.api_key},
                timeout=30.0,
                verify=self._ssl_verify,
            )
        return self._client

    async def search_photos(self, query: str, per_page: int = 5) -> list[StockPhotoResult]:
        client = self._get_client()
        try:
            response = await client.get(
                "/search",
                params={"query": query, "per_page": per_page, "size": "medium"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise StockError(f"Pexels search failed: {e.response.status_code}") from e

        results = []
        for item in response.json().get("photos", []):
            results.append(StockPhotoResult(
                id=str(item["id"]),
                url=item["src"]["large"],
                thumb_url=item["src"]["medium"],
                alt=item.get("alt"),
                source="pexels",
                author_name=item.get("photographer"),
                author_profile_url=item.get("photographer_url"),
                source_link=item.get("url"),
            ))
        return results

    async def download_photo(self, photo_id: str) -> bytes:
        client = self._get_client()
        try:
            resp = await client.get(f"/photos/{photo_id}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise StockError(f"Pexels photo fetch failed: {e.response.status_code}") from e

        download_url = resp.json()["src"]["original"]
        try:
            img = await guarded_get(download_url, ssl_verify=self._ssl_verify,
                                    timeout=60.0, max_bytes=_MAX_IMAGE_BYTES)
            img.raise_for_status()
        except BlockedURL as e:
            raise StockError(str(e)) from e
        except httpx.RequestError as e:
            raise StockError(describe_request_error(e, "Pexels download")) from e
        return img.content

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


class StockClient:
    """Facade that searches both Unsplash and Pexels and falls back gracefully."""

    def __init__(
        self,
        unsplash: Optional[UnsplashClient] = None,
        pexels: Optional[PexelsClient] = None,
    ):
        self.unsplash = unsplash
        self.pexels = pexels

    async def search(self, query: str, per_page: int = 5, source: str = "unsplash") -> list[StockPhotoResult]:
        if source == "unsplash" and self.unsplash:
            return await self.unsplash.search_photos(query, per_page=per_page)
        if source == "pexels" and self.pexels:
            return await self.pexels.search_photos(query, per_page=per_page)
        raise StockError(f"Stock source '{source}' not configured")

    async def search_and_download(
        self, query: str, orientation: str = "squarish",
        size: str = "regular", source: str = "auto",
    ) -> tuple[bytes, StockPhotoResult]:
        """
        source="auto"    → Unsplash first, Pexels as fallback
        source="unsplash"→ Unsplash only
        source="pexels"  → Pexels only
        Fetches top-5 results and picks one at random for variety.
        Returns (image_bytes, picked_StockPhotoResult) so callers can record
        author/source attribution required by Unsplash/Pexels licensing.
        """
        # ── Unsplash ──────────────────────────────────────────────────────
        if source in ("auto", "unsplash") and self.unsplash:
            try:
                results = await self.unsplash.search_photos(
                    query, per_page=5, orientation=orientation
                )
                if results:
                    pick = random.choice(results)
                    log.info("Unsplash: picked '%s' (1 of %d) for query=%r",
                             pick.id, len(results), query)
                    data = await self.unsplash.download_photo(pick.id, size=size)
                    return data, pick
                log.warning("Unsplash: 0 results for query=%r", query)
            except StockError as e:
                log.warning("Unsplash failed (%s), trying Pexels…", e)

        # ── Pexels ────────────────────────────────────────────────────────
        if source in ("auto", "pexels") and self.pexels:
            try:
                results = await self.pexels.search_photos(query, per_page=5)
                if results:
                    pick = random.choice(results)
                    log.info("Pexels: picked '%s' (1 of %d) for query=%r",
                             pick.id, len(results), query)
                    data = await self.pexels.download_photo(pick.id)
                    return data, pick
                log.warning("Pexels: 0 results for query=%r", query)
            except StockError as e:
                log.warning("Pexels failed: %s", e)

        raise StockError(f"No results from any stock source for query={query!r}")

    async def close(self) -> None:
        if self.unsplash:
            await self.unsplash.close()
        if self.pexels:
            await self.pexels.close()
