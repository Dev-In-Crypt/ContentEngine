"""validate_video_for_x — the pre-flight checks that run before any billed X
API call. Real ffmpeg on tiny clips, same convention as test_normalize.py /
test_clip_edit.py: never mock ffmpeg itself.
"""
import subprocess
from pathlib import Path

import pytest

from services.tts import ffmpeg_exe
from services.x_video_publish import MAX_VIDEO_BYTES, MAX_VIDEO_SEC, XVideoRejected, validate_video_for_x


def _clip(path: Path, seconds: float, *, audio: bool = True, vcodec: str = "libx264") -> Path:
    args = [ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={seconds}:size=320x180:rate=30"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-pix_fmt", "yuv420p", "-c:v", vcodec, "-preset", "ultrafast"] \
        if vcodec == "libx264" else ["-pix_fmt", "yuv420p", "-c:v", vcodec]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    args += [str(path)]
    subprocess.run(args, capture_output=True, check=True)
    return path


@pytest.mark.asyncio
async def test_accepts_a_normal_clip_with_no_warning(tmp_path):
    clip = _clip(tmp_path / "ok.mp4", 1.0)
    assert await validate_video_for_x(clip) is None


@pytest.mark.asyncio
async def test_warns_but_does_not_reject_a_silent_clip(tmp_path):
    """Mutation guard: turning this warning into a raise would reject the
    common case the Phase 6 editor produces (neither voiceover nor music)."""
    clip = _clip(tmp_path / "silent.mp4", 1.0, audio=False)
    warning = await validate_video_for_x(clip)
    assert warning is not None
    assert "no audio" in warning.lower()


@pytest.mark.asyncio
async def test_rejects_a_missing_file(tmp_path):
    with pytest.raises(XVideoRejected) as e:
        await validate_video_for_x(tmp_path / "does-not-exist.mp4")
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_rejects_an_empty_file(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(XVideoRejected):
        await validate_video_for_x(empty)


@pytest.mark.asyncio
async def test_rejects_a_clip_over_the_size_cap(tmp_path, monkeypatch):
    """Mutation guard: drop or invert the size comparison and an oversized
    file would sail through to a billed (and doomed) upload."""
    import services.x_video_publish as mod
    monkeypatch.setattr(mod, "MAX_VIDEO_BYTES", 100)   # far below any real clip
    clip = _clip(tmp_path / "big.mp4", 1.0)
    with pytest.raises(XVideoRejected, match="MB"):
        await validate_video_for_x(clip)
    assert MAX_VIDEO_BYTES == 512 * 1024 * 1024        # the real constant is untouched


@pytest.mark.asyncio
async def test_rejects_a_clip_over_the_duration_cap(tmp_path, monkeypatch):
    import services.x_video_publish as mod
    monkeypatch.setattr(mod, "MAX_VIDEO_SEC", 0.3)
    clip = _clip(tmp_path / "long.mp4", 1.0)
    with pytest.raises(XVideoRejected, match="seconds"):
        await validate_video_for_x(clip)
    assert MAX_VIDEO_SEC == 140.0


@pytest.mark.asyncio
async def test_rejects_a_clip_that_is_too_short(tmp_path):
    clip = _clip(tmp_path / "tiny.mp4", 0.1)
    with pytest.raises(XVideoRejected, match="too short"):
        await validate_video_for_x(clip)


@pytest.mark.asyncio
async def test_rejects_a_non_h264_codec(tmp_path):
    clip = _clip(tmp_path / "mpeg4.mp4", 1.0, vcodec="mpeg4")
    with pytest.raises(XVideoRejected, match="H.264"):
        await validate_video_for_x(clip)
