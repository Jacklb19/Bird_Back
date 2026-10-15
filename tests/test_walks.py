"""HTTP contracts of walks saved in the account (ADR-35), without a database.

Ownership, quota and the figures computed from detections are SQL; test_postgres.py checks them against PostgreSQL.
"""
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from birdnet_api.app import app, walks
from birdnet_api.contracts import Walk, WalkDetail, WalkPage
from birdnet_api.domain import MAX_WALK_POINTS, track_meters
from birdnet_api.settings import get_settings
from tests.test_api import api  # noqa: F401  (fixture)

QUOTA = 2


class MemoryWalks:
    def __init__(self):
        self.rows = {}

    def _summary(self, walk_id):
        _, payload = self.rows[walk_id]
        return Walk(id=walk_id, name=payload.name, started_at=payload.started_at, ended_at=payload.ended_at,
                    distance_m=track_meters([(p[0], p[1]) for p in payload.track]), points=len(payload.track), detections=0, species=[])

    def save(self, owner, walk_id, payload, quota):
        stored = self.rows.get(walk_id)
        if stored and stored[0] != owner:
            raise HTTPException(409, "Conflict")
        if not stored and sum(1 for o, _ in self.rows.values() if o == owner) >= quota:
            raise HTTPException(507, "Quota")
        self.rows[walk_id] = (owner, payload)
        return self._summary(walk_id)

    def rename(self, owner, walk_id, name):
        stored = self.rows.get(walk_id)
        if not stored or stored[0] != owner:
            return None
        self.rows[walk_id] = (owner, stored[1].model_copy(update={"name": name}))
        return self._summary(walk_id)

    def page(self, owner, before, limit):
        self.last_page = (before, limit)
        return WalkPage(walks=[self._summary(k) for k, (o, _) in self.rows.items() if o == owner], has_more=False)

    def detail(self, owner, walk_id):
        stored = self.rows.get(walk_id)
        if not stored or stored[0] != owner:
            return None
        return WalkDetail(**self._summary(walk_id).model_dump(), track=stored[1].track, places=[])

    def delete(self, owner, walk_id):
        if self.rows.get(walk_id, (None,))[0] == owner:
            del self.rows[walk_id]

    def delete_all(self, owner):
        mine = [k for k, (o, _) in self.rows.items() if o == owner]
        for key in mine:
            del self.rows[key]
        return len(mine)

    def usage(self, owner):
        return sum(1 for o, _ in self.rows.values() if o == owner)


@pytest.fixture
def walk_api(api, monkeypatch):  # noqa: F811
    client, _, headers, owner = api
    monkeypatch.setenv("WALK_QUOTA", str(QUOTA))
    # The client fixture already read the settings at startup.
    get_settings.cache_clear()
    repo = MemoryWalks()
    app.dependency_overrides[walks] = lambda: repo
    return client, repo, headers, owner


def walk(**changes):
    return {"name": "Paseo al humedal", "started_at": "2026-10-15T07:00:00-05:00", "ended_at": "2026-10-15T08:00:00-05:00",
            "track": [[4.6789, -74.1234, 0], [4.6799, -74.1234, 60], [4.6799, -74.1244, 600]], **changes}


def test_distance_uses_the_formula_of_the_phone():
    # Same cells and value as the client's walk.test.ts: a thousandth of a degree of latitude.
    assert track_meters([(4.6789, -74.1234), (4.6799, -74.1234)]) == pytest.approx(111.195, abs=0.001)
    assert track_meters([]) == 0 and track_meters([(4.6789, -74.1234)]) == 0


@pytest.mark.parametrize("changes", [
    {"track": [[4.67891, -74.1234, 0]]},
    {"track": [[91.0, -74.1234]]},
    {"track": [[4.6789, -74.1234, 10], [4.679, -74.1234, 5]]},
    {"track": [[4.6789, -74.1234, 0], [4.679, -74.1234]]},
    {"track": [[4.6789, -74.1234, 3602]]},
    {"track": [[4.6789, -74.1234, -1]]},
    {"track": [[4.6789, -74.1234]] * (MAX_WALK_POINTS + 1)},
    {"ended_at": "2026-10-15T06:59:59-05:00"},
    {"started_at": "2026-10-15T07:00:00"},
    {"name": "   "},
    {"name": "x" * 81},
    {"shared": True},
], ids=["off-grid", "out-of-range", "time-goes-back", "mixed-times", "after-end", "negative-time", "too-long",
        "ends-before-start", "naive-time", "blank-name", "long-name", "unknown-field"])
