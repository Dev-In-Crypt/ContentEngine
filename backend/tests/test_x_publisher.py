"""XPublisher against mocked X endpoints (no live API — needs a paid tier)."""
import re
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
from authlib.oauth1.rfc5849 import signature as oauth_sig
from pytest_httpx import HTTPXMock

from services.publish_retry import is_retryable
from services.publishing.base import PublisherError
from services.publishing.x import MAX_CHARS, MAX_IMAGES, XPublisher

UPLOAD = re.compile(r"https://upload\.twitter\.com/1\.1/media/upload\.json")
METADATA = re.compile(r"https://upload\.twitter\.com/1\.1/media/metadata/create\.json")
TWEET = "https://api.twitter.com/2/tweets"
ME = "https://api.twitter.com/2/users/me"


def _pub() -> XPublisher:
    return XPublisher("ck", "cs", "at", "ats")


async def test_publish_uploads_media_then_tweets(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=UPLOAD, json={"media_id_string": "m1"})
    httpx_mock.add_response(url=TWEET, json={"data": {"id": "tw123", "text": "hi"}})

    pub = _pub()
    out = await pub.publish([b"jpegbytes"], "Run daily.")
    await pub.close()

    assert out.media_id == "tw123"
    assert out.permalink == "https://x.com/i/web/status/tw123"

    reqs = httpx_mock.get_requests()
    # Every request is OAuth1-signed (Authorization: OAuth ...).
    assert all(r.headers.get("Authorization", "").startswith("OAuth ") for r in reqs)
    # The tweet references the uploaded media id.
    import json
    tweet_body = json.loads([r for r in reqs if str(r.url) == TWEET][0].content)
    assert tweet_body["media"]["media_ids"] == ["m1"]
    assert tweet_body["text"] == "Run daily."


async def test_caption_truncated_to_280(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=TWEET, json={"data": {"id": "t", "text": ""}})
    pub = _pub()
    await pub.publish([], "A" * 500)
    await pub.close()

    import json
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert len(body["text"]) == MAX_CHARS


async def test_at_most_four_images(httpx_mock: HTTPXMock):
    for i in range(MAX_IMAGES):
        httpx_mock.add_response(url=UPLOAD, json={"media_id_string": f"m{i}"})
    httpx_mock.add_response(url=TWEET, json={"data": {"id": "t"}})

    pub = _pub()
    await pub.publish([b"a", b"b", b"c", b"d", b"e", b"f"], "cap")  # 6 → only 4 uploaded
    await pub.close()

    uploads = [r for r in httpx_mock.get_requests() if UPLOAD.match(str(r.url))]
    assert len(uploads) == MAX_IMAGES


async def test_tweet_error_raises_publisher_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=UPLOAD, json={"media_id_string": "m1"})
    httpx_mock.add_response(url=TWEET, status_code=403, text="not permitted")

    pub = _pub()
    with pytest.raises(PublisherError, match="X tweet failed"):
        await pub.publish([b"x"], "cap")
    await pub.close()


async def test_missing_tweet_id_raises(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=TWEET, json={"data": {}})
    pub = _pub()
    with pytest.raises(PublisherError, match="missing tweet id"):
        await pub.publish([], "cap")
    await pub.close()


# ── threads: a chain of replies, not N standalone tweets ────────────────────

async def test_publish_thread_chains_replies(httpx_mock: HTTPXMock):
    """Each tweet must reply to the previous one, or X shows three loose posts."""
    import json as _json
    httpx_mock.add_response(url=UPLOAD, json={"media_id_string": "m1"})
    for tid in ("t1", "t2", "t3"):
        httpx_mock.add_response(url=TWEET, json={"data": {"id": tid}})

    pub = _pub()
    out = await pub.publish_thread(["First.", "Second.", "Third."], [b"img"])
    await pub.close()

    tweets = [r for r in httpx_mock.get_requests() if str(r.url) == TWEET]
    bodies = [_json.loads(r.content) for r in tweets]
    assert len(bodies) == 3
    assert "reply" not in bodies[0]                                  # the hook opens the chain
    assert bodies[0]["media"] == {"media_ids": ["m1"]}               # image on the first only
    assert bodies[1]["reply"]["in_reply_to_tweet_id"] == "t1"
    assert bodies[2]["reply"]["in_reply_to_tweet_id"] == "t2"
    assert "media" not in bodies[1] and "media" not in bodies[2]
    assert out.media_id == "t1"                                      # thread identity = first tweet
    assert out.permalink.endswith("t1")


