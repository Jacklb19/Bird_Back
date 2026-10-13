"""Hand out only upload URLs that Storage itself signed, whatever its response claims."""
from functools import partial
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from birdnet_api.storage import RecordingStorage


def test_signs_only_storage_relative_upload_urls(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co/")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
    owner, detection = uuid4(), uuid4()
    path = f"{owner}/{detection}.webm"
    replies = iter([f"/object/upload/sign/recordings/{path}?token=t", "https://foreign.example/upload"])
    requests = []

    def serve(request):
        requests.append((request.url.path, request.headers["x-upsert"]))
        return httpx.Response(200, json={"url": next(replies)})

    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(serve)))
    clips = RecordingStorage()
    signed = clips.sign(owner, detection)
    assert (signed.upload_url, signed.recording_path) == (f"https://project.supabase.co/storage/v1/object/upload/sign/recordings/{path}?token=t", path)
    with pytest.raises(HTTPException) as error:
        clips.sign(owner, detection)
    assert error.value.status_code == 502
    assert requests == [(f"/storage/v1/object/upload/sign/recordings/{path}", "true")] * 2
