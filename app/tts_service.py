from __future__ import annotations

import asyncio
import hashlib
import io
import math
import shutil
import struct
import tempfile
import time
import wave
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiohttp
import edge_tts

SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit PCM

VoiceChoice = Literal["mrt_female", "mrt_male", "offline"]
AudioFormat = Literal["mp3", "wav"]


@dataclass(frozen=True)
class VoiceProfile:
    id: VoiceChoice
    engine: Literal["edge", "offline"]
    voice: str
    rate: str
    pitch: str


VOICE_PROFILES: dict[VoiceChoice, VoiceProfile] = {
    "mrt_female": VoiceProfile(
        id="mrt_female",
        engine="edge",
        voice="id-ID-GadisNeural",
        rate="-8%",
        pitch="+2Hz",
    ),
    "mrt_male": VoiceProfile(
        id="mrt_male",
        engine="edge",
        voice="id-ID-ArdiNeural",
        rate="-5%",
        pitch="+0Hz",
    ),
    "offline": VoiceProfile(
        id="offline",
        engine="offline",
        voice="Damayanti",
        rate="+0%",
        pitch="+0Hz",
    ),
}


class ReusableConnector(aiohttp.TCPConnector):
    """Keep TCP/DNS pool alive across edge_tts calls (edge_tts closes its session)."""

    async def close(self) -> None:
        return None

    async def shutdown(self) -> None:
        await super().close()


class EphemeralAudioStore:
    """Bounded in-memory TTL store for SoundOfText two-step downloads (no disk I/O)."""

    def __init__(self, max_items: int = 100, ttl_seconds: int = 120) -> None:
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[str, tuple[float, bytes]] = OrderedDict()

    def _evict(self, now: float) -> None:
        expired = [
            key
            for key, (created_at, _) in self._items.items()
            if now - created_at > self.ttl_seconds
        ]
        for key in expired:
            self._items.pop(key, None)
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)

    def make_id(self, *, text: str, voice: VoiceChoice, chime: bool) -> str:
        digest = hashlib.sha1(f"{voice}|{chime}|{text}".encode("utf-8")).hexdigest()[
            :12
        ]
        return f"tts_{digest}"

    def get(self, sound_id: str) -> bytes | None:
        now = time.monotonic()
        self._evict(now)
        entry = self._items.get(sound_id)
        if entry is None:
            return None
        self._items.move_to_end(sound_id)
        return entry[1]

    def put(self, sound_id: str, audio_bytes: bytes) -> None:
        now = time.monotonic()
        self._items[sound_id] = (now, audio_bytes)
        self._items.move_to_end(sound_id)
        self._evict(now)


def is_offline_tts_available() -> bool:
    return shutil.which("say") is not None and shutil.which("afconvert") is not None


def _generate_chime_pcm() -> bytes:
    """Synthesize a soft 3-tone queue bell chime (24kHz, 16-bit mono PCM)."""
    notes_hz = [523.25, 659.25, 783.99]
    note_duration = 0.25
    trailing_pause = 0.25
    total_samples = int((len(notes_hz) * note_duration + trailing_pause) * SAMPLE_RATE)
    note_samples = int(note_duration * SAMPLE_RATE)

    buffer = [0.0] * total_samples
    for idx, freq in enumerate(notes_hz):
        start = idx * note_samples
        ring_len = min(int(0.50 * SAMPLE_RATE), total_samples - start)
        for i in range(ring_len):
            t = i / SAMPLE_RATE
            attack = min(1.0, t / 0.012)
            decay = math.exp(-5.5 * t)
            tone = (
                0.70 * math.sin(2.0 * math.pi * freq * t)
                + 0.22 * math.sin(2.0 * math.pi * freq * 2.0 * t)
                + 0.08 * math.sin(2.0 * math.pi * freq * 3.0 * t)
            )
            buffer[start + i] += 0.36 * attack * decay * tone

    pcm = bytearray()
    for sample in buffer:
        clamped = max(-0.98, min(0.98, sample))
        pcm.extend(struct.pack("<h", int(clamped * 32767)))
    return bytes(pcm)


_CHIME_PCM = _generate_chime_pcm()


async def _run_subprocess(cmd: list[str], stdin_bytes: bytes | None = None) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=stdin_bytes)
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Command {' '.join(cmd)} failed: {detail}")
    return stdout


