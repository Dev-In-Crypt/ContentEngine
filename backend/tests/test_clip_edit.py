"""Clip editing primitives (Phase 6) — real ffmpeg on tiny lavfi clips, same
convention as test_normalize.py: never mock ffmpeg itself.
"""
import subprocess
from pathlib import Path

import pytest

from services.tts import ffmpeg_exe
from services.video.base import VideoError
from services.video.clip_edit import grab_frame, reframe_clip_sync, trim_clip_sync
from services.video.normalize import probe_video


def _clip(path: Path, seconds: float, size: str) -> Path:
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi",
                    "-i", f"testsrc=duration={seconds}:size={size}:rate=30",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset",
                    "ultrafast", str(path)], capture_output=True, check=True)
    return path


def _half_red_half_blue(path: Path, seconds_each: float = 1.0,
                        size: str = "64x64") -> Path:
    subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-y",
         "-f", "lavfi", "-i", f"color=c=red:s={size}:d={seconds_each}:r=30",
         "-f", "lavfi", "-i", f"color=c=blue:s={size}:d={seconds_each}:r=30",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[out]",
         "-map", "[out]", "-pix_fmt", "yuv420p", "-c:v", "libx264",
         "-preset", "ultrafast", str(path)],
        capture_output=True, check=True)
    return path


def _static_drawbox_clip(path: Path, seconds: float, size: str) -> Path:
    """A fixed lime box on black — content is identical in every frame, so any
    reframe pixel check that varies with time would reveal a time-dependent
    crop window (i.e. a pan) leaking in from normalize.py's Ken Burns."""
    subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi",
         "-i", f"color=c=black:s={size}:d={seconds}:r=30",
         "-vf", "drawbox=x=0:y=0:w=20:h=20:color=lime:t=fill",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
         str(path)], capture_output=True, check=True)
    return path


def _frame_at(path: Path, at_sec: float):
    import imageio.v2 as iio
    r = iio.get_reader(str(path))
    fps = r.get_meta_data().get("fps", 30)
    frame = r.get_data(int(at_sec * fps))
    r.close()
    return frame


def test_trim_start_cuts_from_the_correct_point(tmp_path):
    """Mutation guard: trim always cutting from zero would leave the start
    frame red instead of blue."""
    src = _half_red_half_blue(tmp_path / "src.mp4")
    dst = tmp_path / "out.mp4"
    trim_clip_sync(src, dst, start_sec=1.0)
    frame = _frame_at(dst, 0.05)
    r, g, b = frame[32, 32]
    assert b > 100 and r < 100


def test_trim_end_none_keeps_everything_after_start(tmp_path):
    src = _half_red_half_blue(tmp_path / "src.mp4")
    dst = tmp_path / "out.mp4"
    trim_clip_sync(src, dst, start_sec=1.0)
    _w, _h, dur = probe_video(dst)
    assert 0.85 < dur < 1.15


def test_trim_end_before_start_raises(tmp_path):
    """Mutation guard: without the explicit start<end check, ffmpeg would still
    fail on the negative -t but with an opaque ffmpeg-stderr message instead of
    this one — pin the message so removing the guard is caught."""
    src = _half_red_half_blue(tmp_path / "src.mp4")
    with pytest.raises(VideoError, match="end_sec must be greater than start_sec"):
        trim_clip_sync(src, tmp_path / "out.mp4", start_sec=1.0, end_sec=0.5)


def test_reframe_gives_target_dimensions_landscape(tmp_path):
    src = _clip(tmp_path / "land.mp4", 0.5, "320x180")
    dst = tmp_path / "out.mp4"
    reframe_clip_sync(src, dst)
    assert probe_video(dst)[:2] == (1080, 1920)


def test_reframe_gives_target_dimensions_portrait(tmp_path):
    src = _clip(tmp_path / "port.mp4", 0.5, "180x320")
    dst = tmp_path / "out.mp4"
    reframe_clip_sync(src, dst)
    assert probe_video(dst)[:2] == (1080, 1920)


def test_reframe_has_no_pan(tmp_path):
    """Mutation guard: a time-varying crop window (accidentally reusing
    normalize.py's Ken Burns pan) would move the fixed box off the sampled
    pixel by 90% of the duration."""
    src = _static_drawbox_clip(tmp_path / "box.mp4", 1.0, "180x320")
    dst = tmp_path / "out.mp4"
    reframe_clip_sync(src, dst)
    early = _frame_at(dst, 0.1)
    late = _frame_at(dst, 0.9)
    er, eg, eb = early[10, 10]
    lr, lg, lb = late[10, 10]
    assert (int(er), int(eg), int(eb)) == (int(lr), int(lg), int(lb))
    assert eg > 140 and er < 120


def test_grab_frame_returns_valid_jpeg(tmp_path):
    src = _clip(tmp_path / "src.mp4", 1.0, "320x180")
    data = grab_frame(src, at_sec=0.0)
    assert data[:2] == b"\xff\xd8"


def test_grab_frame_respects_at_sec(tmp_path):
    """Mutation guard: ignoring at_sec would always grab frame 0."""
    src = _clip(tmp_path / "src.mp4", 1.0, "320x180")
    frame0 = grab_frame(src, at_sec=0.0)
    frame9 = grab_frame(src, at_sec=0.9)
    assert frame0 != frame9
