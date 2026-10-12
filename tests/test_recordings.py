"""HTTP contracts of saved recordings and of review, with Storage replaced by a local transport.

The quota arithmetic and the review states are SQL; test_postgres.py checks them against PostgreSQL.
"""
import json
from functools import partial
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from birdnet_api.app import app, clips, recordings
from birdnet_api.contracts import ReviewedDetection
from birdnet_api.settings import get_settings
from birdnet_api.storage import RecordingStorage
from tests.test_api import api, detection  # noqa: F401  (fixture)

WEBM = bytes.fromhex("1a45dfa3") + bytes(60)
QUOTA = 1000


class MemoryRecordings:
    def __init__(self, detections):
        self.detections = detections
        self.links = {}

    def _owns(self, owner, detection_id):
        row = self.detections.rows.get(str(detection_id))
        return bool(row) and row["owner"] == owner

    def usage(self, owner):
        sizes = [size for (user, _), (_, size) in self.links.items() if user == owner]
        return len(sizes), sum(sizes)

    def path(self, owner, detection_id):
        return self.links.get((owner, detection_id), (None, 0))[0]

    def has_room(self, owner, detection_id, size, quota):
        return sum(stored for (user, other), (_, stored) in self.links.items() if user == owner and other != detection_id) + size <= quota

    def link(self, owner, detection_id, path, size, quota):
        if not self.has_room(owner, detection_id, size, quota):
            raise HTTPException(507, "Quota")
        self.links[(owner, detection_id)] = (path, size)

    def unlink(self, owner, detection_id):
        self.links.pop((owner, detection_id), None)

    def paths(self, owner):
        return [path for (user, _), (path, _) in self.links.items() if user == owner]

    def unlink_all(self, owner, paths):
        mine = [key for key, (path, _) in self.links.items() if key[0] == owner and path in paths]
        for key in mine:
            del self.links[key]
        return len(mine)


@pytest.fixture
def recording_api(api, monkeypatch):  # noqa: F811
    client, repo, _, headers, owner = api
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
    monkeypatch.setenv("RECORDING_QUOTA_BYTES", str(QUOTA))
    # The client fixture already read the settings at startup.
    get_settings.cache_clear()
    objects = {}

    def serve(request):
        path = request.url.path.removeprefix("/storage/v1")
        if path.startswith("/object/upload/sign/"):
            return httpx.Response(200, json={"url": f"{path}?token=up"})
        if path.startswith("/object/sign/"):
            return httpx.Response(200, json={"signedURL": f"{path}?token=down"})
        if request.method == "DELETE":
            for key in json.loads(request.content)["prefixes"]:
                objects.pop(key, None)
            return httpx.Response(200, json=[])
        key = path.removeprefix("/object/recordings/")
        return httpx.Response(200, content=objects[key]) if key in objects else httpx.Response(404)

    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(serve)))
    saved = MemoryRecordings(repo)
    app.dependency_overrides[recordings] = lambda: saved
    app.dependency_overrides[clips] = RecordingStorage

    def synchronized(user=owner):
        row = detection()
        assert client.post("/v1/detections/batch", json={"detections": [row]}, headers=headers(user)).status_code == 200
        return row["id"]

    return client, saved, objects, headers, owner, synchronized


def declaration(size=len(WEBM)):
    return {"content_type": "audio/webm", "size_bytes": size}


@pytest.mark.parametrize("body", [{"content_type": "audio/wav", "size_bytes": 1000}, {"content_type": "audio/webm", "size_bytes": 0},
                                  {"content_type": "audio/webm", "size_bytes": 100_001}, {"content_type": "audio/webm", "size_bytes": "1000"}],
                         ids=["type", "empty", "oversize", "string-size"])
def test_recording_upload_declaration_is_validated(recording_api, body):
    client, _, _, headers, _, synchronized = recording_api
    assert client.post(f"/v1/detections/{synchronized()}/recording-url", json=body, headers=headers()).status_code == 422


def test_recording_links_only_an_own_verified_clip(recording_api):
    client, saved, objects, headers, owner, synchronized = recording_api
    row = synchronized()
    path = f"{owner}/{row}.webm"
    signed = client.post(f"/v1/detections/{row}/recording-url", json=declaration(), headers=headers()).json()
    assert signed == {"upload_url": f"https://project.supabase.co/storage/v1/object/upload/sign/recordings/{path}?token=up", "recording_path": path}
    link = lambda body: client.put(f"/v1/detections/{row}/recording", json=body, headers=headers())  # noqa: E731
    # Not uploaded yet, then not a WebM file, then the clip of another detection.
    assert link({"recording_path": path}).status_code == 422
    objects[path] = b"RIFF" + bytes(60)
    assert link({"recording_path": path}).status_code == 422
    other = f"{owner}/{uuid4()}.webm"
    objects[other] = WEBM
    assert link({"recording_path": other}).status_code == 422
    assert saved.links == {}
    objects[path] = WEBM
    assert link({"recording_path": path}).json() == {"recordings": 1, "bytes": len(WEBM), "limit_bytes": QUOTA}
    played = client.get(f"/v1/detections/{row}/recording", headers=headers()).json()
    assert played == {"url": f"https://project.supabase.co/storage/v1/object/sign/recordings/{path}?token=down", "expires_in": 600}


