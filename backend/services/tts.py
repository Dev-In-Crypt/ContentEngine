"""ElevenLabs text-to-speech + the small ffmpeg audio toolbox for voiceover Reels.

Deliberately SDK-free: the TTS endpoint is one POST with an xi-api-key header
returning MP3 bytes, so a thin httpx client keeps the dependency graph flat
(matches services/instagram.py style). Durations are measured with the stdlib
`wave` module on ffmpeg-decoded WAVs — the imageio-ffmpeg bundle ships ffmpeg
but NOT ffprobe, so probing is not an option.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import wave
from pathlib import Path

import httpx

_API = "https://api.elevenlabs.io/v1"
_WAV_RATE = 44100


class TTSError(Exception):
    """TTS failed in a way the user can act on (bad key, quota, bad voice)."""


def ffmpeg_exe() -> str:
    """System ffmpeg when present (the Docker image installs one with libass +
    fonts), else the imageio-ffmpeg bundled binary (dev machines)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


class ElevenLabsTTS:
    def __init__(self, api_key: str, *, ssl_verify: bool = True) -> None:
        if not api_key:
            raise TTSError("ElevenLabs API key is not configured")
        self._api_key = api_key
        self._ssl_verify = ssl_verify

    async def synthesize(self, text: str, *, voice_id: str,
                         model_id: str = "eleven_multilingual_v2") -> bytes:
        """Return MP3 bytes for one narration segment."""
        async with httpx.AsyncClient(timeout=60.0, verify=self._ssl_verify) as client:
            resp = await client.post(
                f"{_API}/text-to-speech/{voice_id}",
                headers={"xi-api-key": self._api_key},
                json={"text": text, "model_id": model_id,
                      "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
            )
        if resp.status_code == 401:
            raise TTSError("ElevenLabs rejected the API key — check it in Account.")
        if resp.status_code == 429:
            raise TTSError("ElevenLabs rate/quota limit hit — try again later.")
        if resp.status_code >= 400:
            raise TTSError(f"ElevenLabs error {resp.status_code}: {resp.text[:200]}")
        if not resp.content:
            raise TTSError("ElevenLabs returned empty audio.")
        return resp.content


def _run(args: list[str], what: str) -> None:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", *args],
                          capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise TTSError(f"ffmpeg {what} failed: {proc.stderr[-400:]}")


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        frames, rate = w.getnframes(), w.getframerate()
    return frames / float(rate or _WAV_RATE)


def mp3_to_wav_sync(mp3: bytes, wav_path: Path) -> float:
    """Decode MP3 bytes to a mono WAV on disk; return its exact duration (sec)."""
    tmp_mp3 = wav_path.with_suffix(".mp3")
    tmp_mp3.write_bytes(mp3)
    try:
        _run(["-i", str(tmp_mp3), "-ac", "1", "-ar", str(_WAV_RATE), str(wav_path)],
             "mp3→wav")
    finally:
        tmp_mp3.unlink(missing_ok=True)
    return _wav_duration(wav_path)


def concat_wavs_sync(paths: list[Path], out_m4a: Path, *, gap_sec: float = 0.35) -> float:
    """Concatenate segment WAVs (a short breath of silence after each) into one
    AAC track; return the total duration. The same per-segment `duration + gap`
    must drive the slide lengths so picture and voice stay in lockstep."""
    if not paths:
        raise TTSError("No audio segments to concatenate")
    inputs: list[str] = []
    for p in paths:
        inputs += ["-i", str(p)]
    pads = "".join(
        f"[{i}:a]apad=pad_dur={gap_sec}[p{i}];" for i in range(len(paths)))
    chain = "".join(f"[p{i}]" for i in range(len(paths)))
    filter_complex = f"{pads}{chain}concat=n={len(paths)}:v=0:a=1[out]"
    _run([*inputs, "-filter_complex", filter_complex, "-map", "[out]",
          "-c:a", "aac", "-b:a", "192k", str(out_m4a)], "concat")
    if not out_m4a.exists() or out_m4a.stat().st_size == 0:
        raise TTSError("ffmpeg produced an empty audio track")
    return sum(_wav_duration(p) + gap_sec for p in paths)


def mix_with_music_sync(voice: Path, music: Path, dst: Path, *,
                        music_volume: float = 0.25, duck_threshold: float = 0.05,
                        duck_ratio: float = 8.0,
                        total_dur: float | None = None) -> None:
    """Mix the voiceover (input 0) with the tenant's music track (input 1,
    looped) using sidechain ducking — the proven shorts-pipeline recipe. The mix
    length is pinned to the voice (`duration=first`; `-shortest` as a belt).

    `normalize=0` keeps amix from halving each input's level (the default divides
    by the input count, which buried the voice ~6 dB). When `total_dur` is given,
    the music fades out over the final 0.5s so it doesn't cut off abruptly."""
    fade = ""
    if total_dur is not None and total_dur > 0.5:
        fade = f",afade=t=out:st={total_dur - 0.5:.3f}:d=0.5"
    filter_complex = (
        f"[1:a]aloop=loop=-1:size=2e9,volume={music_volume}[music_raw];"
        f"[music_raw][0:a]sidechaincompress="
        f"threshold={duck_threshold}:ratio={duck_ratio}:"
        f"attack=20:release=300[music_ducked];"
        f"[0:a][music_ducked]amix=inputs=2:duration=first:"
        f"dropout_transition=0:normalize=0[mix_raw];"
        f"[mix_raw]anull{fade}[mix]"
    )
    _run(["-i", str(voice), "-i", str(music),
          "-filter_complex", filter_complex, "-map", "[mix]",
          "-c:a", "aac", "-b:a", "192k", "-shortest", str(dst)],
         "music mix")
    if not dst.exists() or dst.stat().st_size == 0:
        raise TTSError("ffmpeg produced an empty music mix")


async def mix_with_music(voice: Path, music: Path, dst: Path, *,
                         music_volume: float = 0.25, duck_threshold: float = 0.05,
                         duck_ratio: float = 8.0,
                         total_dur: float | None = None) -> None:
    await asyncio.to_thread(mix_with_music_sync, voice, music, dst,
                            music_volume=music_volume,
                            duck_threshold=duck_threshold, duck_ratio=duck_ratio,
                            total_dur=total_dur)


def loop_music_only_sync(music: Path, dst: Path, total_dur: float) -> None:
    """Loop/trim the tenant's track to exactly total_dur with a fade-out at the
    end — for a video that has music but no voice to duck it against."""
    fade = f",afade=t=out:st={total_dur - 0.5:.3f}:d=0.5" if total_dur > 0.5 else ""
    _run(["-stream_loop", "-1", "-i", str(music), "-t", f"{total_dur:.3f}",
          "-af", f"anull{fade}", "-c:a", "aac", "-b:a", "192k", str(dst)],
         "music loop")
    if not dst.exists() or dst.stat().st_size == 0:
        raise TTSError("ffmpeg produced an empty looped music track")


async def loop_music_only(music: Path, dst: Path, total_dur: float) -> None:
    await asyncio.to_thread(loop_music_only_sync, music, dst, total_dur)


async def mp3_to_wav(mp3: bytes, wav_path: Path) -> float:
    return await asyncio.to_thread(mp3_to_wav_sync, mp3, wav_path)


async def concat_wavs(paths: list[Path], out_m4a: Path, *, gap_sec: float = 0.35) -> float:
    return await asyncio.to_thread(concat_wavs_sync, paths, out_m4a, gap_sec=gap_sec)
