"""Repeat the idempotency and ownership checks against actual PostgreSQL and RLS."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException

from birdnet_api.contracts import DetectionInput, MapQuery
from birdnet_api.repository import DetectionRepository
from tests.prepare_database import TEST_DATABASE_URL
from tests.test_api import detection

pytestmark = pytest.mark.skipif(os.environ.get("BIRDNET_TEST_DATABASE_URL") != TEST_DATABASE_URL, reason="Isolated local PostgreSQL fixture not enabled")


@pytest.fixture
def database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    owner, other = uuid4(), uuid4()
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        for user in (owner, other):
            connection.execute("INSERT INTO auth.users(id,email) VALUES (%s,%s)", (user, f"{user}@example.invalid"))
    return DetectionRepository(), owner, other


def test_postgres_concurrent_retries_do_not_duplicate(database):
    repo, owner, _ = database
    rows = [DetectionInput.model_validate(detection()) for _ in range(50)]
    first = repo.batch(owner, rows)
    assert len(first.accepted_ids) == 50
    with ThreadPoolExecutor(max_workers=8) as pool:
        retries = list(pool.map(lambda _: repo.batch(owner, rows), range(20)))
    assert all(len(response.existing_ids) == 50 for response in retries)
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        assert connection.execute("SELECT count(*) FROM public.detections WHERE user_id=%s", (owner,)).fetchone()[0] == 50


def test_foreign_collision_rolls_back_the_whole_batch(database):
    repo, owner, other = database
    original = DetectionInput.model_validate(detection())
    repo.batch(owner, [original])
    with pytest.raises(HTTPException) as error:
        repo.batch(other, [DetectionInput.model_validate(detection()), original])
    assert error.value.status_code == 409
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        assert connection.execute("SELECT count(*) FROM public.detections WHERE user_id=%s", (other,)).fetchone()[0] == 0


def test_rls_and_location_precision_are_enforced(database):
    repo, owner, other = database
    row = DetectionInput.model_validate(detection())
    repo.batch(owner, [row])
    assert repo.get_owned(other, row.id) is None
    with repo.transaction(other) as connection:
        assert connection.execute("UPDATE public.detections SET especie='forged' WHERE id=%s", (row.id,)).rowcount == 0
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        tables = connection.execute("SELECT relname,relrowsecurity FROM pg_class JOIN pg_namespace ON pg_class.relnamespace=pg_namespace.oid WHERE nspname='public' AND relname IN ('profiles','sites','detections','verification_jobs','model_versions')").fetchall()
        assert len(tables) == 5 and all(enabled for _, enabled in tables)
        location = connection.execute("SELECT ST_Y(ubicacion::geometry),ST_X(ubicacion::geometry) FROM public.detections WHERE id=%s", (row.id,)).fetchone()
        assert location == (4.679, -74.123)


def test_audio_job_is_created_once(database):
    repo, owner, _ = database
    row = DetectionInput.model_validate(detection(confidence=0.6, status="provisional"))
    repo.batch(owner, [row])
    row.audio_path = f"{owner}/{row.id}.wav"
    for _ in range(5):
        repo.batch(owner, [row])
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        assert connection.execute("SELECT count(*) FROM public.verification_jobs WHERE detection_id=%s", (row.id,)).fetchone()[0] == 1


def test_map_is_collective_filtered_and_hides_discarded(database):
    repo, owner, other = database
    bogota = DetectionInput.model_validate(detection(species="Turdus fuscater"))
    hidden = DetectionInput.model_validate(detection(species="Zonotrichia capensis"))
    repo.batch(owner, [bogota, hidden])
    with repo.transaction(owner) as connection:
        connection.execute("UPDATE public.detections SET estado='descartada' WHERE id=%s", (hidden.id,))
    area = {"west": bogota.location.longitude - 0.01, "south": bogota.location.latitude - 0.01, "east": bogota.location.longitude + 0.01, "north": bogota.location.latitude + 0.01}
    visible = repo.map(other, MapQuery(**area))
    ids = {row.id for row in visible.detections}
    assert bogota.id in ids and hidden.id not in ids
    assert "user_id" not in visible.detections[0].model_dump()
    assert repo.map(other, MapQuery(**area, species="Other species")).detections == []
    elsewhere = repo.map(other, MapQuery(west=10, south=10, east=11, north=11))
    assert bogota.id not in {row.id for row in elsewhere.detections}
