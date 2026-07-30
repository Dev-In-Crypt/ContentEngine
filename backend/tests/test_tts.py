"""ElevenLabs TTS client + ffmpeg audio toolbox (Reels R1).

The HTTP client is tested against a mock transport (no network); the wav/concat
helpers run the real bundled ffmpeg on a generated test tone — same spirit as
test_kenburns, which also encodes for real.
"""
import asyncio
import subprocess
import wave
from pathlib import Path

import httpx
import pytest

from services.tts import (
    ElevenLabsTTS, TTSError, concat_wavs_sync, ffmpeg_exe, loop_music_only_sync,
    mix_with_music_sync, mp3_to_wav_sync,
)


def _client_with(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_synthesize_returns_audio_bytes(monkeypatch):
    def handler(request):
        assert request.headers["xi-api-key"] == "k"
        assert "/text-to-speech/voice1" in str(request.url)
        return httpx.Response(200, content=b"ID3fakemp3")

    transport = _client_with(handler)
    orig = httpx.AsyncClient

    def patched(*a, **kw):
        kw.pop("verify", None)
        return orig(transport=transport, timeout=kw.get("timeout"))
    monkeypatch.setattr(httpx, "AsyncClient", patched)

    out = await ElevenLabsTTS("k").synthesize("hi", voice_id="voice1")
    assert out == b"ID3fakemp3"


@pytest.mark.asyncio
async def test_bad_key_is_actionable(monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"detail": "nope"})
    transport = _client_with(handler)
    orig = httpx.AsyncClient

    def patched(*a, **kw):
        kw.pop("verify", None)
        return orig(transport=transport, timeout=kw.get("timeout"))
    monkeypatch.setattr(httpx, "AsyncClient", patched)

    with pytest.raises(TTSError) as e:
        await ElevenLabsTTS("bad").synthesize("hi", voice_id="v")
    assert "key" in str(e.value).lower()


def test_empty_key_rejected_upfront():
    with pytest.raises(TTSError):
        ElevenLabsTTS("")


def _tone_mp3(path: Path, seconds: float) -> bytes:
    """Generate a real tiny MP3 tone with the bundled ffmpeg."""
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-y",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(path)],
                   capture_output=True, check=True)
    return path.read_bytes()


def test_mp3_to_wav_measures_duration(tmp_path):
    mp3 = _tone_mp3(tmp_path / "tone.mp3", 0.5)
    wav = tmp_path / "tone.wav"
    dur = mp3_to_wav_sync(mp3, wav)
    assert wav.exists()
    assert 0.4 < dur < 0.7            # mp3 padding makes it slightly over 0.5


def test_concat_wavs_builds_track_of_expected_length(tmp_path):
    durs = []
    paths = []
    for i, sec in enumerate((0.3, 0.5)):
        mp3 = _tone_mp3(tmp_path / f"t{i}.mp3", sec)
        wav = tmp_path / f"t{i}.wav"
        durs.append(mp3_to_wav_sync(mp3, wav))
        paths.append(wav)
    out = tmp_path / "voice.m4a"
    total = concat_wavs_sync(paths, out, gap_sec=0.2)
    assert out.exists() and out.stat().st_size > 0
    # reported total = sum(durations + gap); mutation guard: drop the gap → fails
    assert abs(total - (sum(durs) + 0.4)) < 1e-6


def test_concat_empty_raises(tmp_path):
    with pytest.raises(TTSError):
        concat_wavs_sync([], tmp_path / "x.m4a")


def test_mix_with_music_pins_length_to_voice(tmp_path):
    """Music loops forever — the mix MUST end with the voice. Mutation guard:
    drop `duration=first`/`-shortest` → the output outruns the voice and fails."""
    from services.tts import mix_with_music_sync

    voice_mp3 = _tone_mp3(tmp_path / "v.mp3", 0.5)
    voice = tmp_path / "v.wav"
    vdur = mp3_to_wav_sync(voice_mp3, voice)
    music_mp3 = _tone_mp3(tmp_path / "m.mp3", 3.0)      # much longer than voice
    music = tmp_path / "m.wav"
    mp3_to_wav_sync(music_mp3, music)

    out = tmp_path / "mix.m4a"
    mix_with_music_sync(voice, music, out)
    assert out.exists() and out.stat().st_size > 0
    # probe duration via ffmpeg -i stderr
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(out)],
                          capture_output=True, text=True, errors="replace")
    import re
    m = re.search(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", proc.stderr)
    assert m, proc.stderr[-200:]
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    assert dur <= vdur + 0.3        # pinned to voice, not the 3s music


def _mean_volume_db(path: Path, *, ss: float | None = None,
                    t: float | None = None) -> float:
    """mean_volume (dB) of a clip (optionally a [ss, ss+t] window) via ffmpeg."""
    args = [ffmpeg_exe(), "-hide_banner"]
    if ss is not None:
        args += ["-ss", f"{ss}"]
    if t is not None:
        args += ["-t", f"{t}"]
    args += ["-i", str(path), "-af", "volumedetect", "-f", "null", "-"]
    proc = subprocess.run(args, capture_output=True, text=True, errors="replace")
    import re
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stderr)
    assert m, proc.stderr[-300:]
    return float(m.group(1))


