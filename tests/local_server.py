"""Temporary loopback-only server for browser fault injection; excluded from deployment."""
import os
import secrets
import time
import json
from pathlib import Path
from uuid import UUID

import jwt
import psycopg
from fastapi import Request
from fastapi.responses import JSONResponse

from birdnet_api.app import app, storage
from birdnet_api.domain import EXPECTED_WAV_BYTES
from birdnet_api.storage import AudioStorage
from tests.prepare_database import TEST_DATABASE_URL

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["SUPABASE_AUTH_ISSUER"] = "http://127.0.0.1:8000/auth/v1"
os.environ["SUPABASE_JWT_SECRET"] = secrets.token_hex(32)
owner = UUID("00000000-0000-4000-8000-000000000004")
with psycopg.connect(TEST_DATABASE_URL) as connection:
    connection.execute("INSERT INTO auth.users(id,email) VALUES (%s,'sprint4@example.invalid') ON CONFLICT DO NOTHING", (owner,))

state = {"lose_ack": False, "fail_link": False, "upload_count": 0}
uploads: dict[str, bytes] = {}


class LocalAudioStorage(AudioStorage):
    def sign(self, user, detection):
        path = self.path(user, detection)
        return {"upload_url": f"/api/test/upload/{path}", "audio_path": path}

    def verify(self, user, detection, path):
        if path != self.path(user, detection) or len(uploads.get(path, b"")) != EXPECTED_WAV_BYTES:
            raise ValueError("Missing fixture upload")


app.dependency_overrides[storage] = LocalAudioStorage


@app.middleware("http")
async def lose_response(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.endswith("/v1/detections/batch") and response.status_code == 200 and state["lose_ack"]:
        state["lose_ack"] = False
        return JSONResponse({"detail": "Simulated response loss after database commit"}, status_code=503)
    return response


@app.get("/test/session")
def session():
    expiry = int(time.time()) + 3600
    claims = {"sub": str(owner), "iss": os.environ["SUPABASE_AUTH_ISSUER"], "aud": "authenticated", "role": "authenticated", "iat": int(time.time()), "exp": expiry}
    return {"userId": str(owner), "accessToken": jwt.encode(claims, os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256"), "expiresAt": expiry}


@app.get("/test/invalid-manifest")
def invalid_manifest():
    manifest = json.loads((Path(__file__).resolve().parents[1] / "birdnet_api" / "model_manifest.json").read_text(encoding="utf-8-sig"))
    return {**manifest, "sha256": "0" * 64, "model_file": "/models/birdnet_model.onnx", "labels_file": "/models/labels.txt"}


@app.post("/test/lose-ack")
def lose_ack():
    state["lose_ack"] = True
    return {"ok": True}


@app.put("/test/upload/{path:path}")
async def upload(path: str, request: Request):
    uploads[path] = await request.body()
    state["upload_count"] += 1
    return {"ok": True}


@app.get("/test/counts")
def counts():
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        return {"detections": connection.execute("SELECT count(*) FROM public.detections WHERE user_id=%s", (owner,)).fetchone()[0], "jobs": connection.execute("SELECT count(*) FROM public.verification_jobs JOIN public.detections ON detections.id=verification_jobs.detection_id WHERE user_id=%s", (owner,)).fetchone()[0], "uploads": state["upload_count"]}
