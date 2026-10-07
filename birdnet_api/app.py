"""Synchronization, collective map queries and the model manifest; verification arrives in S6."""
import json
import os
from pathlib import Path
from uuid import UUID
from urllib.parse import urlparse

import httpx

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .auth import authenticate
from .contracts import AudioInput, BatchInput, BatchResponse, MapQuery, MapResponse
from .repository import DetectionRepository
from .storage import AudioStorage

# Deployed as its own project: routes live at the root and the web app proxies /api/* here.
app = FastAPI(title="BirdNet Local API", docs_url=None, redoc_url=None)
MANIFEST = Path(__file__).with_name("model_manifest.json")


def repository() -> DetectionRepository:
    return DetectionRepository()


def storage() -> AudioStorage:
    return AudioStorage()


@app.middleware("http")
async def bounded_body(request: Request, call_next):
    # The metadata endpoint never accepts audio or unbounded JSON bodies.
    if request.method == "POST":
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 128 * 1024:
                return JSONResponse({"detail": "Request body exceeds metadata limit"}, status_code=413)
        request._body = bytes(data)
    return await call_next(request)


@app.post("/v1/detections/batch", response_model=BatchResponse)
def batch(payload: BatchInput, owner: UUID = Depends(authenticate), repo: DetectionRepository = Depends(repository), audio: AudioStorage = Depends(storage)) -> BatchResponse:
    for row in payload.detections:
        if (row.status == "confirmed") != (row.confidence >= 0.80):
            raise HTTPException(422, "Confidence and verification state disagree")
        if row.audio_path:
            if row.status != "provisional":
                raise HTTPException(422, "Only provisional detections can include audio")
            if repo.get_owned(owner, row.id) is None:
                raise HTTPException(404, "Detection must be synchronized before uploading audio")
            audio.verify(owner, row.id, row.audio_path)
    return repo.batch(owner, payload.detections)


@app.post("/v1/detections/{detection_id}/audio-url")
def audio_url(detection_id: UUID, payload: AudioInput, owner: UUID = Depends(authenticate), repo: DetectionRepository = Depends(repository), audio: AudioStorage = Depends(storage)) -> dict[str, str]:
    row = repo.get_owned(owner, detection_id)
    if row is None:
        raise HTTPException(404, "Detection not found")
    if row["estado"] != "provisional":
        raise HTTPException(422, "Only provisional audio may be uploaded")
    return audio.sign(owner, detection_id)


@app.get("/v1/detections", response_model=MapResponse)
def detections(query: Annotated[MapQuery, Query()], viewer: UUID = Depends(authenticate), repo: DetectionRepository = Depends(repository)) -> MapResponse:
    if query.west > query.east or query.south > query.north:
        raise HTTPException(422, "Invalid map bounds")
    if query.since and query.until and query.since >= query.until:
        raise HTTPException(422, "Invalid period")
    return repo.map(viewer, query)


@app.get("/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/model/latest")
def latest_model() -> dict:
    manifest_file = Path(os.environ.get("MODEL_MANIFEST_PATH", MANIFEST))
    try:
        remote = os.environ.get("MODEL_MANIFEST_URL")
        if remote:
            url = urlparse(remote)
            if url.scheme != "https" or not (url.hostname or "").endswith(".supabase.co"):
                raise ValueError("Invalid manifest origin")
            with httpx.Client(timeout=10) as client:
                with client.stream("GET", remote) as response:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 128 * 1024:
                            raise ValueError("Manifest exceeds metadata limit")
                manifest = json.loads(content)
        else:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        resource_base = os.environ.get("MODEL_RESOURCE_BASE_URL", "/models/").rstrip("/")
        for field in ("model_file", "labels_file"):
            if not manifest[field].startswith(("https://", "http://", "/")):
                manifest[field] = f"{resource_base}/{manifest[field]}"
        return manifest
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        raise HTTPException(503, "Model manifest unavailable") from None
