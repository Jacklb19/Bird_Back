"""Validate uploaded audio bytes instead of trusting client paths or MIME headers."""
import struct
from functools import partial
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from birdnet_api.storage import AudioStorage


def wav():
    return b"RIFF" + struct.pack("<I", 288036) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 48000, 96000, 2, 16) + b"data" + struct.pack("<I", 288000) + bytes(288000)


@pytest.mark.parametrize("content", [b"invalid", wav() + b"extra", wav()[:44], wav()[:24] + struct.pack("<I", 44100) + wav()[28:], wav()[:4] + struct.pack("<I", 0) + wav()[8:]], ids=["invalid", "oversize", "truncated", "sample-rate", "riff-size"])
def test_rejects_invalid_audio(monkeypatch, content):
    audio = AudioStorage()
    monkeypatch.setattr(audio, "client", lambda: httpx.Client(base_url="https://storage.invalid", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))))
    owner, detection = uuid4(), uuid4()
    with pytest.raises(HTTPException) as error:
        audio.verify(owner, detection, audio.path(owner, detection))
    assert error.value.status_code == 422


def test_signs_only_storage_relative_upload_urls(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co/")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
    monkeypatch.setenv("SUPABASE_AUDIO_BUCKET", "audio")
    owner, detection = uuid4(), uuid4()
    path = f"{owner}/{detection}.wav"
    replies = iter([f"/object/upload/sign/audio/{path}?token=t", "https://foreign.example/upload"])
    requests = []

    def serve(request):
        requests.append((request.url.path, request.headers["x-upsert"]))
        return httpx.Response(200, json={"url": next(replies)})

    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(serve)))
    audio = AudioStorage()
    signed = audio.sign(owner, detection)
    assert (signed.upload_url, signed.audio_path) == (f"https://project.supabase.co/storage/v1/object/upload/sign/audio/{path}?token=t", path)
    with pytest.raises(HTTPException) as error:
        audio.sign(owner, detection)
    assert error.value.status_code == 502
    assert requests == [(f"/storage/v1/object/upload/sign/audio/{path}", "true")] * 2


def test_accepts_expected_wave_and_rejects_foreign_path(monkeypatch):
    audio = AudioStorage()
    monkeypatch.setattr(audio, "client", lambda: httpx.Client(base_url="https://storage.invalid", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=wav()))))
    owner, detection = uuid4(), uuid4()
    audio.verify(owner, detection, audio.path(owner, detection))
    with pytest.raises(HTTPException):
        audio.verify(owner, detection, "foreign/path.wav")
