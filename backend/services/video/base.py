"""Video (Reel) generation provider abstraction.

One implementation: KenBurnsVideoProvider — a local ffmpeg slideshow built
from a post's own slides (the only Reel-render backend that exists).

This is a different system from services/video/genai/: that package is
prompt/image-to-video generation for the standalone Video tab (Kling and,
later, other providers), which never fits this Protocol's shape — make_reel()
takes a post's rendered slide images, not a text prompt. A stub used to sit
here under the name "ai" pretending to bridge the two; it never did, and has
been removed rather than left to confuse the next person who assumes it's
where text-to-video lives.
"""

from __future__ import annotations

from typing import Optional, Protocol


class VideoError(Exception):
    pass


class VideoProvider(Protocol):
    async def make_reel(
        self,
        slides: list[bytes],
        overlays: Optional[list[str]] = None,
        duration_per: float | list[float] = 3.0,
        audio_path: Optional[str] = None,
    ) -> bytes:
        """Return H.264 MP4 bytes sized 1080x1920 (9:16 Reels). A list for
        duration_per gives each slide its own length (voiceover sync)."""
        ...


def get_video_provider(name: str = "kenburns") -> VideoProvider:
    name = (name or "kenburns").lower()
    if name == "kenburns":
        from services.video.kenburns import KenBurnsVideoProvider
        return KenBurnsVideoProvider()
    raise VideoError(f"Unknown video provider: {name!r}")
