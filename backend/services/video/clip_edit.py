"""Clip editing primitives for the Media Studio editor (Phase 6): arbitrary
trim and a motion-free reframe. Distinct from normalize.py's Ken Burns
pipeline, which is built to ANIMATE a still image — running its synthetic pan
over an already-moving generated clip would fight the clip's own motion. These
two primitives crop/pad without adding any motion at all.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional

from services.tts import ffmpeg_exe
from services.video.base import VideoError
from services.video.normalize import (
    TARGET_FPS, TARGET_H, TARGET_W, aspect_fit_vf, probe_video,
)


def trim_clip_sync(src: Path, dst: Path, *, start_sec: float = 0.0,
                   end_sec: Optional[float] = None) -> None:
    """Cut [start_sec, end_sec) out of src. end_sec=None keeps everything from
    start_sec to the end. Always strips audio (-an) — this is the point where
    a source clip's native sound is muted for the editor."""
    if end_sec is not None and end_sec <= start_sec:
        raise VideoError("end_sec must be greater than start_sec")
    args = [ffmpeg_exe(), "-hide_banner", "-y", "-ss", f"{start_sec:.3f}",
            "-i", str(src)]
    if end_sec is not None:
        args += ["-t", f"{end_sec - start_sec:.3f}"]
    args += ["-an", "-r", str(TARGET_FPS), "-c:v", "libx264", "-preset", "fast",
             "-crf", "20", "-pix_fmt", "yuv420p", str(dst)]
    proc = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise VideoError(f"trim failed: {proc.stderr[-400:]}")
    if not dst.exists() or dst.stat().st_size == 0:
        raise VideoError("trim produced an empty clip")


def reframe_clip_sync(src: Path, dst: Path) -> None:
    """Crop/pad src into TARGET_W x TARGET_H with no motion — for stitching
    clips of heterogeneous aspect ratio (Kling can generate 9:16/16:9/1:1)."""
    src_w, src_h, _dur = probe_video(src)
    vf = aspect_fit_vf(src_w, src_h, TARGET_W, TARGET_H)
    args = [ffmpeg_exe(), "-hide_banner", "-y", "-i", str(src), "-vf", vf,
            "-an", "-r", str(TARGET_FPS), "-c:v", "libx264", "-preset", "fast",
            "-crf", "20", "-pix_fmt", "yuv420p", str(dst)]
    proc = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise VideoError(f"reframe failed: {proc.stderr[-400:]}")
    if not dst.exists() or dst.stat().st_size == 0:
        raise VideoError("reframe produced an empty clip")


def grab_frame(src: Path, at_sec: float = 0.0) -> bytes:
    """One JPEG frame at at_sec — for the editor's default cover, which must be
    taken from the ALREADY-EDITED timeline (trim may change what's at t=0)."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-y", "-ss", f"{at_sec:.3f}", "-i", str(src),
         "-frames:v", "1", "-f", "image2", "-"],
        capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr[-400:].decode("utf-8", "replace")
        raise VideoError(f"grab_frame failed: {err}")
    return proc.stdout


async def trim_clip(src: Path, dst: Path, *, start_sec: float = 0.0,
                    end_sec: Optional[float] = None) -> None:
    await asyncio.to_thread(trim_clip_sync, src, dst,
                            start_sec=start_sec, end_sec=end_sec)


async def reframe_clip(src: Path, dst: Path) -> None:
    await asyncio.to_thread(reframe_clip_sync, src, dst)
