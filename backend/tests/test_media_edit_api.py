"""POST /api/media/{asset_id}/edit — trim/reframe/concat library clips into a
new asset, optionally with voiceover, music and a cover (Phase 6).

Real ffmpeg end-to-end (same convention as test_publishing_api.py's voiceover
reel tests): tiny lavfi clips in, real trim/reframe/concat/mux, TTS mocked at
the network boundary (ElevenLabsTTS.synthesize), never at the ffmpeg layer.
"""
import asyncio
import io
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.deps import get_db, get_effective_settings, get_settings
from config import Settings
from main import app
from models.database import Base, MediaAsset
from services import media_store, music_store
from services.tts import ffmpeg_exe


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(media_store, "MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setattr(music_store, "MUSIC_ROOT", tmp_path / "music")
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'media.db'}")

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    SM = async_sessionmaker(eng, expire_on_commit=False)

    async def override_db():
        async with SM() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: Settings(app_mode="cloud")
    app.state.sessionmaker = SM
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_effective_settings, None)
    asyncio.run(eng.dispose())


def _register(client, email):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123", "account_type": "creator"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _clip_bytes(tmp_path, name: str, seconds: float, size: str = "320x180") -> bytes:
    path = tmp_path / name
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi",
                    "-i", f"testsrc=duration={seconds}:size={size}:rate=30",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    str(path)], capture_output=True, check=True)
    return path.read_bytes()


def _upload_video(client, headers, data: bytes, name: str = "clip.mp4") -> str:
    r = client.post("/api/media/uploads", headers=headers,
                    files={"files": (name, io.BytesIO(data), "video/mp4")})
    assert r.status_code == 200
    return r.json()[0]["id"]


def _probe_stderr(path_or_url: str) -> str:
    return subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path_or_url],
                          capture_output=True, text=True, errors="replace").stderr


def _set_elevenlabs_key(monkeypatch, key="k", pexels_key=""):
    app.dependency_overrides[get_effective_settings] = (
        lambda: Settings(app_mode="cloud", elevenlabs_api_key=key,
                         pexels_api_key=pexels_key))


def _fake_tts(monkeypatch, tmp_path):
    from services import tts as tts_mod
    tone_path = tmp_path / "tone.mp3"
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=0.4",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(tone_path)],
                   capture_output=True, check=True)
    tone_bytes = tone_path.read_bytes()

    async def fake_synth(self, text, *, voice_id, model_id="eleven_multilingual_v2"):
        assert voice_id
        return tone_bytes
    monkeypatch.setattr(tts_mod.ElevenLabsTTS, "synthesize", fake_synth)


# ------------------------------------------------------------------ silent edit