async def _stream_edge_mp3_bytes(
    *,
    text: str,
    profile: VoiceProfile,
    connector: aiohttp.TCPConnector | None,
) -> bytes:
    comm = edge_tts.Communicate(
        text=text,
        voice=profile.voice,
        rate=profile.rate,
        pitch=profile.pitch,
        connector=connector,
    )
    buf = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            buf.extend(chunk["data"])
    return bytes(buf)


def _resolve_target_format(
    profile: VoiceProfile, chime: bool, requested_format: AudioFormat
) -> AudioFormat:
    needs_pcm_pipeline = chime or profile.engine == "offline"
    if not needs_pcm_pipeline:
        return requested_format
    if requested_format == "mp3" and shutil.which("ffmpeg") is not None:
        return "mp3"
    return "wav"


async def _mp3_bytes_to_pcm(mp3_bytes: bytes) -> bytes:
    if shutil.which("ffmpeg") is not None:
        return await _run_subprocess(
            [
                "ffmpeg",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                str(CHANNELS),
                "pipe:1",
            ],
            stdin_bytes=mp3_bytes,
        )
    if shutil.which("afconvert") is not None:
        with tempfile.TemporaryDirectory(prefix="queue_tts_") as tmp:
            tmp_dir = Path(tmp)
            in_mp3 = tmp_dir / "in.mp3"
            out_wav = tmp_dir / "out.wav"
            in_mp3.write_bytes(mp3_bytes)
            await _run_subprocess(
                [
                    "afconvert",
                    "-f",
                    "WAVE",
                    "-d",
                    f"LEI16@{SAMPLE_RATE}",
                    "-c",
                    str(CHANNELS),
                    str(in_mp3),
                    str(out_wav),
                ]
            )
            with wave.open(str(out_wav), "rb") as wf:
                return wf.readframes(wf.getnframes())
    raise RuntimeError("Audio conversion requires `ffmpeg` or macOS `afconvert`.")


async def _offline_text_to_pcm(text: str, voice: str) -> bytes:
    if not is_offline_tts_available():
        raise RuntimeError("Offline macOS TTS (`say` / `afconvert`) is not available.")
    with tempfile.TemporaryDirectory(prefix="queue_tts_") as tmp:
        tmp_dir = Path(tmp)
        aiff_path = tmp_dir / "speech.aiff"
        wav_path = tmp_dir / "speech.wav"
        await _run_subprocess(["say", "-v", voice, "-o", str(aiff_path), text])
        await _run_subprocess(
            [
                "afconvert",
                "-f",
                "WAVE",
                "-d",
                f"LEI16@{SAMPLE_RATE}",
                "-c",
                str(CHANNELS),
                str(aiff_path),
                str(wav_path),
            ]
        )
        with wave.open(str(wav_path), "rb") as wf:
            return wf.readframes(wf.getnframes())


def _pcm_to_wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


async def synthesize_audio_bytes(
    *,
    text: str,
    voice_choice: VoiceChoice,
    chime: bool,
    requested_format: AudioFormat,
    connector: aiohttp.TCPConnector | None = None,
) -> tuple[bytes, AudioFormat]:
    """Synthesize speech purely in memory and return (audio_bytes, actual_format)."""
    profile = VOICE_PROFILES[voice_choice]
    target_format = _resolve_target_format(profile, chime, requested_format)

    # Zero-overhead fast path: stream MP3 directly into RAM
    if profile.engine == "edge" and not chime and target_format == "mp3":
        mp3_bytes = await _stream_edge_mp3_bytes(
            text=text, profile=profile, connector=connector
        )
        return mp3_bytes, "mp3"

    if profile.engine == "offline":
        speech_pcm = await _offline_text_to_pcm(text, profile.voice)
    else:
        mp3_bytes = await _stream_edge_mp3_bytes(
            text=text, profile=profile, connector=connector
        )
        speech_pcm = await _mp3_bytes_to_pcm(mp3_bytes)

    final_pcm = (_CHIME_PCM + speech_pcm) if chime else speech_pcm
    wav_bytes = _pcm_to_wav_bytes(final_pcm)

    if target_format == "mp3":
        mp3_out = await _run_subprocess(
            [
                "ffmpeg",
                "-i",
                "pipe:0",
                "-codec:a",
                "libmp3lame",
                "-qscale:a",
                "2",
                "-f",
                "mp3",
                "pipe:1",
            ],
            stdin_bytes=wav_bytes,
        )
        return mp3_out, "mp3"

    return wav_bytes, "wav"