def test_recordings_are_private_to_their_author(recording_api):
    client, saved, objects, headers, owner, synchronized = recording_api
    row = synchronized()
    path = f"{owner}/{row}.webm"
    objects[path] = WEBM
    assert client.put(f"/v1/detections/{row}/recording", json={"recording_path": path}, headers=headers()).status_code == 200
    stranger = uuid4()
    assert client.get(f"/v1/detections/{row}/recording").status_code == 401
    assert client.get(f"/v1/detections/{row}/recording", headers=headers(stranger)).status_code == 404
    assert client.post(f"/v1/detections/{row}/recording-url", json=declaration(), headers=headers(stranger)).status_code == 404
    assert client.put(f"/v1/detections/{row}/recording", json={"recording_path": path}, headers=headers(stranger)).status_code == 404
    assert client.delete(f"/v1/detections/{row}/recording", headers=headers(stranger)).status_code == 204
    assert client.delete("/v1/me/recordings", headers=headers(stranger)).json() == {"deleted": 0}
    assert path in objects and saved.links


def test_recording_quota_is_enforced_before_and_after_the_upload(recording_api):
    client, saved, objects, headers, owner, synchronized = recording_api
    first, second = synchronized(), synchronized()
    big = bytes.fromhex("1a45dfa3") + bytes(QUOTA - 104)
    objects[f"{owner}/{first}.webm"] = big
    assert client.put(f"/v1/detections/{first}/recording", json={"recording_path": f"{owner}/{first}.webm"}, headers=headers()).status_code == 200
    # 900 of 1000 bytes are taken: a 200-byte clip is refused when it is declared and again when it is linked.
    assert client.post(f"/v1/detections/{second}/recording-url", json=declaration(200), headers=headers()).status_code == 507
    objects[f"{owner}/{second}.webm"] = bytes.fromhex("1a45dfa3") + bytes(196)
    assert client.put(f"/v1/detections/{second}/recording", json={"recording_path": f"{owner}/{second}.webm"}, headers=headers()).status_code == 507
    # Replacing a detection's own clip does not count it twice.
    assert client.post(f"/v1/detections/{first}/recording-url", json=declaration(len(big)), headers=headers()).status_code == 200
    assert len(saved.links) == 1


def test_recordings_are_deleted_one_by_one_and_all_at_once(recording_api):
    client, saved, objects, headers, owner, synchronized = recording_api
    rows = [synchronized() for _ in range(3)]
    for row in rows:
        objects[f"{owner}/{row}.webm"] = WEBM
        assert client.put(f"/v1/detections/{row}/recording", json={"recording_path": f"{owner}/{row}.webm"}, headers=headers()).status_code == 200
    assert client.delete(f"/v1/detections/{rows[0]}/recording", headers=headers()).status_code == 204
    assert f"{owner}/{rows[0]}.webm" not in objects
    assert client.get(f"/v1/detections/{rows[0]}/recording", headers=headers()).status_code == 404
    # Deleting what is already gone is not an error.
    assert client.delete(f"/v1/detections/{rows[0]}/recording", headers=headers()).status_code == 204
    assert client.get("/v1/me/recordings", headers=headers()).json() == {"recordings": 2, "bytes": 2 * len(WEBM), "limit_bytes": QUOTA}
    assert client.delete("/v1/me/recordings", headers=headers()).json() == {"deleted": 2}
    assert objects == {} and saved.links == {}


@pytest.mark.parametrize("body", [{"action": "correct"}, {"action": "confirm", "species": "Turdus fuscater"}, {"action": "discard", "species": "Turdus fuscater"},
                                  {"action": "approve"}, {"action": "correct", "species": ""}, {"action": "correct", "species": " Turdus fuscater"},
                                  {"action": "correct", "species": "x" * 201}, {"action": "confirm", "note": "heard it"}],
                         ids=["no-species", "confirm-species", "discard-species", "unknown", "blank", "padded", "long", "extra"])
def test_review_verdict_is_validated(recording_api, body):
    client, _, _, headers, _, synchronized = recording_api
    assert client.patch(f"/v1/detections/{synchronized()}", json=body, headers=headers()).status_code == 422


def test_review_reaches_only_the_authors_detection(recording_api, monkeypatch):
    client, saved, _, headers, owner, synchronized = recording_api
    row = synchronized()
    calls = []

    def review(user, detection_id, action, species):
        calls.append((user, str(detection_id), action.value, species))
        if not saved._owns(user, detection_id):
            return None
        return ReviewedDetection(id=detection_id, species=species or "Turdus fuscater", model_species="Turdus fuscater" if species else None,
                                 status="corrected" if species else "verified", reviewed_by="person")

    monkeypatch.setattr(saved.detections, "review", review, raising=False)
    body = {"action": "correct", "species": "Zonotrichia capensis"}
    assert client.patch(f"/v1/detections/{row}", json=body).status_code == 401
    assert client.patch(f"/v1/detections/{row}", json=body, headers=headers(uuid4())).status_code == 404
    assert client.patch(f"/v1/detections/{row}", json=body, headers=headers()).json() == {
        "id": row, "species": "Zonotrichia capensis", "model_species": "Turdus fuscater", "status": "corrected", "reviewed_by": "person"}
    assert calls[-1] == (owner, row, "correct", "Zonotrichia capensis")
