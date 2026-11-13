from __future__ import annotations

import io
import wave
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    main_module.audio_store._items.clear()
    with TestClient(app) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["storage"] == "in-memory-ephemeral"


def test_direct_tts_returns_mp3_bytes_without_disk(client: TestClient) -> None:
    resp = client.post(
        "/tts",
        json={"text": "Alex Marquez Muldoko Satyo, silakan menuju loket satu."},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("audio/mpeg")
    assert len(resp.content) > 5000


def test_direct_tts_with_chime_wav(client: TestClient) -> None:
    resp = client.post(
        "/tts",
        json={
            "text": "Nomor antrian A dua belas.",
            "chime": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("audio/wav")
    with wave.open(io.BytesIO(resp.content), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 24000
        assert wf.getnframes() > 24000


def test_offline_voice(client: TestClient) -> None:
    resp = client.post(
        "/tts",
        json={"text": "Nomor antrian satu.", "voice": "offline"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("audio/wav")
    assert len(resp.content) > 5000


def test_soundoftext_dropin_and_ephemeral_memory_cache(client: TestClient) -> None:
    payload = {
        "engine": "Google",
        "data": {
            "text": "Alex Marquez Muldoko Satyo",
            "voice": "id-ID",
            "voiceName": "id-ID-Wavenet-A",
        },
    }
    first = client.post("/sounds", json=payload)
    assert first.status_code == 200, first.text
    first_data = first.json()
    assert first_data["success"] is True
    sound_id = first_data["id"]

    # Second call with same text returns immediately from ephemeral RAM cache
    second = client.post("/sounds", json=payload)
    assert second.status_code == 200
    assert second.json()["id"] == sound_id

    for url in [
        f"/sounds/{sound_id}.mp3",
        f"/sounds//{sound_id}.mp3",
        f"/{sound_id}.mp3",
        f"http://testserver//{sound_id}.mp3",
    ]:
        dl = client.get(url)
        assert dl.status_code == 200, f"Failed for {url}: {dl.status_code}"
        assert dl.headers["content-type"].startswith("audio/mpeg")
        assert len(dl.content) > 5000


def test_validation_and_missing_sound(client: TestClient) -> None:
    blank = client.post("/tts", json={"text": "   "})
    assert blank.status_code == 422

    missing = client.get("/sounds/tidak_ada.mp3")
    assert missing.status_code == 404