async def test_publish_thread_enforces_the_char_limit(httpx_mock: HTTPXMock):
    import json as _json
    httpx_mock.add_response(url=TWEET, json={"data": {"id": "t1"}})
    pub = _pub()
    await pub.publish_thread(["word " * 200], [])
    await pub.close()
    body = _json.loads(httpx_mock.get_requests()[0].content)
    assert len(body["text"]) <= MAX_CHARS


async def test_publish_thread_reports_partial_failure(httpx_mock: HTTPXMock):
    """X cannot roll back tweets already posted — say what went out."""
    httpx_mock.add_response(url=TWEET, json={"data": {"id": "t1"}})
    httpx_mock.add_response(url=TWEET, json={"data": {"id": "t2"}})
    httpx_mock.add_response(url=TWEET, status_code=403, text="duplicate content")

    pub = _pub()
    with pytest.raises(PublisherError) as err:
        await pub.publish_thread(["One.", "Two.", "Three."], [])
    await pub.close()

    msg = str(err.value)
    assert "2 of 3" in msg               # how many are live
    assert "t1" in msg                   # link to what exists
    assert "403" in msg                  # and why it stopped


async def test_publish_thread_rejects_empty(httpx_mock: HTTPXMock):
    pub = _pub()
    with pytest.raises(PublisherError, match="empty"):
        await pub.publish_thread(["", "   "], [])
    await pub.close()


async def test_long_form_publish_skips_the_cap(httpx_mock: HTTPXMock):
    """X Premium lifts the limit — a long post must not be cut."""
    import json as _json
    httpx_mock.add_response(url=TWEET, json={"data": {"id": "t1"}})
    long_text = "sentence. " * 200
    pub = _pub()
    await pub.publish([], long_text, long_form=True)
    await pub.close()
    body = _json.loads(httpx_mock.get_requests()[0].content)
    assert len(body["text"]) > MAX_CHARS


# ── connection preflight + accessibility ────────────────────────────────────

async def test_verify_credentials_returns_handle(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=ME, json={"data": {"username": "acme", "name": "Acme"}})
    pub = _pub()
    info = await pub.verify_credentials()
    await pub.close()
    assert info["username"] == "acme"
    req = httpx_mock.get_requests()[0]
    assert req.method == "GET"                          # read-only, never posts
    assert req.headers.get("Authorization", "").startswith("OAuth ")


async def test_verify_credentials_bad_key_raises(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=ME, status_code=401, text="Unauthorized")
    pub = _pub()
    with pytest.raises(PublisherError, match="401"):
        await pub.verify_credentials()
    await pub.close()


async def test_alt_text_is_sent_to_media_metadata(httpx_mock: HTTPXMock):
    """alt_text must reach X (v1.1 metadata/create) — it was silently dropped.
    Mutation guard: remove the _set_alt_text call → no metadata request, fails."""
    import json as _json
    httpx_mock.add_response(url=UPLOAD, json={"media_id_string": "m1"})
    httpx_mock.add_response(url=METADATA, json={})
    httpx_mock.add_response(url=TWEET, json={"data": {"id": "t1"}})

    pub = _pub()
    await pub.publish([b"img"], "cap", alt_text="A runner at dawn.")
    await pub.close()

    meta = [r for r in httpx_mock.get_requests() if METADATA.match(str(r.url))]
    assert len(meta) == 1
    body = _json.loads(meta[0].content)
    assert body["media_id"] == "m1"
    assert body["alt_text"]["text"] == "A runner at dawn."


