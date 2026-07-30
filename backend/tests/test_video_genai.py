"""Kling video-gen adapter: request shape, response parsing, error handling.

Mocked via pytest_httpx.HTTPXMock — the repo's actual convention for outbound
HTTP (respx is in requirements.txt but unused anywhere in the codebase; this
file follows what every other provider adapter already does rather than
starting a second convention for one file).

This is a different contract from services/video/base.py's VideoProvider
(slides -> slideshow, synchronous). Prompt/image -> video is async: create a
task, poll it later, download on success — so nothing here returns bytes
directly except download().
"""
import base64
import json

import pytest
from pytest_httpx import HTTPXMock

from services.video.genai.base import VideoGenError
from services.video.genai.factory import get_gen_video_provider
from services.video.genai.kling import KlingVideoProvider


# ── factory ──────────────────────────────────────────────────────────────────

def test_factory_builds_kling():
    p = get_gen_video_provider("kling", "sk-test")
    assert isinstance(p, KlingVideoProvider)


def test_factory_rejects_unknown_name():
    with pytest.raises(VideoGenError):
        get_gen_video_provider("not-a-real-provider", "sk-test")


# ── create_task: text2video ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_task_text_to_video_hits_the_right_endpoint(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"data": {"task_id": "abc123"}})
    p = KlingVideoProvider(api_key="sk-test")
    task_id = await p.create_task(prompt="a cat on a windowsill", model="kling-v1-6",
                                  duration_sec=5, aspect_ratio="9:16")
    assert task_id
    req = httpx_mock.get_requests()[0]
    assert str(req.url).endswith("/v1/videos/text2video")
    assert req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["prompt"] == "a cat on a windowsill"
    assert body["model_name"] == "kling-v1-6"
    assert body["aspect_ratio"] == "9:16"
    assert body["duration"] == "5"
    await p.close()


@pytest.mark.asyncio
async def test_create_task_image_to_video_hits_the_image_endpoint_and_encodes_bytes(
        httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"data": {"task_id": "abc123"}})
    p = KlingVideoProvider(api_key="sk-test")
    await p.create_task(prompt="make it move", model="kling-v1-6", duration_sec=5,
                        aspect_ratio="9:16", image_bytes=b"fake-jpeg-bytes")
    req = httpx_mock.get_requests()[0]
    assert str(req.url).endswith("/v1/videos/image2video")
    body = json.loads(req.content)
    assert base64.b64decode(body["image"]) == b"fake-jpeg-bytes"
    # An image sets its own frame; aspect_ratio has no meaning once it's supplied.
    assert "aspect_ratio" not in body
    await p.close()


@pytest.mark.asyncio
async def test_create_task_rejects_a_response_with_no_task_id(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"data": {}})
    p = KlingVideoProvider(api_key="sk-test")
    with pytest.raises(VideoGenError):
        await p.create_task(prompt="x", model="kling-v1-6", duration_sec=5,
                            aspect_ratio="9:16")
    await p.close()


@pytest.mark.asyncio
async def test_create_task_http_error_becomes_videogen_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=401, text="invalid key")
    p = KlingVideoProvider(api_key="bad-key")
    with pytest.raises(VideoGenError):
        await p.create_task(prompt="x", model="kling-v1-6", duration_sec=5,
                            aspect_ratio="9:16")
    await p.close()


@pytest.mark.asyncio
async def test_create_task_network_error_becomes_videogen_error(httpx_mock: HTTPXMock):
    import httpx
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    p = KlingVideoProvider(api_key="sk-test")
    with pytest.raises(VideoGenError):
        await p.create_task(prompt="x", model="kling-v1-6", duration_sec=5,
                            aspect_ratio="9:16")
    await p.close()


# ── poll: routed back to the endpoint family the task was created under ────
#
# Kling's task-status routes are namespaced by which creation path made the
# task (text2video/{id} vs image2video/{id}) — querying the wrong family is
# exactly the kind of drift this file exists to absorb in one place, so the
# routing is pinned as its own test per creation path.

@pytest.mark.asyncio
async def test_poll_a_text_task_queries_the_text_endpoint(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"data": {"task_id": "abc123"}})
    p = KlingVideoProvider(api_key="sk-test")
    task_id = await p.create_task(prompt="x", model="kling-v1-6", duration_sec=5,
                                  aspect_ratio="9:16")

    httpx_mock.add_response(json={"data": {"task_status": "processing"}})
    await p.poll(task_id)
    req = httpx_mock.get_requests()[-1]
    assert str(req.url).endswith("/v1/videos/text2video/abc123")
    await p.close()


