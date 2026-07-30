"""Picks a video-gen provider by name.

One branch today (Kling) — the plan is to add Runway/Luma/Veo/an aggregator
here one file at a time as each adapter is actually written, not to pre-build
branches for providers that don't exist yet.
"""
from __future__ import annotations

from services.video.genai.base import GenVideoProvider, VideoGenError


def get_gen_video_provider(name: str, api_key: str, ssl_verify: bool = True) -> GenVideoProvider:
    if name == "kling":
        from services.video.genai.kling import KlingVideoProvider
        return KlingVideoProvider(api_key, ssl_verify=ssl_verify)
    raise VideoGenError(f"Unknown video provider: {name!r}")
