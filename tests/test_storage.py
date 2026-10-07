"""Validate uploaded audio bytes instead of trusting client paths or MIME headers."""
import struct
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from birdnet_api.storage import AudioStorage


def wav():
    return b"RIFF" + struct.pack("<I", 288036) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 48000, 96000, 2, 16) + b"data" + struct.pack("<I", 288000) + bytes(288000)


@pytest.mark.parametrize("content", [b"invalid", wav() + b"extra", wav()[:44], wav()[:24] + struct.pack("<I", 44100) + wav()[28:]], ids=["invalid", "oversize", "truncated", "sample-rate"])
def test_rejects_invalid_audio(monkeypatch, content):
    audio = AudioStorage()
    monkeypatch.setattr(audio, "client", lambda: httpx.Client(base_url="https://storage.invalid", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))))
    owner, detection = uuid4(), uuid4()
    with pytest.raises(HTTPException) as error:
        audio.verify(owner, detection, audio.path(owner, detection))
    assert error.value.status_code == 422


def test_accepts_expected_wave_and_rejects_foreign_path(monkeypatch):
    audio = AudioStorage()
    monkeypatch.setattr(audio, "client", lambda: httpx.Client(base_url="https://storage.invalid", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=wav()))))
    owner, detection = uuid4(), uuid4()
    audio.verify(owner, detection, audio.path(owner, detection))
    with pytest.raises(HTTPException):
        audio.verify(owner, detection, "foreign/path.wav")