async def test_alt_text_failure_does_not_block_the_tweet(httpx_mock: HTTPXMock):
    """The image is already up — a metadata failure must not lose the post.
    Mutation guard: let _set_alt_text raise → publish blows up, fails."""
    httpx_mock.add_response(url=UPLOAD, json={"media_id_string": "m1"})
    httpx_mock.add_response(url=METADATA, status_code=500, text="boom")
    httpx_mock.add_response(url=TWEET, json={"data": {"id": "t1"}})

    pub = _pub()
    out = await pub.publish([b"img"], "cap", alt_text="desc")
    await pub.close()
    assert out.media_id == "t1"                         # tweet still went out


# ── chunked video upload (Phase 8) ───────────────────────────────────────────

def _oauth_params_from_header(auth_header: str) -> list[tuple[str, str]]:
    """Parse 'OAuth k="v", k="v", ...' back into (key, value) pairs, values
    LEFT percent-encoded — construct_base_string unescapes oauth_ params
    itself, exactly once, so re-decoding here would double-unescape."""
    body = auth_header[len("OAuth "):]
    pairs = []
    for part in body.split(", "):
        k, v = part.split("=", 1)
        pairs.append((k, v.strip('"')))
    return pairs


def _recompute_signature(req, consumer_secret: str, token_secret: str) -> str:
    """Independently recompute the HMAC-SHA1 signature for a captured request,
    using the SAME authlib primitives ClientAuth uses internally — proves the
    query-string params are actually covered by the signature, not just
    present in the URL next to an unrelated one."""
    oauth_params = _oauth_params_from_header(req.headers["Authorization"])
    query_params = parse_qsl(urlsplit(str(req.url)).query)   # already decoded
    base = oauth_sig.construct_base_string(req.method, str(req.url),
                                           oauth_params + query_params)
    return oauth_sig.hmac_sha1_signature(base, consumer_secret, token_secret)


async def test_video_init_params_are_inside_the_oauth_signature(httpx_mock: HTTPXMock):
    """Mutation guard: sign the bare upload URL instead of the full query URL
    (or move `command=` into a form body) → the locally recomputed signature
    no longer matches the header. That mutation is a production 401 on every
    single INIT/APPEND/FINALIZE/STATUS call."""
    httpx_mock.add_response(url=UPLOAD, json={"media_id_string": "vid1"})
    pub = _pub()
    media_id = await pub.video_upload_init(12345, media_type="video/mp4",
                                           media_category="tweet_video")
    await pub.close()

    assert media_id == "vid1"
    req = httpx_mock.get_requests()[0]
    assert "command=INIT" in str(req.url)
    assert "total_bytes=12345" in str(req.url)

    header_sig = dict(_oauth_params_from_header(req.headers["Authorization"]))["oauth_signature"]
    assert unquote(header_sig) == _recompute_signature(req, "cs", "ats")


async def test_video_append_keeps_the_chunk_out_of_the_signature(httpx_mock: HTTPXMock):
    """The chunk rides multipart (outside the OAuth1 base string) — proven two
    ways: the recomputed signature (which never touches the body) still
    matches, and oauth_body_hash (an extension X doesn't implement) is absent.
    Mutation guard: pass the chunk as `body` to .sign() → oauth_body_hash
    appears in the header and a real request 401s."""
    httpx_mock.add_response(url=UPLOAD, status_code=204)
    pub = _pub()
    await pub.video_upload_append("vid1", 2, b"some binary chunk bytes")
    await pub.close()

    req = httpx_mock.get_requests()[0]
    assert "command=APPEND" in str(req.url)
    assert "segment_index=2" in str(req.url)
    assert req.headers["Content-Type"].startswith("multipart/")
    assert "oauth_body_hash" not in req.headers["Authorization"]

    header_sig = dict(_oauth_params_from_header(req.headers["Authorization"]))["oauth_signature"]
    assert unquote(header_sig) == _recompute_signature(req, "cs", "ats")


