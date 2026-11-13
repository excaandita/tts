from __future__ import annotations

import os
import re
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.limits import (
    SlidingWindowLimiter,
    client_key,
    get_cors_origins,
    get_rate_limit_per_minute,
    get_trust_proxy_headers,
)

from app.tts_service import (
    VOICE_PROFILES,
    AudioFormat,
    EphemeralAudioStore,
    ReusableConnector,
    VoiceChoice,
    is_offline_tts_available,
    synthesize_audio_bytes,
)


def _get_default_voice() -> VoiceChoice:
    raw = os.getenv("TTS_DEFAULT_VOICE", "mrt_female").strip()
    if raw in VOICE_PROFILES:
        return raw  # type: ignore[return-value]
    return "mrt_female"


def _get_default_chime() -> bool:
    return os.getenv("TTS_DEFAULT_CHIME", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


audio_store = EphemeralAudioStore(max_items=100, ttl_seconds=120)
rate_limiter = SlidingWindowLimiter(limit=get_rate_limit_per_minute())
RATE_LIMIT_EXEMPT_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    connector = ReusableConnector(limit=20, ttl_dns_cache=300, keepalive_timeout=60)
    app_instance.state.connector = connector
    try:
        yield
    finally:
        await connector.shutdown()
        app_instance.state.connector = None


app = FastAPI(
    title="Queue Monitor Indonesian TTS API",
    version="3.0.0",
    description=(
        "API TTS Bahasa Indonesia gratis, cepat (in-memory tanpa simpan file ke disk), "
        "suara seragam ala MRT Jakarta, dan kompatibel langsung dengan SoundOfText."
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def collapse_double_slashes(request: Request, call_next):
    """Handle double slashes (e.g. `urlDown + '/' + id`) from client URL joins."""
    raw_path = request.scope.get("path", "")
    if "//" in raw_path:
        normalized = re.sub(r"/+", "/", raw_path)
        request.scope["path"] = normalized
        request.scope["raw_path"] = normalized.encode("utf-8")
    return await call_next(request)


@app.middleware("http")
async def enforce_rate_limit(request: Request, call_next):
    """Batasi permintaan per IP agar kredensial TTS tidak dieksploitasi pihak lain."""
    if rate_limiter.enabled and request.scope.get("path") not in RATE_LIMIT_EXEMPT_PATHS:
        forwarded = (
            request.headers.get("x-forwarded-for")
            if get_trust_proxy_headers()
            else None
        )
        key = client_key(
            forwarded, request.client.host if request.client else None
        )
        allowed, retry_after = rate_limiter.check(key)
        if not allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Terlalu banyak permintaan. Coba lagi nanti."},
                headers={
                    "Retry-After": str(int(retry_after)),
                    "Access-Control-Allow-Origin": "*",
                },
            )
    return await call_next(request)


# Didaftarkan setelah middleware di atas agar menjadi lapisan terluar,
# sehingga respons 429 pun tetap membawa header CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Retry-After"],
    max_age=86400,
)


class TTSRequest(BaseModel):
    text: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Kalimat atau nama lengkap yang akan diucapkan.",
    )
    voice: VoiceChoice = Field(default_factory=_get_default_voice)
    chime: bool = Field(default_factory=_get_default_chime)
    format: AudioFormat = Field(default="mp3")

    @field_validator("text")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("`text` tidak boleh kosong.")
        return stripped


class SoundOfTextData(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    voice: str | None = None
    voiceName: str | None = None

    @field_validator("text")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("`data.text` tidak boleh kosong.")
        return stripped


class SoundOfTextRequest(BaseModel):
    engine: str = Field(default="Google")
    data: SoundOfTextData


class SoundOfTextResponse(BaseModel):
    success: bool
    id: str


def _get_connector(request: Request) -> ReusableConnector | None:
    return getattr(request.app.state, "connector", None)


def _serve_ephemeral_mp3(sound_id: str) -> Response:
    if not sound_id or "/" in sound_id or "\\" in sound_id or ".." in sound_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sound id"
        )
    audio_bytes = audio_store.get(sound_id)
    if audio_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sound expired or not found",
        )
    return Response(
        content=audio_bytes,
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'inline; filename="{sound_id}.mp3"'},
    )


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "storage": "in-memory-ephemeral",
        "default_voice": _get_default_voice(),
        "default_chime": _get_default_chime(),
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "offline_available": is_offline_tts_available(),
        "rate_limit_per_minute": rate_limiter.limit,
        "cors_origins": get_cors_origins(),
    }


@app.post("/tts")
async def create_tts_stream(payload: TTSRequest, request: Request) -> Response:
    """Generate audio in memory and return audio bytes directly (no disk storage)."""
    try:
        audio_bytes, actual_format = await synthesize_audio_bytes(
            text=payload.text,
            voice_choice=payload.voice,
            chime=payload.chime,
            requested_format=payload.format,
            connector=_get_connector(request),
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    media_type = "audio/mpeg" if actual_format == "mp3" else "audio/wav"
    return Response(
        content=audio_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="tts.{actual_format}"'},
    )


@app.post("/sounds", response_model=SoundOfTextResponse)
async def soundoftext_generate(
    payload: SoundOfTextRequest, request: Request
) -> SoundOfTextResponse:
    """Drop-in replacement for SoundOfText `POST /sounds` (in-memory only)."""
    voice = _get_default_voice()
    chime = _get_default_chime() and shutil.which("ffmpeg") is not None
    if voice == "offline" and shutil.which("ffmpeg") is None:
        voice = "mrt_female"

    sound_id = audio_store.make_id(text=payload.data.text, voice=voice, chime=chime)
    if audio_store.get(sound_id) is None:
        try:
            mp3_bytes, _ = await synthesize_audio_bytes(
                text=payload.data.text,
                voice_choice=voice,
                chime=chime,
                requested_format="mp3",
                connector=_get_connector(request),
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
            ) from exc
        audio_store.put(sound_id, mp3_bytes)

    return SoundOfTextResponse(success=True, id=sound_id)


@app.get("/sounds/{sound_id}.mp3")
async def soundoftext_download_under_sounds(sound_id: str) -> Response:
    return _serve_ephemeral_mp3(sound_id)


@app.get("/{sound_id}.mp3")
async def soundoftext_download_root(sound_id: str) -> Response:
    return _serve_ephemeral_mp3(sound_id)
