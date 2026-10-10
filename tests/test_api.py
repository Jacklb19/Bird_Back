"""Exercise HTTP contracts, trust boundaries and retries without remote services."""
import copy
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from birdnet_api.app import app, repository, storage
from birdnet_api.contracts import BatchResponse, MapResponse


class MemoryRepository:
    def __init__(self):
        self.rows = {}
        self.jobs = set()
        self.lock = threading.Lock()

    def get_owned(self, owner, detection):
        row = self.rows.get(str(detection))
        if not row or row["owner"] != owner:
            return None
        return {"id": detection, "estado": "provisional" if row["status"] == "provisional" else "confirmada"}

    def map(self, viewer, query):
        self.last_query = query
        return MapResponse(detections=[], truncated=False)

    def batch(self, owner, detections):
        with self.lock:
            rows = copy.deepcopy(self.rows)
            jobs = self.jobs.copy()
            accepted, existing = [], []
            for row in detections:
                previous = rows.get(str(row.id))
                if previous and previous["owner"] != owner:
                    raise HTTPException(409, "Identifier conflict")
                (existing if previous else accepted).append(row.id)
                rows[str(row.id)] = {**row.model_dump(), "owner": owner}
                if row.audio_path:
                    jobs.add(row.id)
            self.rows, self.jobs = rows, jobs
            return BatchResponse(accepted_ids=accepted, existing_ids=existing)


class MemoryStorage:
    def __init__(self):
        self.verified = []

    def verify(self, owner, detection, path):
        if path != f"{owner}/{detection}.wav":
            raise HTTPException(422, "Invalid path")
        self.verified.append(path)

    def sign(self, owner, detection):
        return {"upload_url": "https://storage.invalid/signed", "audio_path": f"{owner}/{detection}.wav"}


@pytest.fixture
def api(monkeypatch):
    secret = secrets.token_hex(32)
    issuer = "https://auth.invalid/auth/v1"
    monkeypatch.setenv("SUPABASE_AUTH_ISSUER", issuer)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", secret)
    repo, audio = MemoryRepository(), MemoryStorage()
    app.dependency_overrides[repository] = lambda: repo
    app.dependency_overrides[storage] = lambda: audio
    owner = uuid4()

    def headers(user=owner, **changes):
        claims = {"sub": str(user), "iss": issuer, "aud": "authenticated", "role": "authenticated", "iat": int(time.time()), "exp": int(time.time()) + 600, **changes}
        return {"Authorization": "Bearer " + jwt.encode(claims, secret, algorithm="HS256")}

    with TestClient(app) as client:
        yield client, repo, audio, headers, owner
    app.dependency_overrides.clear()


def detection(**changes):
    return {"id": str(uuid4()), "species": "Turdus fuscater", "confidence": 0.9, "status": "confirmed", "recorded_at": "2026-10-06T12:00:00Z", "location": {"latitude": 4.679, "longitude": -74.123}, "model_version": "birdnet-v2.4:arm", **changes}


def test_repeated_and_concurrent_batches_are_idempotent(api):
    client, repo, _, headers, _ = api
    rows = [detection() for _ in range(50)]
    first = client.post("/v1/detections/batch", json={"detections": rows}, headers=headers())
    assert first.status_code == 200
    assert len(first.json()["accepted_ids"]) == 50
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: client.post("/v1/detections/batch", json={"detections": rows}, headers=headers()), range(20)))
    assert all(response.status_code == 200 and len(response.json()["existing_ids"]) == 50 for response in responses)
    assert len(repo.rows) == 50


@pytest.mark.parametrize("changes", [{"exp": 1}, {"aud": "anon"}, {"role": "anon"}, {"iss": "https://foreign.invalid"}, {"sub": "not-a-uuid"}])
def test_rejects_untrusted_identity(api, changes):
    client, repo, _, headers, _ = api
    assert client.post("/v1/detections/batch", json={"detections": [detection()]}, headers=headers(**changes)).status_code == 401
    assert repo.rows == {}


