"""Second step of sign-in, private notes, threatened species on the map and walk paths, against PostgreSQL and RLS."""
import secrets
import time
from uuid import uuid4

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

from birdnet_api import repository as repository_module
from birdnet_api.app import app
from birdnet_api.contracts import DetectionInput, MapQuery, NoteInput, WalkInput
from birdnet_api.domain import THREATENED_CELL_METERS
from birdnet_api.exports import ExportRepository
from birdnet_api.notes import NoteRepository
from birdnet_api.session import SECOND_FACTOR, session_assurance
from birdnet_api.walks import WalkRepository
from tests.prepare_database import TEST_DATABASE_URL
from tests.test_api import detection
from tests.test_postgres import database  # noqa: F401  (fixture)
from tests.test_postgres import pytestmark  # noqa: F401  (same isolated database only)

# Tables that hold data of a person; the public ones (species cards, model versions) need no second step.
PERSONAL_TABLES = ("profiles", "sites", "detections", "walks", "site_reports", "llm_usage", "detection_notes")


def add_factor(person, status="verified"):
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        connection.execute("INSERT INTO auth.mfa_factors(user_id, status) VALUES (%s, %s)", (person, status))


def test_every_personal_table_asks_for_the_second_step():
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        restricted = {row[0] for row in connection.execute(
            "SELECT tablename FROM pg_policies WHERE schemaname='public' AND permissive='RESTRICTIVE' AND qual LIKE '%session_meets_second_factor%'")}
    assert restricted == set(PERSONAL_TABLES)


def test_rls_hides_everything_from_a_session_without_the_code(database):  # noqa: F811
    repo, owner, _ = database
    row = DetectionInput.model_validate(detection())
    repo.batch(owner, [row])
    # An unverified factor (enrolment never finished) asks for nothing.
    add_factor(owner, "unverified")
    assert repo.get_owned(owner, row.id) is not None and not repo.has_second_factor(owner)
    add_factor(owner)
    assert repo.has_second_factor(owner)
    assert repo.get_owned(owner, row.id) is None
    with repo.transaction(owner) as connection:
        assert connection.execute("UPDATE public.detections SET compartida=false WHERE id=%s", (row.id,)).rowcount == 0
    token = session_assurance.set(SECOND_FACTOR)
    try:
        assert repo.get_owned(owner, row.id) is not None
    finally:
        session_assurance.reset(token)


def test_the_api_carries_the_session_level_into_rls(database, monkeypatch):  # noqa: F811
    repo, owner, _ = database
    secret, issuer = secrets.token_hex(32), "https://auth.invalid/auth/v1"
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("SUPABASE_AUTH_ISSUER", issuer)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", secret)

    def headers(level):
        claims = {"sub": str(owner), "iss": issuer, "aud": "authenticated", "role": "authenticated", "iat": int(time.time()),
                  "exp": int(time.time()) + 600, "aal": level}
        return {"Authorization": "Bearer " + jwt.encode(claims, secret, algorithm="HS256")}

    add_factor(owner)
    with TestClient(app) as client:
        assert client.get("/v1/me/summary", headers=headers("aal1")).status_code == 403
        # The level reaches the database's claims: with the code, RLS lets the owner read their own record.
        response = client.post("/v1/detections/batch", json={"detections": [detection()]}, headers=headers("aal2"))
        assert response.status_code == 200 and len(response.json()["accepted_ids"]) == 1
        assert client.get("/v1/me/summary", headers=headers("aal2")).json()["detections"] == 1


def test_notes_are_private_idempotent_and_exported(database):  # noqa: F811
    repo, owner, other = database
    notes = NoteRepository()
    row = DetectionInput.model_validate(detection())
    repo.batch(owner, [row])
    note = NoteInput(note="=HYPERLINK(\"x\") dos machos", individuals=2)
    saved = notes.save(owner, row.id, note)
    assert (saved.note, saved.individuals) == (note.note, 2)
    assert notes.save(owner, row.id, note) == saved
    assert notes.save(other, row.id, NoteInput(note="forged")) is None
    with repo.transaction(other) as connection:
        assert connection.execute("SELECT count(*) AS n FROM public.detection_notes").fetchone()["n"] == 0
        # Even a shared detection, readable by others on the map, does not open its note.
        assert connection.execute("SELECT count(*) AS n FROM public.detections WHERE id=%s", (row.id,)).fetchone()["n"] == 1
    area = {"west": -74.2, "south": 4.6, "east": -74.0, "north": 4.8}
    assert "note" not in repo.map(other, MapQuery(**area)).detections[0].model_dump()
    body, _ = ExportRepository().export_csv(owner, None, None, None, None)
    header, line = body.splitlines()[:2]
    assert header.endswith(",individuals,note")
    # A note that would run as a formula in a spreadsheet goes out as text.
    assert line.endswith(",2,\"'=HYPERLINK(\"\"x\"\") dos machos\"")
    notes.save(owner, row.id, NoteInput())
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        assert connection.execute("SELECT count(*) FROM public.detection_notes WHERE detection_id=%s", (row.id,)).fetchone()[0] == 0
        connection.execute("INSERT INTO public.detection_notes(detection_id,user_id,individuos) VALUES (%s,%s,3)", (row.id, owner))
        connection.execute("DELETE FROM public.detections WHERE id=%s", (row.id,))
        assert connection.execute("SELECT count(*) FROM public.detection_notes WHERE detection_id=%s", (row.id,)).fetchone()[0] == 0