def test_walk_upload_is_validated(walk_api, changes):
    client, repo, headers, _ = walk_api
    assert client.put(f"/v1/walks/{uuid4()}", json=walk(**changes), headers=headers()).status_code == 422
    assert repo.rows == {}


def test_walk_upload_is_idempotent_and_private(walk_api):
    client, repo, headers, owner = walk_api
    walk_id = str(uuid4())
    assert client.put(f"/v1/walks/{walk_id}", json=walk()).status_code == 401
    first = client.put(f"/v1/walks/{walk_id}", json=walk(name="  Paseo   al humedal "), headers=headers())
    assert first.status_code == 200
    body = first.json()
    assert body["name"] == "Paseo al humedal" and body["points"] == 3 and body["distance_m"] > 0
    assert client.put(f"/v1/walks/{walk_id}", json=walk(), headers=headers()).json() == body
    # Walks recorded before points had times still upload, without them.
    legacy = client.put(f"/v1/walks/{uuid4()}", json={k: v for k, v in walk(track=[[4.6789, -74.1234], [4.679, -74.1234]]).items() if k != "name"},
                        headers=headers())
    assert legacy.status_code == 200 and legacy.json()["name"] is None
    assert client.put(f"/v1/walks/{walk_id}", json=walk(), headers=headers(user=uuid4())).status_code == 409
    assert client.get(f"/v1/walks/{walk_id}", headers=headers(user=uuid4())).status_code == 404
    detail = client.get(f"/v1/walks/{walk_id}", headers=headers()).json()
    assert detail["track"] == walk()["track"]
    assert client.get("/v1/me/walks", headers=headers()).json() == {"walks": 2, "limit": QUOTA}
    assert all(o == owner for o, _ in repo.rows.values())


def test_walk_quota_rename_and_delete(walk_api):
    client, repo, headers, _ = walk_api
    ids = [str(uuid4()) for _ in range(QUOTA)]
    for walk_id in ids:
        assert client.put(f"/v1/walks/{walk_id}", json=walk(), headers=headers()).status_code == 200
    assert client.put(f"/v1/walks/{uuid4()}", json=walk(), headers=headers()).status_code == 507
    # A walk already stored can still be sent again when the account is full.
    assert client.put(f"/v1/walks/{ids[0]}", json=walk(), headers=headers()).status_code == 200
    renamed = client.patch(f"/v1/walks/{ids[0]}", json={"name": " Al alba "}, headers=headers())
    assert renamed.status_code == 200 and renamed.json()["name"] == "Al alba"
    assert client.patch(f"/v1/walks/{uuid4()}", json={"name": "Otra"}, headers=headers()).status_code == 404
    assert client.patch(f"/v1/walks/{ids[0]}", json={"name": ""}, headers=headers()).status_code == 422
    assert client.patch(f"/v1/walks/{ids[0]}", json={}, headers=headers()).status_code == 422
    assert client.patch(f"/v1/walks/{ids[0]}", json={"name": None}, headers=headers()).json()["name"] is None
    assert client.delete(f"/v1/walks/{ids[0]}", headers=headers()).status_code == 204
    assert client.delete(f"/v1/walks/{ids[0]}", headers=headers()).status_code == 204
    assert client.delete("/v1/me/walks", headers=headers()).json() == {"deleted": 1}
    assert repo.rows == {}


def test_walk_list_is_paged(walk_api):
    client, repo, headers, _ = walk_api
    assert client.get("/v1/walks", headers=headers()).json() == {"walks": [], "has_more": False}
    assert client.get("/v1/walks", params={"limit": 51}, headers=headers()).status_code == 422
    assert client.get("/v1/walks", params={"before": "2026-10-15T07:00:00"}, headers=headers()).status_code == 422
    assert client.get("/v1/walks", params={"before": "2026-10-15T07:00:00Z", "limit": 5}, headers=headers()).status_code == 200
    before, limit = repo.last_page
    assert before == datetime.fromisoformat("2026-10-15T07:00:00+00:00") and limit == 5