def test_missing_and_tampered_tokens_are_rejected(api):
    client, repo, _, headers, _ = api
    payload = {"detections": [detection()]}
    assert client.post("/v1/detections/batch", json=payload).status_code == 401
    auth = headers()
    auth["Authorization"] = auth["Authorization"][:-8] + "tampered"
    assert client.post("/v1/detections/batch", json=payload, headers=auth).status_code == 401
    assert repo.rows == {}


@pytest.mark.parametrize("changes", [{"location": {"latitude": 4.67891, "longitude": -74.123}}, {"location": {"latitude": 4.679, "longitude": -74.12345}}, {"confidence": 0.4}, {"confidence": 0.6}, {"recorded_at": "2026-10-06T12:00:00"}, {"user_id": str(uuid4())}, {"location": None}, {"audio_path": "foreign/audio.wav"}])
def test_rejects_invalid_detection_boundaries(api, changes):
    client, repo, _, headers, _ = api
    response = client.post("/v1/detections/batch", json={"detections": [detection(**changes)]}, headers=headers())
    assert response.status_code == 422
    assert repo.rows == {}


def test_accepts_the_location_grid_and_the_coarser_one_before_it(api):
    client, repo, _, headers, _ = api
    # Clients that still round to 3 decimals send points that lie on the 4-decimal grid too.
    coarse, fine = detection(), detection(location={"latitude": 4.6789, "longitude": -74.1234})
    assert client.post("/v1/detections/batch", json={"detections": [coarse, fine]}, headers=headers()).status_code == 200
    assert repo.rows[coarse["id"]]["location"] == coarse["location"] and repo.rows[fine["id"]]["location"] == fine["location"]


def test_foreign_uuid_conflict_does_not_acknowledge_or_partially_commit(api):
    client, repo, _, headers, _ = api
    row = detection()
    client.post("/v1/detections/batch", json={"detections": [row]}, headers=headers())
    response = client.post("/v1/detections/batch", json={"detections": [detection(), row]}, headers=headers(uuid4()))
    assert response.status_code == 409
    assert len(repo.rows) == 1


def test_audio_requires_ownership_and_links_only_after_validation(api):
    client, repo, audio, headers, owner = api
    row = detection(confidence=0.6, status="provisional")
    url = f"/v1/detections/{row['id']}/audio-url"
    request = {"content_type": "audio/wav", "size_bytes": 288044}
    assert client.post(url, json=request, headers=headers()).status_code == 404
    assert client.post("/v1/detections/batch", json={"detections": [row]}, headers=headers()).status_code == 200
    assert client.post(url, json=request, headers=headers(uuid4())).status_code == 404
    signed = client.post(url, json=request, headers=headers())
    assert signed.status_code == 200
    row["audio_path"] = signed.json()["audio_path"]
    for _ in range(3):
        assert client.post("/v1/detections/batch", json={"detections": [row]}, headers=headers()).status_code == 200
    assert len(repo.jobs) == 1
    assert audio.verified == [f"{owner}/{row['id']}.wav"] * 3


def test_limits_and_public_model_contract(api):
    client, _, _, headers, _ = api
    assert client.post("/v1/detections/batch", json={"detections": [detection() for _ in range(51)]}, headers=headers()).status_code == 422
    assert client.post("/v1/detections/batch", content=b" " * (128 * 1024 + 1), headers=headers()).status_code == 413
    response = client.get("/v1/model/latest")
    assert response.status_code == 200
    assert response.json()["model_file"] == "/models/birdnet_model.onnx"
    assert len(response.json()["sha256"]) == 64


def test_map_query_requires_session_and_valid_bounds(api):
    client, repo, _, headers, _ = api
    area = {"west": -74.2, "south": 4.5, "east": -74.0, "north": 4.8}
    assert client.get("/v1/detections", params=area).status_code == 401
    assert client.get("/v1/detections", params={**area, "west": -73.0}, headers=headers()).status_code == 422
    assert client.get("/v1/detections", params={**area, "since": "2026-10-01T00:00:00"}, headers=headers()).status_code == 422
    response = client.get("/v1/detections", params={**area, "species": "Turdus fuscater", "since": "2026-10-01T00:00:00Z"}, headers=headers())
    assert response.status_code == 200 and response.json() == {"detections": [], "truncated": False}
    assert repo.last_query.species == "Turdus fuscater"