def test_edit_single_clip_produces_a_silent_ready_asset(client, tmp_path):
    h = _register(client, "a@ex.com")
    clip_id = _upload_video(client, h, _clip_bytes(tmp_path, "a.mp4", 1.0))

    r = client.post(f"/api/media/{clip_id}/edit", headers=h,
                    json={"clips": [{"asset_id": clip_id, "trim_start_sec": 0.2}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "video"
    assert body["status"] == "ready"
    assert body["source"] == "edited"
    assert body["parent_asset_id"] == clip_id
    assert 0.6 < body["duration_sec"] < 0.95

    served = client.get(body["url"])
    assert served.status_code == 200

    # the actual served file has no audio stream
    out_path = tmp_path / "served.mp4"
    out_path.write_bytes(served.content)
    assert "Audio:" not in _probe_stderr(str(out_path))


def test_edit_concat_two_clips_hard_cut(client, tmp_path):
    h = _register(client, "a@ex.com")
    a = _upload_video(client, h, _clip_bytes(tmp_path, "a.mp4", 1.0))
    b = _upload_video(client, h, _clip_bytes(tmp_path, "b.mp4", 1.0, size="180x320"))

    r = client.post(f"/api/media/{a}/edit", headers=h,
                    json={"clips": [{"asset_id": a}, {"asset_id": b}]})
    assert r.status_code == 200, r.text
    assert 1.7 < r.json()["duration_sec"] < 2.3


def test_edit_concat_with_transitions_preserves_total_duration(client, tmp_path):
    h = _register(client, "a@ex.com")
    a = _upload_video(client, h, _clip_bytes(tmp_path, "a.mp4", 1.0))
    b = _upload_video(client, h, _clip_bytes(tmp_path, "b.mp4", 1.0, size="180x320"))

    r = client.post(f"/api/media/{a}/edit", headers=h,
                    json={"clips": [{"asset_id": a}, {"asset_id": b}], "transitions": True})
    assert r.status_code == 200, r.text
    # sync-preserving crossfade: total duration ~= sum of the two full clips
    assert 1.7 < r.json()["duration_sec"] < 2.3


def test_edit_transitions_duration_honors_each_clips_trim_start(client, tmp_path):
    """Mutation guard: a duration computed from trim_end alone (ignoring
    trim_start) would report ~2.0s here instead of the correct ~1.6s."""
    h = _register(client, "a@ex.com")
    a = _upload_video(client, h, _clip_bytes(tmp_path, "a.mp4", 1.0))
    b = _upload_video(client, h, _clip_bytes(tmp_path, "b.mp4", 1.0, size="180x320"))

    r = client.post(f"/api/media/{a}/edit", headers=h,
                    json={"clips": [{"asset_id": a, "trim_start_sec": 0.4},
                                   {"asset_id": b}],
                          "transitions": True})
    assert r.status_code == 200, r.text
    assert 1.3 < r.json()["duration_sec"] < 1.9


def test_edit_transitions_needs_two_clips_422(client, tmp_path):
    h = _register(client, "a@ex.com")
    a = _upload_video(client, h, _clip_bytes(tmp_path, "a.mp4", 1.0))
    r = client.post(f"/api/media/{a}/edit", headers=h,
                    json={"clips": [{"asset_id": a}], "transitions": True})
    assert r.status_code == 422


# ------------------------------------------------------------------ guard order


def test_edit_rejects_a_foreign_clip_anywhere_in_the_list(client, tmp_path):
    """A foreign id in position 1 (not 0) must still reject the whole request
    before any ffmpeg call runs, and must leave no new asset behind."""
    a = _register(client, "a@ex.com")
    b = _register(client, "b@ex.com")
    mine = _upload_video(client, a, _clip_bytes(tmp_path, "mine.mp4", 1.0))
    theirs = _upload_video(client, b, _clip_bytes(tmp_path, "theirs.mp4", 1.0))

    r = client.post(f"/api/media/{mine}/edit", headers=a,
                    json={"clips": [{"asset_id": mine}, {"asset_id": theirs}]})
    assert r.status_code == 404
    # no new "edited" asset was created — the request was rejected before any
    # ffmpeg call, not partway through
    videos = client.get("/api/media?kind=video", headers=a).json()
    assert [v["id"] for v in videos] == [mine]


def test_edit_url_asset_must_be_owned(client, tmp_path):
    a = _register(client, "a@ex.com")
    b = _register(client, "b@ex.com")
    theirs = _upload_video(client, b, _clip_bytes(tmp_path, "theirs.mp4", 1.0))
    r = client.post(f"/api/media/{theirs}/edit", headers=a,
                    json={"clips": [{"asset_id": theirs}]})
    assert r.status_code == 404


def test_edit_clip_must_be_a_video_asset(client, tmp_path):
    h = _register(client, "a@ex.com")
    img = client.post("/api/media/uploads", headers=h,
                      files={"files": ("p.jpg", io.BytesIO(b"jpeg-bytes"), "image/jpeg")}
                      ).json()[0]["id"]
    r = client.post(f"/api/media/{img}/edit", headers=h,
                    json={"clips": [{"asset_id": img}]})
    assert r.status_code == 400


def test_edit_clip_must_be_ready(client, tmp_path):
    h = _register(client, "a@ex.com")
    pending_id = "55555555-5555-4555-8555-555555555555"

    async def _seed():
        from sqlalchemy import select
        from models.database import User
        async with app.state.sessionmaker() as db:
            user_id = (await db.execute(select(User).where(User.email == "a@ex.com"))
                      ).scalar_one().id
            db.add(MediaAsset(id=pending_id, user_id=user_id, kind="video",
                              source="ai_gen", status="pending"))
            await db.commit()
    asyncio.run(_seed())

    r = client.post(f"/api/media/{pending_id}/edit", headers=h,
                    json={"clips": [{"asset_id": pending_id}]})
    assert r.status_code == 400


# ------------------------------------------------------------------ voiceover / music / cover


def test_edit_with_voiceover_produces_narrated_asset(client, tmp_path, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_elevenlabs_key(monkeypatch)
    clip_id = _upload_video(client, h, _clip_bytes(tmp_path, "a.mp4", 1.0))
    _fake_tts(monkeypatch, tmp_path)

    r = client.post(f"/api/media/{clip_id}/edit", headers=h,
                    json={"clips": [{"asset_id": clip_id}],
                          "voiceover": True, "voiceover_script": "One short line."})
    assert r.status_code == 200, r.text
    body = r.json()
    served = client.get(body["url"])
    out_path = tmp_path / "narrated.mp4"
    out_path.write_bytes(served.content)
    assert "Audio:" in _probe_stderr(str(out_path))


def test_edit_voiceover_needs_a_script(client, tmp_path, monkeypatch):
    h = _register(client, "a@ex.com")
    _set_elevenlabs_key(monkeypatch)
    clip_id = _upload_video(client, h, _clip_bytes(tmp_path, "a.mp4", 1.0))
    r = client.post(f"/api/media/{clip_id}/edit", headers=h,
                    json={"clips": [{"asset_id": clip_id}], "voiceover": True})
    assert r.status_code == 400
    assert "script" in r.json()["detail"].lower()


def test_edit_voiceover_needs_elevenlabs_key(client, tmp_path):
    h = _register(client, "a@ex.com")
    clip_id = _upload_video(client, h, _clip_bytes(tmp_path, "a.mp4", 1.0))
    # explicit empty key — a dev .env may otherwise supply a real one
    app.dependency_overrides[get_effective_settings] = (
        lambda: Settings(app_mode="cloud", elevenlabs_api_key=""))
    r = client.post(f"/api/media/{clip_id}/edit", headers=h,
                    json={"clips": [{"asset_id": clip_id}], "voiceover": True,
                          "voiceover_script": "Hello there."})
    assert r.status_code == 400
    assert "elevenlabs" in r.json()["detail"].lower()


def test_edit_music_needs_an_uploaded_track(client, tmp_path):
    h = _register(client, "a@ex.com")
    clip_id = _upload_video(client, h, _clip_bytes(tmp_path, "a.mp4", 1.0))
    r = client.post(f"/api/media/{clip_id}/edit", headers=h,
                    json={"clips": [{"asset_id": clip_id}], "music": True})
    assert r.status_code == 400
    assert "music" in r.json()["detail"].lower() or "track" in r.json()["detail"].lower()


def test_edit_voiceover_with_music_ducks_via_mix_with_music(client, tmp_path, monkeypatch):
    """With both voiceover and music, the route must duck (mix_with_music), not
    loop_music_only — spy on both so the assertion pins the actual code path,
    not just an observable side effect the wrong path could also produce."""
    import api.routes.media as media_routes

    mix_calls, loop_calls = [], []
    orig_mix, orig_loop = media_routes.mix_with_music, media_routes.loop_music_only

    async def spy_mix(*a, **kw):
        mix_calls.append((a, kw))
        return await orig_mix(*a, **kw)

    async def spy_loop(*a, **kw):
        loop_calls.append((a, kw))
        return await orig_loop(*a, **kw)

    monkeypatch.setattr(media_routes, "mix_with_music", spy_mix)
    monkeypatch.setattr(media_routes, "loop_music_only", spy_loop)

    h = _register(client, "a@ex.com")
    _set_elevenlabs_key(monkeypatch)
    clip_id = _upload_video(client, h, _clip_bytes(tmp_path, "a.mp4", 1.0))
    _fake_tts(monkeypatch, tmp_path)

    tone_path = tmp_path / "music.mp3"
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=220:duration=5.0",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(tone_path)],
                   capture_output=True, check=True)
    up = client.post("/api/settings/music", headers=h,
                     files={"file": ("t.mp3", io.BytesIO(tone_path.read_bytes()), "audio/mpeg")})
    assert up.status_code == 200

    r = client.post(f"/api/media/{clip_id}/edit", headers=h,
                    json={"clips": [{"asset_id": clip_id}], "voiceover": True,
                          "voiceover_script": "One short line.", "music": True})
    assert r.status_code == 200, r.text
    assert len(mix_calls) == 1
    assert len(loop_calls) == 0
    # the 5s music track must NOT stretch the result out past the ~0.4s voice
    assert r.json()["duration_sec"] < 1.0


def test_edit_music_without_voiceover_has_no_sidechain(client, tmp_path, monkeypatch):
    """Music-only must still work — loop_music_only, not mix_with_music (which
    would need a voice track to duck against, and there isn't one)."""
    h = _register(client, "a@ex.com")
    _set_elevenlabs_key(monkeypatch)  # harmless if unused; music doesn't need it
    clip_id = _upload_video(client, h, _clip_bytes(tmp_path, "a.mp4", 1.0))

    tone_path = tmp_path / "music.mp3"
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=220:duration=2.0",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(tone_path)],
                   capture_output=True, check=True)
    up = client.post("/api/settings/music", headers=h,
                     files={"file": ("t.mp3", io.BytesIO(tone_path.read_bytes()), "audio/mpeg")})
    assert up.status_code == 200

    r = client.post(f"/api/media/{clip_id}/edit", headers=h,
                    json={"clips": [{"asset_id": clip_id}], "music": True})
    assert r.status_code == 200, r.text
    served = client.get(r.json()["url"])
    out_path = tmp_path / "musiconly.mp4"
    out_path.write_bytes(served.content)
    assert "Audio:" in _probe_stderr(str(out_path))