async def test_video_init_failure_keeps_the_retryable_message_shape(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=UPLOAD, status_code=503, text="Service Unavailable")
    pub = _pub()
    with pytest.raises(PublisherError, match="X media INIT failed") as e:
        await pub.video_upload_init(100)
    await pub.close()
    assert is_retryable(e.value)   # publish_retry must find the "503" in the message


async def test_video_append_failure_raises(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=UPLOAD, status_code=400, text="bad segment")
    pub = _pub()
    with pytest.raises(PublisherError, match="X media APPEND failed"):
        await pub.video_upload_append("vid1", 0, b"chunk")
    await pub.close()


async def test_video_finalize_without_processing_info_is_succeeded(httpx_mock: HTTPXMock):
    """Mutation guard: require processing_info unconditionally → a small clip
    that finalizes instantly would hang in 'processing' forever."""
    httpx_mock.add_response(url=UPLOAD, json={"media_id_string": "vid1", "size": 10})
    pub = _pub()
    status = await pub.video_upload_finalize("vid1")
    await pub.close()
    assert status == {"state": "succeeded", "check_after_secs": None, "error": None}


async def test_video_finalize_reports_pending_processing(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=UPLOAD, json={
        "media_id_string": "vid1",
        "processing_info": {"state": "pending", "check_after_secs": 5},
    })
    pub = _pub()
    status = await pub.video_upload_finalize("vid1")
    await pub.close()
    assert status == {"state": "pending", "check_after_secs": 5, "error": None}


async def test_video_status_reports_succeeded(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=UPLOAD, json={
        "media_id_string": "vid1",
        "processing_info": {"state": "succeeded", "progress_percent": 100},
    })
    pub = _pub()
    status = await pub.video_upload_status("vid1")
    await pub.close()
    assert status["state"] == "succeeded"


async def test_video_status_reports_failed_with_the_reason(httpx_mock: HTTPXMock):
    """Mutation guard: drop the error formatting → the poller (and the user)
    never learns WHY X rejected the video."""
    httpx_mock.add_response(url=UPLOAD, json={
        "media_id_string": "vid1",
        "processing_info": {
            "state": "failed",
            "error": {"code": 1, "name": "InvalidMedia", "message": "Unsupported codec"},
        },
    })
    pub = _pub()
    status = await pub.video_upload_status("vid1")
    await pub.close()
    assert status["state"] == "failed"
    assert "InvalidMedia" in status["error"]
    assert "Unsupported codec" in status["error"]


async def test_publish_video_posts_a_single_tweet_with_the_given_media_id(httpx_mock: HTTPXMock):
    import json as _json
    httpx_mock.add_response(url=TWEET, json={"data": {"id": "tw1"}})
    pub = _pub()
    out = await pub.publish_video("vid1", "Check this out.")
    await pub.close()
    assert out.media_id == "tw1"
    body = _json.loads(httpx_mock.get_requests()[0].content)
    assert body["media"]["media_ids"] == ["vid1"]


async def test_publish_video_thread_puts_the_video_on_the_first_tweet_only(httpx_mock: HTTPXMock):
    """Mutation guard: attach media_ids on every tweet → bodies 1..n carry
    'media' and this fails. Also proves the _post_chain extraction (4.4)
    didn't change publish_thread's own behaviour — see the untouched tests
    above."""
    import json as _json
    for tid in ("t1", "t2"):
        httpx_mock.add_response(url=TWEET, json={"data": {"id": tid}})
    pub = _pub()
    out = await pub.publish_video_thread("vid1", ["Hook.", "Follow-up."])
    await pub.close()

    tweets = [_json.loads(r.content) for r in httpx_mock.get_requests()
             if str(r.url) == TWEET]
    assert tweets[0]["media"] == {"media_ids": ["vid1"]}
    assert "media" not in tweets[1]
    assert tweets[1]["reply"]["in_reply_to_tweet_id"] == "t1"
    assert out.media_id == "t1"
