"""Publishing a video to X (Phase 8).

Only pre-validation lives here for now — the checks that run in-request,
before the first billed API call, and therefore never depend on which upload
path (see backend/scripts/x_video_spike.py) our OAuth 1.0a credentials turn
out to accept. The chunked uploader and the resumable poller are written once
that recon settles the wire format; this module gets them next.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from services.video.normalize import probe_av

MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_VIDEO_SEC = 140.0
MIN_VIDEO_SEC = 0.5
_ACCEPTED_VIDEO_CODEC = "h264"


class XVideoRejected(Exception):
    """The file itself means X would refuse it — checked before the first
    billed API call, so it's mapped straight to a 4xx and never reaches
    publish_retry's classifier; the "verb: NNN body" message shape used by
    the real X error paths doesn't apply here."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


async def validate_video_for_x(path: Path) -> Optional[str]:
    """Raise XVideoRejected if X would refuse the file outright; otherwise
    return an advisory warning, or None.

    A silent clip (no audio stream) is a WARNING, not a rejection: it's the
    common case out of the Phase 6 editor when neither voiceover nor music
    was chosen, X's tweet_video category accepts a video-only file, and
    rejecting it would block the main workflow rather than an edge case.
    """
    if not path.exists() or path.stat().st_size == 0:
        raise XVideoRejected(
            "The video file is missing on disk. Render it again.", status_code=404)

    size = path.stat().st_size
    if size > MAX_VIDEO_BYTES:
        raise XVideoRejected(
            f"That video is {size / 1024 / 1024:.0f} MB — X accepts up to "
            f"{MAX_VIDEO_BYTES // 1024 // 1024} MB.")

    _w, _h, duration, vcodec, acodec = await asyncio.to_thread(probe_av, path)

    if duration > MAX_VIDEO_SEC:
        raise XVideoRejected(
            f"That clip is {duration:.0f}s — X accepts up to {MAX_VIDEO_SEC:.0f} seconds.")
    if duration < MIN_VIDEO_SEC:
        raise XVideoRejected(
            "That clip is too short for X (it needs to be at least half a second).")
    if vcodec != _ACCEPTED_VIDEO_CODEC:
        raise XVideoRejected(
            f"X needs H.264 video; this file is {vcodec}. Re-render it in the clip editor.")

    if acodec is None:
        return "This clip has no audio — X will publish it silently."
    return None
