import asyncio

import httpx

from services.url_guard import BlockedURL, guarded_get

#: Same ceiling as a user's own photo upload (api/routes/media.py).
_MAX_EXPORT_BYTES = 20 * 1024 * 1024


class CanvaError(Exception):
    pass


class CanvaClient:
    BASE_URL = "https://api.canva.com/rest/v1"
    OAUTH_TOKEN_URL = "https://api.canva.com/rest/v1/oauth/token"

    def __init__(self, access_token: str):
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    # ------------------------------------------------------------------
    # OAuth helpers (used by the web auth route, not the client itself)
    # ------------------------------------------------------------------

    @classmethod
    async def exchange_code(
        cls,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> dict:
        """Exchange an auth code for access + refresh tokens."""
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                cls.OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise CanvaError(f"Token exchange failed: {e.response.status_code} {e.response.text}") from e
            return resp.json()

    @classmethod
    async def refresh_token(
        cls,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                cls.OAUTH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise CanvaError(f"Token refresh failed: {e.response.status_code}") from e
            return resp.json()

    # ------------------------------------------------------------------
    # Designs
    # ------------------------------------------------------------------

    async def list_designs(self) -> list[dict]:
        resp = await self._get("/designs")
        return resp.json().get("items", [])

    async def get_design(self, design_id: str) -> dict:
        resp = await self._get(f"/designs/{design_id}")
        return resp.json()["design"]

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    async def upload_asset(self, image_bytes: bytes, filename: str) -> str:
        """Upload image bytes as a Canva asset. Returns asset_id."""
        try:
            resp = await self._client.post(
                "/asset-uploads",
                content=image_bytes,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Authorization": self._client.headers["Authorization"],
                },
                params={"name": filename},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise CanvaError(f"Asset upload failed: {e.response.status_code}") from e

        job = resp.json()["job"]
        return await self._poll_asset_job(job["id"])

    async def _poll_asset_job(self, job_id: str, max_retries: int = 20, interval: float = 2.0) -> str:
        for _ in range(max_retries):
            resp = await self._get(f"/asset-uploads/{job_id}")
            job = resp.json()["job"]
            if job["status"] == "success":
                return job["asset"]["id"]
            if job["status"] == "failed":
                raise CanvaError(f"Asset upload job failed: {job}")
            await asyncio.sleep(interval)
        raise TimeoutError(f"Asset upload job {job_id} timed out")

    # ------------------------------------------------------------------
    # Design creation
    # ------------------------------------------------------------------

    async def create_design_from_asset(self, asset_id: str, title: str) -> dict:
        try:
            resp = await self._client.post(
                "/designs",
                json={
                    "type": "type_and_asset",
                    "design_type": {"type": "preset", "name": "instagramPost"},
                    "asset_id": asset_id,
                    "title": title,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise CanvaError(f"Design creation failed: {e.response.status_code}") from e
        return resp.json()["design"]

    # ------------------------------------------------------------------
    # Design Editing API
    # ------------------------------------------------------------------

    async def update_design_elements(self, design_id: str, updates: dict) -> None:
        try:
            resp = await self._client.patch(
                f"/designs/{design_id}/elements",
                json=updates,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise CanvaError(f"Design update failed: {e.response.status_code}") from e

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    async def export_design(self, design_id: str, format: str = "png") -> bytes:
        """Export a design and return image bytes."""
        try:
            resp = await self._client.post(
                f"/designs/{design_id}/exports",
                json={"format": {"type": format}},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise CanvaError(f"Export initiation failed: {e.response.status_code}") from e

        job = resp.json()["job"]
        export_url = await self._poll_export_job(job["id"])

        # export_url is whatever the export job named — Canva's choice, not
        # ours, so it goes through the guard (services/url_guard.py).
        try:
            img = await guarded_get(export_url, timeout=60.0,
                                    max_bytes=_MAX_EXPORT_BYTES)
            img.raise_for_status()
        except BlockedURL as e:
            raise CanvaError(str(e)) from e
        return img.content

    async def _poll_export_job(self, job_id: str, max_retries: int = 30, interval: float = 2.0) -> str:
        for _ in range(max_retries):
            resp = await self._get(f"/exports/{job_id}")
            job = resp.json()["job"]
            if job["status"] == "success":
                return job["urls"][0]
            if job["status"] == "failed":
                raise CanvaError(f"Export job failed: {job}")
            await asyncio.sleep(interval)
        raise TimeoutError(f"Export job {job_id} timed out")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get(self, path: str) -> httpx.Response:
        try:
            resp = await self._client.get(path)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise CanvaError(f"GET {path} failed: {e.response.status_code}") from e
        return resp

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
