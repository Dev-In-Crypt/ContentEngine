"""Prompt/image -> video generation — a different shape from services/video/base.py's
VideoProvider (slides -> a Ken Burns slideshow, synchronous, no network).

Text/image-to-video providers are asynchronous by nature: you create a task and
the result arrives minutes later, so this protocol has no "generate and return
bytes" method at all — create_task() returns a task id, poll() checks on it,
download() fetches the finished file once poll() says it's ready. The server-side
poller (services/video_poll.py) is what actually drives this loop; nothing here
blocks waiting for a result.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


class VideoGenError(Exception):
    """Any provider failure, normalised. The message is user-facing."""


@dataclass
class GenVideoStatus:
    state: str                        # "processing" | "succeed" | "failed"
    video_url: Optional[str] = None   # set only when state == "succeed"
    error: Optional[str] = None       # set only when state == "failed"


@runtime_checkable
class GenVideoProvider(Protocol):
    async def create_task(
        self, *, prompt: str, model: str, duration_sec: int, aspect_ratio: str,
        image_bytes: Optional[bytes] = None,
    ) -> str:
        """Kick off generation; return the provider's task id.

        `image_bytes` present means image-to-video (animate this frame);
        absent means text-to-video from `prompt` alone.
        """
        ...

    async def poll(self, task_id: str) -> GenVideoStatus:
        ...

    async def download(self, url: str) -> bytes:
        ...

    async def close(self) -> None:
        ...