def test_notes_cannot_hang_from_someone_elses_detection(database):  # noqa: F811
    repo, owner, other = database
    row = DetectionInput.model_validate(detection())
    repo.batch(owner, [row])
    with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(TEST_DATABASE_URL) as connection:
        connection.execute("SET ROLE authenticated")
        connection.execute("SELECT set_config('request.jwt.claims', %s, true)", (f'{{"sub":"{other}","role":"authenticated"}}',))
        connection.execute("INSERT INTO public.detection_notes(detection_id,user_id,nota) VALUES (%s,%s,'x')", (row.id, other))


def test_threatened_species_reach_others_only_in_a_coarse_cell(database, monkeypatch):  # noqa: F811
    repo, owner, other = database
    monkeypatch.setattr(repository_module, "threatened_species", lambda: frozenset({"Vultur gryphus"}))
    point = {"latitude": 4.6789, "longitude": -74.1234}
    condor = DetectionInput.model_validate(detection(species="Vultur gryphus", location=point))
    thrush = DetectionInput.model_validate(detection(species="Turdus fuscater", location=point))
    repo.batch(owner, [condor, thrush])
    area = MapQuery(west=-74.2, south=4.6, east=-74.0, north=4.8)
    seen = {row.species: row for row in repo.map(other, area).detections if row.id in (condor.id, thrush.id)}
    assert (seen["Vultur gryphus"].latitude, seen["Vultur gryphus"].longitude) == (4.65, -74.15)
    assert seen["Vultur gryphus"].generalized_m == THREATENED_CELL_METERS
    assert (seen["Turdus fuscater"].latitude, seen["Turdus fuscater"].generalized_m) == (4.6789, None)
    # Its author always sees it where it was heard.
    own = {row.species: row for row in repo.map(owner, area).detections if row.id in (condor.id, thrush.id)}
    assert (own["Vultur gryphus"].latitude, own["Vultur gryphus"].generalized_m) == (4.6789, None)
    # A small area around the real point does not find the threatened one; only the cell centre does.
    around = MapQuery(west=-74.124, south=4.678, east=-74.123, north=4.679)
    assert [row.id for row in repo.map(other, around).detections if row.id in (condor.id, thrush.id)] == [thrush.id]
    centre = MapQuery(west=-74.151, south=4.649, east=-74.149, north=4.651)
    assert [row.id for row in repo.map(other, centre).detections if row.id in (condor.id, thrush.id)] == [condor.id]


def test_walk_paths_carry_track_and_places_page_by_page(database):  # noqa: F811
    repo, owner, other = database
    walks = WalkRepository()
    first = WalkInput.model_validate({"started_at": "2026-10-06T11:00:00Z", "ended_at": "2026-10-06T12:00:00Z",
                                      "track": [[4.679, -74.123, 0], [4.6795, -74.1235, 600]]})
    later = WalkInput.model_validate({"started_at": "2026-10-07T11:00:00Z", "ended_at": "2026-10-07T12:00:00Z", "track": [[4.7, -74.1, 0]]})
    first_id, later_id = uuid4(), uuid4()
    walks.save(owner, first_id, first, quota=10)
    walks.save(owner, later_id, later, quota=10)
    repo.batch(owner, [DetectionInput.model_validate(detection(recorded_at="2026-10-06T11:30:00Z"))])
    page = walks.paths(owner, None, 1)
    assert [walk.id for walk in page.walks] == [later_id] and page.has_more
    rest = walks.paths(owner, page.walks[0].started_at, 10)
    assert [walk.id for walk in rest.walks] == [first_id] and not rest.has_more
    assert len(rest.walks[0].track) == 2 and [(p.species, p.detections) for p in rest.walks[0].places] == [("Turdus fuscater", 1)]
    assert walks.paths(other, None, 10).walks == []


def test_own_located_songs_page_by_page_without_discarded_or_foreign(database):  # noqa: F811
    from birdnet_api.records import RecordRepository

    repo, owner, other = database
    rows = [DetectionInput.model_validate(detection(recorded_at=f"2026-10-0{day}T12:00:00Z")) for day in (1, 2, 3)]
    repo.batch(owner, rows)
    repo.batch(other, [DetectionInput.model_validate(detection())])
    with repo.transaction(owner) as connection:
        connection.execute("UPDATE public.detections SET estado='descartada' WHERE id=%s", (rows[1].id,))
    records = RecordRepository()
    first = records.located(owner, None, 1)
    assert [row.id for row in first.detections] == [rows[2].id] and first.has_more
    rest = records.located(owner, first.detections[0].recorded_at, 10)
    assert [row.id for row in rest.detections] == [rows[0].id] and not rest.has_more
    assert (rest.detections[0].latitude, rest.detections[0].longitude) == (4.679, -74.123)
