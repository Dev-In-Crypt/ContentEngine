"""Normalize any stock clip into a 1080x1920 Reel segment with Ken Burns motion.

Ported from the shorts-pipeline step4 recipe: fit the source into a slightly
oversized 1188x2112 buffer, then pan a 1080x1920 crop window across it (direction
rotates per segment for variety), mild unsharp, freeze-pad when the source runs
short. Probing is done by parsing `ffmpeg -i` stderr — the imageio bundle ships
no ffprobe, and ffmpeg alone is enough for width/height/duration.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from services.tts import ffmpeg_exe
from services.video.base import VideoError

TARGET_W, TARGET_H, TARGET_FPS = 1080, 1920, 30
BUFFER_W = int(TARGET_W * 1.10)   # 1188 — room for the pan window
BUFFER_H = int(TARGET_H * 1.10)   # 2112
PAD_FREEZE_MAX_SEC = 2.0
_UNSHARP = "unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount=0.6"

_RE_DIMS = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")
_RE_DUR = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def probe_video(path: Path) -> tuple[int, int, float]:
    """(width, height, duration_sec) parsed from `ffmpeg -i` stderr."""
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    err = proc.stderr or ""
    dims = _RE_DIMS.search(err)
    dur = _RE_DUR.search(err)
    if not dims or not dur:
        raise VideoError(f"Could not probe video {path.name}: {err[-200:]}")
    h, m, s = int(dur.group(1)), int(dur.group(2)), float(dur.group(3))
    return int(dims.group(1)), int(dims.group(2)), h * 3600 + m * 60 + s


def aspect_fit_vf(src_w: int, src_h: int, out_w: int, out_h: int) -> str:
    """Fit any source into out_w x out_h: landscape sources are center-cropped
    to the target aspect then scaled up; portrait/square ones are scaled down
    and padded with black. No motion — shared by normalize.py's buffer-fit and
    clip_edit.py's motion-free reframe."""
    if src_w / src_h > out_w / out_h:
        return f"crop=ih*{out_w}/{out_h}:ih,scale={out_w}:{out_h},setsar=1"
    return (f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")


def _aspect_to_buffer_vf(src_w: int, src_h: int) -> str:
    """Fit any source into BUFFER_W x BUFFER_H."""
    return aspect_fit_vf(src_w, src_h, BUFFER_W, BUFFER_H)


def _motion_vf(duration: float, segment_id: int) -> str:
    """Pan a 1080x1920 window across the buffer; direction rotates by segment."""
    dx, dy = BUFFER_W - TARGET_W, BUFFER_H - TARGET_H
    fwd = f"t/{duration}"
    bwd = f"(1-t/{duration})"
    direction = (segment_id - 1) % 4
    if direction == 0:
        x_expr, y_expr = f"{dx}*{fwd}", f"{dy}*{fwd}"       # ↘
    elif direction == 1:
        x_expr, y_expr = f"{dx}/2", f"{dy}*{fwd}"           # ↓
    elif direction == 2:
        x_expr, y_expr = f"{dx}*{bwd}", f"{dy}*{bwd}"       # ↖
    else:
        x_expr, y_expr = f"{dx}/2", f"{dy}*{bwd}"           # ↑
    return f"crop=w={TARGET_W}:h={TARGET_H}:x='{x_expr}':y='{y_expr}',setsar=1"


def _maybe_pad(vf: str, src_dur: float, target_duration: float) -> str:
    """Freeze the last frame when the source runs short of the narration."""
    if src_dur + 0.05 >= target_duration:
        return vf
    delta = target_duration - src_dur
    if delta > PAD_FREEZE_MAX_SEC:
        # a long freeze looks broken, but a broken-looking segment still beats
        # a crashed reel — log it and let the fallback-to-slide path catch it
        # upstream if the caller cares.
        pass
    return vf + f",tpad=stop_mode=clone:stop_duration={delta:.3f}"


def normalize_clip_sync(src: Path, dst: Path, *, target_duration: float,
                        segment_id: int) -> None:
    """Aspect-fit + Ken Burns pan + sharpen + trim/pad to the segment length."""
    src_w, src_h, src_dur = probe_video(src)
    vf = (f"{_aspect_to_buffer_vf(src_w, src_h)},"
          f"{_motion_vf(target_duration, segment_id)},{_UNSHARP}")
    vf = _maybe_pad(vf, src_dur, target_duration)
    args = [ffmpeg_exe(), "-hide_banner", "-y", "-i", str(src), "-vf", vf,
            "-t", f"{target_duration:.3f}", "-r", str(TARGET_FPS), "-an",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p", str(dst)]
    proc = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise VideoError(f"normalize failed: {proc.stderr[-400:]}")
    if not dst.exists() or dst.stat().st_size == 0:
        raise VideoError("normalize produced an empty clip")


def concat_clips_sync(paths: list[Path], dst: Path) -> None:
    """Concat same-format segments (re-encoded once to kill seam artifacts)."""
    if not paths:
        raise VideoError("No clips to concatenate")
    lst = dst.with_suffix(".txt")
    lst.write_text("\n".join(f"file '{p.as_posix()}'" for p in paths),
                   encoding="utf-8")
    try:
        args = [ffmpeg_exe(), "-hide_banner", "-y", "-f", "concat", "-safe", "0",
                "-i", str(lst), "-r", str(TARGET_FPS), "-an",
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-pix_fmt", "yuv420p", str(dst)]
        proc = subprocess.run(args, capture_output=True, text=True, errors="replace")
        if proc.returncode != 0:
            raise VideoError(f"concat failed: {proc.stderr[-400:]}")
    finally:
        lst.unlink(missing_ok=True)
    if not dst.exists() or dst.stat().st_size == 0:
        raise VideoError("concat produced an empty file")


XFADE_SEC = 0.3


def concat_clips_xfade_sync(paths: list[Path], durations: list[float], dst: Path,
                            *, fade: float = XFADE_SEC) -> None:
    """Crossfade-concat, SYNC-PRESERVING: every clip except the last was rendered
    `fade` seconds LONGER than its narration segment, so each crossfade consumes
    exactly that surplus — offsets are the ORIGINAL cumulative durations and the
    output timeline still matches the voice/subtitles second for second."""
    if not paths:
        raise VideoError("No clips to concatenate")
    if len(paths) != len(durations):
        raise VideoError("clips and durations must match")
    if len(paths) == 1:
        import shutil
        shutil.copyfile(paths[0], dst)
        return
    inputs: list[str] = []
    for p in paths:
        inputs += ["-i", str(p)]
    chain: list[str] = []
    last = "[0:v]"
    accumulated = 0.0
    for i in range(1, len(paths)):
        accumulated += durations[i - 1]
        out_label = f"[v{i:02d}]"
        chain.append(f"{last}[{i}:v]xfade=transition=fade:duration={fade}"
                     f":offset={accumulated:.3f}{out_label}")
        last = out_label
    args = [ffmpeg_exe(), "-hide_banner", "-y", *inputs,
            "-filter_complex", ";".join(chain), "-map", last,
            "-r", str(TARGET_FPS), "-an", "-c:v", "libx264", "-preset", "fast",
            "-crf", "20", "-pix_fmt", "yuv420p", str(dst)]
    proc = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise VideoError(f"xfade concat failed: {proc.stderr[-400:]}")
    if not dst.exists() or dst.stat().st_size == 0:
        raise VideoError("xfade concat produced an empty file")


def align_to_duration_sync(src: Path, dst: Path, target: float) -> None:
    """Belt-and-suspenders after xfade: pad (freeze last frame) or trim so the
    video is exactly as long as the voice track."""
    import shutil
    _w, _h, src_dur = probe_video(src)
    delta = target - src_dur
    if abs(delta) < 0.05:
        shutil.copyfile(src, dst)
        return
    if delta > 0:
        args = ["-i", str(src), "-vf",
                f"tpad=stop_mode=clone:stop_duration={delta:.3f}"]
    else:
        args = ["-i", str(src), "-t", f"{target:.3f}"]
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-y", *args, "-r", str(TARGET_FPS), "-an",
         "-c:v", "libx264", "-preset", "fast", "-crf", "20",
         "-pix_fmt", "yuv420p", str(dst)],
        capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise VideoError(f"align failed: {proc.stderr[-400:]}")


async def normalize_clip(src: Path, dst: Path, *, target_duration: float,
                         segment_id: int) -> None:
    await asyncio.to_thread(normalize_clip_sync, src, dst,
                            target_duration=target_duration, segment_id=segment_id)


async def concat_clips(paths: list[Path], dst: Path) -> None:
    await asyncio.to_thread(concat_clips_sync, paths, dst)


async def concat_clips_xfade(paths: list[Path], durations: list[float], dst: Path,
                             *, fade: float = XFADE_SEC) -> None:
    await asyncio.to_thread(concat_clips_xfade_sync, paths, durations, dst, fade=fade)


async def align_to_duration(src: Path, dst: Path, target: float) -> None:
    await asyncio.to_thread(align_to_duration_sync, src, dst, target)
