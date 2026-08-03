"""One-off recon: which video-upload path our OAuth 1.0a X credentials accept.

PUBLISHES NOTHING — stops once it has a working media_id, never posts a tweet.
Keys come from backend/.env (config.Settings) or OS environment variables.
Keys and the Authorization header are NEVER printed.

Run from backend/:   py scripts/x_video_spike.py

Delete this file once phase 8 lands — it is scaffolding for a one-time decision,
not a permanent part of the app.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from authlib.oauth1 import ClientAuth

from config import get_settings
from services.tts import ffmpeg_exe

V11 = "https://upload.twitter.com/1.1/media/upload.json"
V2 = "https://api.x.com/2/media/upload"
V2_INIT, V2_APPEND, V2_FINAL = f"{V2}/initialize", f"{V2}/append", f"{V2}/finalize"


def _tiny_mp4() -> Path:
    """~30 KB, 1 sec, H.264+AAC 1080x1920 — the same shape the clip editor emits."""
    p = Path(tempfile.mkdtemp()) / "spike.mp4"
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=1:size=1080x1920:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(p)], capture_output=True, check=True)
    return p


def _keys() -> tuple[str, str, str, str]:
    s = get_settings()
    k = (os.getenv("X_API_KEY") or s.x_api_key,
         os.getenv("X_API_SECRET") or s.x_api_secret,
         os.getenv("X_ACCESS_TOKEN") or s.x_access_token,
         os.getenv("X_ACCESS_TOKEN_SECRET") or s.x_access_token_secret)
    if not all(k):
        sys.exit("No X keys found. Set X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / "
                 "X_ACCESS_TOKEN_SECRET in backend/.env or as environment variables.")
    return k


class Probe:
    def __init__(self):
        ck, cs, at, ats = _keys()
        self.signer = ClientAuth(ck, cs, token=at, token_secret=ats)
        self.http = httpx.AsyncClient(timeout=60.0)

    def auth(self, method: str, url: str) -> dict:
        # body=None on purpose: multipart/JSON bodies sit outside the OAuth1
        # signature base string, and a non-empty non-form body makes authlib
        # append oauth_body_hash — an extension X doesn't implement (401).
        _, headers, _ = self.signer.sign(method, url, {}, None)
        return {"Authorization": headers["Authorization"]}

    async def show(self, tag: str, resp: httpx.Response) -> httpx.Response:
        body = (resp.text or "")[:300].replace("\n", " ")
        print(f"  [{tag}] HTTP {resp.status_code}  {body}")
        return resp

    async def variant_a(self, mp4: bytes) -> None:
        """v1.1 upload.twitter.com, params in the QUERY STRING (signed)."""
        print("\nA) v1.1 upload.twitter.com, params in query (signed)")
        q = {"command": "INIT", "total_bytes": len(mp4),
             "media_type": "video/mp4", "media_category": "tweet_video"}
        url = f"{V11}?{urlencode(sorted(q.items()))}"
        r = await self.show("INIT", await self.http.post(url, headers=self.auth("POST", url)))
        if r.status_code >= 300:
            return
        mid = r.json().get("media_id_string")
        q2 = {"command": "APPEND", "media_id": mid, "segment_index": 0}
        u2 = f"{V11}?{urlencode(sorted(q2.items()))}"
        await self.show("APPEND", await self.http.post(
            u2, files={"media": mp4}, headers=self.auth("POST", u2)))
        q3 = {"command": "FINALIZE", "media_id": mid}
        u3 = f"{V11}?{urlencode(sorted(q3.items()))}"
        await self.show("FINALIZE", await self.http.post(u3, headers=self.auth("POST", u3)))
        q4 = {"command": "STATUS", "media_id": mid}
        u4 = f"{V11}?{urlencode(sorted(q4.items()))}"
        await asyncio.sleep(3)
        await self.show("STATUS", await self.http.get(u4, headers=self.auth("GET", u4)))

    async def variant_b(self, mp4: bytes) -> None:
        """v1.1 upload.twitter.com, params in MULTIPART (outside the signature)."""
        print("\nB) v1.1 upload.twitter.com, params in multipart (outside signature)")
        await self.show("INIT", await self.http.post(
            V11, data={"command": "INIT", "total_bytes": str(len(mp4)),
                       "media_type": "video/mp4", "media_category": "tweet_video"},
            headers=self.auth("POST", V11)))

    async def variant_c(self, mp4: bytes) -> None:
        """v2 api.x.com/2/media/upload with command= (the documented OAuth2 path)."""
        print("\nC) v2 api.x.com/2/media/upload, command= in multipart")
        await self.show("INIT", await self.http.post(
            V2, data={"command": "INIT", "total_bytes": str(len(mp4)),
                      "media_type": "video/mp4", "media_category": "tweet_video"},
            headers=self.auth("POST", V2)))

    async def variant_d(self, mp4: bytes) -> None:
        """v2 dedicated /2/media/upload/initialize — forums say OAuth 1.0a fails here."""
        print("\nD) v2 dedicated /2/media/upload/initialize (JSON)")
        await self.show("INIT", await self.http.post(
            V2_INIT, json={"total_bytes": len(mp4), "media_type": "video/mp4",
                           "media_category": "tweet_video"},
            headers=self.auth("POST", V2_INIT)))


async def main() -> None:
    mp4 = _tiny_mp4().read_bytes()
    print(f"Test clip: {len(mp4)} bytes, 1s, H.264+AAC 1080x1920")
    p = Probe()
    for variant in (p.variant_a, p.variant_b, p.variant_c, p.variant_d):
        try:
            await variant(mp4)
        except Exception as e:  # noqa: BLE001 — one failing variant must not kill the rest
            print(f"  [ERROR] {type(e).__name__}: {e}")
    await p.http.aclose()
    print("\nNothing was published. Send the full output above to pick the real path.")


asyncio.run(main())
