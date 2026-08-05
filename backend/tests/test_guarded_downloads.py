"""Downloads whose URL arrived inside a third party's response (phase 1.3).

These are not URLs a user typed. They come out of an OpenRouter completion, a
Pexels search result, a Kling poll, a Canva export job — so the host is chosen
by somebody who is not us, which is the whole criterion in
services/url_guard.py. A provider having a bad day, a response tampered with in
transit, or a prompt that talked a model into naming an address, all end the
same way without a guard: our server fetching our own network.

Every test here follows the same shape, and the shape is the point. It points
DNS into private space AND registers a working response as is_optional, so an
unguarded implementation gets a clean 200 and the test fails on its own
assertion. Without that second half the tests would pass whether or not the
guard were wired, since an unguarded fetch of an invented host fails anyway.
"""
import pytest
from pytest_httpx import HTTPXMock

from services import url_guard
from services.url_guard import BLOCKED_MESSAGE, BlockedURL

CDN = "https://cdn.example.com/asset.bin"


@pytest.fixture(autouse=True)
def _dns_points_into_private_space(monkeypatch):
    monkeypatch.setattr(url_guard, "_resolve", lambda host: ["169.254.169.254"])


def _ok(httpx_mock: HTTPXMock, **kw):
    """The response an unguarded download would happily accept."""
    httpx_mock.add_response(content=b"payload", is_optional=True, **kw)


async def test_openrouter_image_download_is_guarded(httpx_mock: HTTPXMock):
    from services.openrouter import OpenRouterClient, OpenRouterError

    _ok(httpx_mock)
    client = OpenRouterClient(api_key="k")
    with pytest.raises(OpenRouterError) as err:
        await client._download_url(CDN)
    await client.close()
    assert str(err.value) == BLOCKED_MESSAGE


async def test_unsplash_download_is_guarded(httpx_mock: HTTPXMock):
    from services.stock import StockError, UnsplashClient

    httpx_mock.add_response(url="https://api.unsplash.com/photos/p1",
                            json={"urls": {"regular": CDN}})
    _ok(httpx_mock, url=CDN)
    client = UnsplashClient(access_key="k")
    with pytest.raises(StockError) as err:
        await client.download_photo("p1")
    await client.close()
    assert str(err.value) == BLOCKED_MESSAGE


async def test_pexels_download_is_guarded(httpx_mock: HTTPXMock):
    from services.stock import PexelsClient, StockError

    httpx_mock.add_response(url="https://api.pexels.com/v1/photos/p1",
                            json={"src": {"original": CDN}})
    _ok(httpx_mock, url=CDN)
    client = PexelsClient(api_key="k")
    with pytest.raises(StockError) as err:
        await client.download_photo("p1")
    await client.close()
    assert str(err.value) == BLOCKED_MESSAGE


async def test_kling_video_download_is_guarded(httpx_mock: HTTPXMock):
    from services.video.genai.base import VideoGenError
    from services.video.genai.kling import KlingVideoProvider

    _ok(httpx_mock)
    provider = KlingVideoProvider(api_key="k")
    with pytest.raises(VideoGenError) as err:
        await provider.download(CDN)
    await provider.close()
    assert str(err.value) == BLOCKED_MESSAGE


async def test_canva_export_download_is_guarded(httpx_mock: HTTPXMock):
    from services.canva import CanvaClient, CanvaError

    httpx_mock.add_response(url="https://api.canva.com/rest/v1/designs/d1/exports",
                            json={"job": {"id": "j1"}})
    httpx_mock.add_response(url="https://api.canva.com/rest/v1/exports/j1",
                            json={"job": {"status": "success", "urls": [CDN]}})
    _ok(httpx_mock, url=CDN)
    client = CanvaClient(access_token="t")
    with pytest.raises(CanvaError) as err:
        await client.export_design("d1")
    assert str(err.value) == BLOCKED_MESSAGE


async def test_broll_clip_download_is_guarded(httpx_mock: HTTPXMock, tmp_path):
    from services.broll import PexelsVideoSearch

    _ok(httpx_mock)
    dest = tmp_path / "clip.mp4"
    with pytest.raises(BlockedURL):
        await PexelsVideoSearch("k").download(CDN, dest)
    assert not dest.exists(), "a refused download must not leave a file to be muxed"


async def test_broll_streams_rather_than_buffering(monkeypatch, httpx_mock: HTTPXMock,
                                                   tmp_path):
    """A stock clip is tens of megabytes and belongs on disk. Mutation guard:
    swap guarded_stream for guarded_get and the whole clip lands in the RAM of
    a box that is also running ffmpeg — this fails because guarded_get has no
    way to hand back chunks."""
    from services.broll import PexelsVideoSearch

    monkeypatch.setattr(url_guard, "_resolve", lambda host: ["93.184.216.34"])
    chunks = []
    real_stream = url_guard.guarded_stream

    def _spy(*args, **kwargs):
        chunks.append(args[1])
        return real_stream(*args, **kwargs)

    import services.broll as broll_mod
    monkeypatch.setattr(broll_mod, "guarded_stream", _spy)
    httpx_mock.add_response(content=b"video-bytes")

    dest = tmp_path / "clip.mp4"
    await PexelsVideoSearch("k").download(CDN, dest)
    assert dest.read_bytes() == b"video-bytes"
    assert chunks == [CDN]