def test_mix_keeps_voice_level_with_normalize_off(tmp_path):
    """amix default normalize=1 halves every input (voice buried ~6 dB). With
    normalize=0 the voice keeps its level. Mutation guard: revert to normalize=1
    → the mixed voice drops well below the raw voice and this fails."""
    voice_mp3 = _tone_mp3(tmp_path / "v.mp3", 1.0)
    voice = tmp_path / "v.wav"
    mp3_to_wav_sync(voice_mp3, voice)
    music_mp3 = _tone_mp3(tmp_path / "m.mp3", 3.0)
    music = tmp_path / "m.wav"
    mp3_to_wav_sync(music_mp3, music)

    out = tmp_path / "mix.m4a"
    # music_volume=0 isolates the voice contribution — with normalize=0 it should
    # stay within ~2 dB of the raw voice, not ~6 dB down.
    mix_with_music_sync(voice, music, out, music_volume=0.0)
    assert _mean_volume_db(out) > _mean_volume_db(voice) - 2.0


def test_mix_fades_music_tail_when_total_dur_given(tmp_path):
    """`total_dur` fades the mix out over the final 0.5s so music doesn't cut off
    abruptly. Guard: the last 0.2s is markedly quieter than the middle; without
    total_dur there is no such drop."""
    voice_mp3 = _tone_mp3(tmp_path / "v.mp3", 2.0)
    voice = tmp_path / "v.wav"
    mp3_to_wav_sync(voice_mp3, voice)
    music_mp3 = _tone_mp3(tmp_path / "m.mp3", 5.0)
    music = tmp_path / "m.wav"
    mp3_to_wav_sync(music_mp3, music)

    faded = tmp_path / "faded.m4a"
    mix_with_music_sync(voice, music, faded, total_dur=2.0)
    mid = _mean_volume_db(faded, ss=0.8, t=0.2)
    tail = _mean_volume_db(faded, ss=1.8, t=0.2)
    assert tail < mid - 5.0

    plain = tmp_path / "plain.m4a"
    mix_with_music_sync(voice, music, plain)
    p_mid = _mean_volume_db(plain, ss=0.8, t=0.2)
    p_tail = _mean_volume_db(plain, ss=1.8, t=0.2)
    assert p_tail >= p_mid - 5.0        # no deliberate fade without total_dur


def test_loop_music_only_pins_length_to_total_dur(tmp_path):
    """A 1s track looped to 3s must actually reach ~3s, not stay at 1s.
    Mutation guard: drop -stream_loop/-t → output stays at the source length."""
    music_mp3 = _tone_mp3(tmp_path / "m.mp3", 1.0)
    music = tmp_path / "m.wav"
    mp3_to_wav_sync(music_mp3, music)

    out = tmp_path / "looped.m4a"
    loop_music_only_sync(music, out, 3.0)
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(out)],
                          capture_output=True, text=True, errors="replace")
    import re
    m = re.search(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", proc.stderr)
    assert m, proc.stderr[-200:]
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    assert dur > 2.7


def test_loop_music_only_fades_the_tail(tmp_path):
    music_mp3 = _tone_mp3(tmp_path / "m.mp3", 1.0)
    music = tmp_path / "m.wav"
    mp3_to_wav_sync(music_mp3, music)

    out = tmp_path / "looped.m4a"
    loop_music_only_sync(music, out, 2.0)
    mid = _mean_volume_db(out, ss=0.8, t=0.2)
    tail = _mean_volume_db(out, ss=1.8, t=0.2)
    assert tail < mid - 5.0


def test_async_wrappers_run(tmp_path):
    mp3 = _tone_mp3(tmp_path / "a.mp3", 0.3)
    from services.tts import concat_wavs, mp3_to_wav

    async def _run():
        wav = tmp_path / "a.wav"
        d = await mp3_to_wav(mp3, wav)
        total = await concat_wavs([wav], tmp_path / "a.m4a", gap_sec=0.1)
        return d, total
    d, total = asyncio.run(_run())
    assert abs(total - (d + 0.1)) < 1e-6
    with wave.open(str(tmp_path / "a.wav"), "rb") as w:
        assert w.getnchannels() == 1
