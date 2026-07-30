import io

import pytest
from PIL import Image

from services.video.base import get_video_provider, VideoError
from services.video.kenburns import KenBurnsVideoProvider


def _slide(color="teal") -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (1080, 1350), color).save(b, format="JPEG")
    return b.getvalue()


@pytest.mark.asyncio
async def test_kenburns_produces_vertical_mp4():
    prov = KenBurnsVideoProvider()
    data = await prov.make_reel([_slide("teal"), _slide("orange")],
                                overlays=["First.", "Second."], duration_per=0.5)
    assert isinstance(data, bytes) and len(data) > 0
    # Verify dimensions via imageio reader
    import imageio.v2 as iio
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mktemp(suffix=".mp4"))
    p.write_bytes(data)
    try:
        r = iio.get_reader(str(p))
        frame = r.get_data(0)
        r.close()
        assert frame.shape == (1920, 1080, 3)   # 9:16 vertical
    finally:
        p.unlink()


@pytest.mark.asyncio
async def test_kenburns_per_slide_durations():
    """A durations LIST gives each slide its own length (voiceover sync).
    Mutation guard: ignore the list → frame count reverts to uniform and fails."""
    import imageio.v2 as imageio
    import tempfile
    from pathlib import Path

    mp4 = await KenBurnsVideoProvider().make_reel(
        [_slide("red"), _slide("blue")], duration_per=[0.2, 0.4])
    tmp = Path(tempfile.mkdtemp()) / "reel.mp4"
    tmp.write_bytes(mp4)
    reader = imageio.get_reader(str(tmp))
    n_frames = sum(1 for _ in reader)
    reader.close()
    # 0.2s*30fps + 0.4s*30fps = 6 + 12 = 18
    assert n_frames == 18


def _slide_green_border() -> bytes:
    """A 4:5 slide, white with a 20px green frame on every edge."""
    img = Image.new("RGB", (1080, 1350), "white")
    for y in range(1350):
        for x in range(1080):
            if x < 20 or x >= 1060 or y < 20 or y >= 1330:
                img.putpixel((x, y), (0, 200, 0))
    b = io.BytesIO()
    img.save(b, format="JPEG", quality=95)
    return b.getvalue()


@pytest.mark.asyncio
async def test_kenburns_pad_keeps_full_width():
    """fit="pad" fits the whole 4:5 slide to full width over a blurred backdrop,
    so the slide's left/right edges survive in 9:16 (no side crop). The green
    frame is visible at the left edge. Mutation guard: fall back to cover
    (crop) → the sides are chopped and the edge pixel is no longer green."""
    import imageio.v2 as imageio
    import tempfile
    from pathlib import Path

    mp4 = await KenBurnsVideoProvider().make_reel(
        [_slide_green_border()], duration_per=0.5, fit="pad")
    tmp = Path(tempfile.mkdtemp()) / "pad.mp4"
    tmp.write_bytes(mp4)
    reader = imageio.get_reader(str(tmp))
    frame = reader.get_data(0)          # (1920, 1080, 3)
    reader.close()
    assert frame.shape == (1920, 1080, 3)
    # a couple of px in from the left edge, at vertical centre → the green frame
    r, g, b = frame[960, 3]
    assert g > 140 and r < 120 and b < 120


@pytest.mark.asyncio
async def test_kenburns_cover_crops_sides():
    """The default cover mode DOES crop the sides (kept for the silent reel):
    the same left-edge pixel is NOT the green frame. This pins the behavioural
    difference the pad mode was added to fix."""
    import imageio.v2 as imageio
    import tempfile
    from pathlib import Path

    mp4 = await KenBurnsVideoProvider().make_reel(
        [_slide_green_border()], duration_per=0.5)      # fit defaults to "cover"
    tmp = Path(tempfile.mkdtemp()) / "cover.mp4"
    tmp.write_bytes(mp4)
    reader = imageio.get_reader(str(tmp))
    frame = reader.get_data(0)
    reader.close()
    r, g, b = frame[960, 3]
    assert not (g > 140 and r < 120 and b < 120)


@pytest.mark.asyncio
async def test_kenburns_empty_raises():
    with pytest.raises(VideoError):
        await KenBurnsVideoProvider().make_reel([])


def test_get_provider_ai_is_no_longer_a_recognised_name():
    """"ai" used to select a slideshow-shaped stub for prompt-to-video, which
    was never the right contract (see services/video/genai/ for the real
    thing — a wholly separate system, not a VideoProvider). Removing the stub
    means "ai" is just an unrecognised name now, same as any other typo."""
    with pytest.raises(VideoError, match="Unknown video provider"):
        get_video_provider("ai")


def test_get_provider_unknown():
    with pytest.raises(VideoError):
        get_video_provider("banana")
