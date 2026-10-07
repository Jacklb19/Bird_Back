"""HTTP contracts for sites, statistics and CSV export without a database."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from birdnet_api.app import app, now, sites
from birdnet_api.contracts import Site, SiteStats
from tests.test_api import api  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class MemorySites:
    def __init__(self):
        self.owned = {}
        self.calls = []

    def list_sites(self, owner):
        return [s for o, s in self.owned.values() if o == owner]

    def create_site(self, owner, payload):
        site = Site(id=uuid4(), name=payload.name, latitude=payload.location.latitude, longitude=payload.location.longitude, created_at=NOW)
        self.owned[site.id] = (owner, site)
        return site

    def _check(self, owner, site_id):
        if self.owned.get(site_id, (None,))[0] != owner:
            raise HTTPException(404, "Site not found")

    def stats(self, owner, site_id, period, since, previous, until, tz):
        self._check(owner, site_id)
        self.calls.append((period, since, previous, until, tz))
        return SiteStats(period=period, since=since, until=until, species_count=0, previous_species_count=None, detections=0, active_days=0, hourly=[0] * 24, species=[], missing=[])

    def export_csv(self, owner, site_id, since, until):
        self._check(owner, site_id)
        return "id,species\n", False


@pytest.fixture
def site_api(api):  # noqa: F811
    client, _, _, headers, owner = api
    repo = MemorySites()
    app.dependency_overrides[sites] = lambda: repo
    app.dependency_overrides[now] = lambda: NOW
    yield client, repo, headers, owner


def test_sites_require_session_and_approximate_location(site_api):
    client, _, headers, _ = site_api
    body = {"name": "  Finca   El Roble ", "location": {"latitude": 4.711, "longitude": -74.072}}
    assert client.post("/v1/sites", json=body).status_code == 401
    assert client.post("/v1/sites", json={**body, "location": {"latitude": 4.71123, "longitude": -74.072}}, headers=headers()).status_code == 422
    assert client.post("/v1/sites", json={**body, "name": "   "}, headers=headers()).status_code == 422
    created = client.post("/v1/sites", json=body, headers=headers())
    assert created.status_code == 201 and created.json()["name"] == "Finca El Roble"
    assert [s["name"] for s in client.get("/v1/sites", headers=headers()).json()["sites"]] == ["Finca El Roble"]


def test_stats_windows_and_foreign_sites(site_api):
    client, repo, headers, _ = site_api
    site = client.post("/v1/sites", json={"name": "Humedal", "location": {"latitude": 4.7, "longitude": -74.1}}, headers=headers()).json()
    assert client.get(f"/v1/sites/{site['id']}/stats", params={"tz": "Mars/Base"}, headers=headers()).status_code == 422
    assert client.get(f"/v1/sites/{site['id']}/stats", params={"period": "month"}, headers=headers()).status_code == 200
    period, since, previous, until, tz = repo.calls[-1]
    assert (until - since, since - previous, tz) == (timedelta(days=30), timedelta(days=30), "America/Bogota")
    client.get(f"/v1/sites/{site['id']}/stats", params={"period": "all"}, headers=headers())
    assert repo.calls[-1][1:3] == (None, None)
    assert client.get(f"/v1/sites/{site['id']}/stats", headers=headers(user=uuid4())).status_code == 404


def test_export_is_csv_for_own_sites_only(site_api):
    client, _, headers, _ = site_api
    site = client.post("/v1/sites", json={"name": "Barrio", "location": {"latitude": 4.6, "longitude": -74.0}}, headers=headers()).json()
    response = client.get("/v1/export", params={"site_id": site["id"]}, headers=headers())
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert client.get("/v1/export", params={"site_id": site["id"], "since": "2026-10-07T00:00:00Z", "until": "2026-10-01T00:00:00Z"}, headers=headers()).status_code == 422
    assert client.get("/v1/export", params={"site_id": site["id"]}, headers=headers(user=uuid4())).status_code == 404
