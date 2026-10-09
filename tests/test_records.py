"""HTTP contracts of the personal record without a database; the queries are checked in test_postgres.py."""
from birdnet_api.app import app, records
from birdnet_api.contracts import OwnSpeciesList, RecordSummary, SpeciesRecord
from tests.test_api import api  # noqa: F401  (fixture)


def test_record_routes_validate_species_and_time_zone(api):  # noqa: F811
    client, _, _, headers, _ = api
    calls = []

    class MemoryRecords:
        def summary(self, owner, tz):
            calls.append(("summary", tz))
            return RecordSummary(detections=0, species=0, sites=0, first_recorded_at=None, last_recorded_at=None, active_days=0)

        def species(self, owner):
            return OwnSpeciesList(species=[])

        def species_record(self, owner, species, tz):
            calls.append((species, tz))
            return SpeciesRecord(species=species, detections=0, best_confidence=None, first_recorded_at=None, last_recorded_at=None,
                                 hours=[0] * 24, sites=[], cells=[], recent=[])

    app.dependency_overrides[records] = MemoryRecords
    assert client.get("/v1/me/species").status_code == 401
    assert client.get("/v1/me/summary", params={"tz": "America"}, headers=headers()).status_code == 422
    assert client.get("/v1/me/species/" + "x" * 201, headers=headers()).status_code == 422
    assert client.get("/v1/me/summary", headers=headers()).status_code == 200
    unseen = client.get("/v1/me/species/Turdus%20fuscater", params={"tz": "UTC"}, headers=headers())
    assert unseen.status_code == 200 and unseen.json()["detections"] == 0
    assert calls == [("summary", "America/Bogota"), ("Turdus fuscater", "UTC")]