@pytest.mark.asyncio
async def test_poll_an_image_task_queries_the_image_endpoint(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"data": {"task_id": "xyz789"}})
    p = KlingVideoProvider(api_key="sk-test")
    task_id = await p.create_task(prompt="x", model="kling-v1-6", duration_sec=5,
                                  aspect_ratio="9:16", image_bytes=b"jpeg")

    httpx_mock.add_response(json={"data": {"task_status": "processing"}})
    await p.poll(task_id)
    req = httpx_mock.get_requests()[-1]
    assert str(req.url).endswith("/v1/videos/image2video/xyz789")
    await p.close()


@pytest.mark.asyncio
async def test_poll_with_a_malformed_task_id_is_refused_before_any_request(httpx_mock: HTTPXMock):
    """poll() may run in a different process than create_task() (the scheduler
    poller, not the original request) — the routing kind is encoded in the id
    string itself, so a corrupted or foreign id must be caught here rather than
    silently querying the wrong endpoint or crashing on a partition() result."""
    p = KlingVideoProvider(api_key="sk-test")
    for bad in ("no-colon-here", "unknownkind:abc123", ":abc123", "text2video:"):
        with pytest.raises(VideoGenError):
            await p.poll(bad)
    assert httpx_mock.get_requests() == []
    await p.close()


@pytest.mark.asyncio
async def test_poll_processing_state(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"data": {"task_id": "abc"}})
    p = KlingVideoProvider(api_key="sk-test")
    task_id = await p.create_task(prompt="x", model="m", duration_sec=5, aspect_ratio="9:16")

    httpx_mock.add_response(json={"data": {"task_status": "processing"}})
    status = await p.poll(task_id)
    assert status.state == "processing"
    assert status.video_url is None
    await p.close()


@pytest.mark.asyncio
async def test_poll_succeed_extracts_the_video_url(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"data": {"task_id": "abc"}})
    p = KlingVideoProvider(api_key="sk-test")
    task_id = await p.create_task(prompt="x", model="m", duration_sec=5, aspect_ratio="9:16")

    httpx_mock.add_response(json={"data": {
        "task_status": "succeed",
        "task_result": {"videos": [{"url": "https://cdn.klingai.com/out.mp4"}]},
    }})
    status = await p.poll(task_id)
    assert status.state == "succeed"
    assert status.video_url == "https://cdn.klingai.com/out.mp4"
    await p.close()


@pytest.mark.asyncio
async def test_poll_failed_carries_the_message(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"data": {"task_id": "abc"}})
    p = KlingVideoProvider(api_key="sk-test")
    task_id = await p.create_task(prompt="x", model="m", duration_sec=5, aspect_ratio="9:16")

    httpx_mock.add_response(json={"data": {
        "task_status": "failed", "task_status_msg": "prompt violates content policy",
    }})
    status = await p.poll(task_id)
    assert status.state == "failed"
    assert status.error == "prompt violates content policy"
    await p.close()


@pytest.mark.asyncio
async def test_poll_succeed_with_no_video_url_is_reported_as_failed(httpx_mock: HTTPXMock):
    """"succeed" with nothing to download from is not a state the rest of the
    system can act on — call it failed with an honest message instead of
    crashing the poller trying to download None."""
    httpx_mock.add_response(json={"data": {"task_id": "abc"}})
    p = KlingVideoProvider(api_key="sk-test")
    task_id = await p.create_task(prompt="x", model="m", duration_sec=5, aspect_ratio="9:16")

    httpx_mock.add_response(
        json={"data": {"task_status": "succeed", "task_result": {"videos": []}}})
    status = await p.poll(task_id)
    assert status.state == "failed"
    assert status.error
    await p.close()


@pytest.mark.asyncio
async def test_poll_http_error_becomes_videogen_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"data": {"task_id": "abc"}})
    p = KlingVideoProvider(api_key="sk-test")
    task_id = await p.create_task(prompt="x", model="m", duration_sec=5, aspect_ratio="9:16")

    httpx_mock.add_response(status_code=500, text="server error")
    with pytest.raises(VideoGenError):
        await p.poll(task_id)
    await p.close()


# ── download ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_download_returns_the_bytes(httpx_mock: HTTPXMock):
    httpx_mock.add_response(content=b"mp4-bytes-here")
    p = KlingVideoProvider(api_key="sk-test")
    data = await p.download("https://cdn.klingai.com/out.mp4")
    assert data == b"mp4-bytes-here"
    await p.close()
