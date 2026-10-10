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


def test_locations_are_stored_on_the_location_grid(database):
    from birdnet_api.contracts import SiteInput
    from birdnet_api.domain import LOCATION_GRID_DECIMALS
    from birdnet_api.sites import SiteRepository

    repo, owner, _ = database
    fine = {"latitude": 4.6789, "longitude": -74.1234}
    row = DetectionInput.model_validate(detection(location=fine))
    repo.batch(owner, [row])
    site = SiteRepository().create_site(owner, SiteInput(name="Humedal", location=fine))
    assert (site.latitude, site.longitude) == (4.6789, -74.1234)
    stored = "SELECT ST_Y(ubicacion::geometry),ST_X(ubicacion::geometry) FROM public.detections WHERE id=%s"
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        assert connection.execute(stored, (row.id,)).fetchone() == (4.6789, -74.1234)
        # The trigger is the last line of defence: a raw reading written past the API is still rounded.
        raw = (4.678912, -74.123456)
        connection.execute("UPDATE public.detections SET ubicacion=ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography WHERE id=%s", (raw[1], raw[0], row.id))
        assert connection.execute(stored, (row.id,)).fetchone() == tuple(round(value, LOCATION_GRID_DECIMALS) for value in raw)


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


def test_sites_stats_and_batch_site_ownership(database):
    from datetime import UTC, datetime, timedelta

    from birdnet_api.contracts import SiteInput
    from birdnet_api.sites import SiteRepository

    repo, owner, other = database
    sites = SiteRepository()
    mine = sites.create_site(owner, SiteInput(name="Finca", location={"latitude": 4.679, "longitude": -74.123}))
    assert sites.list_sites(other) == []
    foreign = DetectionInput.model_validate(detection(site_id=str(mine.id)))
    with pytest.raises(HTTPException) as error:
        repo.batch(other, [foreign])
    assert error.value.status_code == 422
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    rows = [detection(site_id=str(mine.id), species="Turdus fuscater", recorded_at=(now - timedelta(days=2)).isoformat()),
            detection(site_id=str(mine.id), species="Turdus fuscater", recorded_at=(now - timedelta(days=40)).isoformat()),
            detection(site_id=str(mine.id), species="Sturnella magna", recorded_at=(now - timedelta(days=45)).isoformat()),
            detection(site_id=str(mine.id), species="Pyrocephalus rubinus", recorded_at=(now - timedelta(days=1)).isoformat())]
    repo.batch(owner, [DetectionInput.model_validate(r) for r in rows])
    stats = sites.stats(owner, mine.id, "month", now - timedelta(days=30), now - timedelta(days=60), now, "America/Bogota")
    assert stats.species_count == 2 and stats.previous_species_count == 2 and stats.detections == 2
    assert {s.species: s.is_new for s in stats.species} == {"Turdus fuscater": False, "Pyrocephalus rubinus": True}
    assert stats.missing == ["Sturnella magna"] and sum(stats.hourly) == 2
    with pytest.raises(HTTPException) as error:
        sites.stats(other, mine.id, "all", None, None, now, "UTC")
    assert error.value.status_code == 404
    body, truncated = sites.export_csv(owner, mine.id, None, None)
    assert body.count("\n") == 5 and not truncated


def test_profiles_are_private_and_bounded(database):
    repo, owner, other = database
    with repo.transaction(other) as connection:
        assert connection.execute("SELECT 1 FROM public.profiles WHERE id=%s", (owner,)).fetchone() is None
        assert connection.execute("UPDATE public.profiles SET alias='forged' WHERE id=%s", (owner,)).rowcount == 0
    for column, value in (("alias", "x" * 41), ("alias", ""), ("avatar_path", f"{other}/avatar.webp"), ("avatar_path", f"{owner}/other.webp")):
        with pytest.raises(HTTPException) as error, repo.transaction(owner) as connection:
            connection.execute(psycopg.sql.SQL("UPDATE public.profiles SET {}=%s WHERE id=%s").format(psycopg.sql.Identifier(column)), (value, owner))
        assert error.value.status_code == 503
    with repo.transaction(owner) as connection:
        assert connection.execute("UPDATE public.profiles SET avatar_path=%s WHERE id=%s", (f"{owner}/avatar.webp", owner)).rowcount == 1


def test_sign_up_bounds_the_default_alias():
    user = uuid4()
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        connection.execute("INSERT INTO auth.users(id,email) VALUES (%s,%s)", (user, f"{'a' * 60}@example.invalid"))
        assert connection.execute("SELECT alias FROM public.profiles WHERE id=%s", (user,)).fetchone()[0] == "a" * 40


def test_profile_upsert_runs_under_rls(database):
    from birdnet_api.profiles import EMPTY_PROFILE, ProfileRepository

    _, owner, _ = database
    profiles = ProfileRepository()
    # The sign-up trigger already created the row with the e-mail's local part.
    assert profiles.get(owner).alias == str(owner)
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        connection.execute("DELETE FROM public.profiles WHERE id=%s", (owner,))
    assert profiles.get(owner) == EMPTY_PROFILE
    created = profiles.update(owner, {"alias": "Ana"})
    assert created.alias == "Ana" and created.created_at is not None
    updated = profiles.update(owner, {"avatar_path": f"{owner}/avatar.webp"})
    assert (updated.alias, updated.avatar_path) == ("Ana", f"{owner}/avatar.webp")


def test_personal_record_counts_only_own_kept_detections(database):
    from datetime import UTC, datetime

    from birdnet_api.contracts import SiteInput
    from birdnet_api.records import RecordRepository
    from birdnet_api.sites import SiteRepository

    repo, owner, other = database
    site = SiteRepository().create_site(owner, SiteInput(name="Humedal", location={"latitude": 4.7, "longitude": -74.1}))
    # 23:30 UTC on the 6th is 18:30 on the 6th in Bogotá; 03:00 UTC on the 7th is still the 6th there.
    mine = [detection(species="Turdus fuscater", recorded_at="2026-10-06T23:30:00Z", site_id=str(site.id), confidence=0.6, status="provisional"),
            detection(species="Turdus fuscater", recorded_at="2026-10-07T03:00:00Z", confidence=0.95),
            detection(species="Zonotrichia capensis", recorded_at="2026-10-05T12:00:00Z")]
    discarded = detection(species="Sturnella magna", recorded_at="2026-10-08T12:00:00Z")
    repo.batch(owner, [DetectionInput.model_validate(r) for r in [*mine, discarded]])
    repo.batch(other, [DetectionInput.model_validate(detection(species="Turdus fuscater"))])
    with repo.transaction(owner) as connection:
        connection.execute("UPDATE public.detections SET estado='descartada' WHERE id=%s", (discarded["id"],))
    records = RecordRepository()
    summary = records.summary(owner, "America/Bogota")
    assert (summary.detections, summary.species, summary.sites, summary.active_days) == (3, 2, 1, 2)
    assert summary.last_recorded_at == datetime(2026, 10, 7, 3, tzinfo=UTC)
    assert [s.species for s in records.species(owner).species] == ["Turdus fuscater", "Zonotrichia capensis"]
    thrush = records.species_record(owner, "Turdus fuscater", "America/Bogota")
    assert thrush.detections == 2 and round(thrush.best_confidence, 2) == 0.95
    assert thrush.hours[18] == 1 and thrush.hours[22] == 1 and sum(thrush.hours) == 2
    assert [(s.name, s.detections) for s in thrush.sites] == [("Humedal", 1)]
    assert sum(c.detections for c in thrush.cells) == 2
    assert [str(r.id) for r in thrush.recent] == [mine[1]["id"], mine[0]["id"]] and not thrush.recent[0].has_audio
    assert records.species_record(owner, "Sturnella magna", "UTC").model_dump(include={"detections", "best_confidence", "recent", "sites", "cells"}) == \
        {"detections": 0, "best_confidence": None, "recent": [], "sites": [], "cells": []}
    assert records.summary(other, "UTC").sites == 0


def test_map_marks_own_rows_and_hides_foreign_sites(database):
    from birdnet_api.contracts import SiteInput
    from birdnet_api.sites import SiteRepository

    repo, owner, other = database
    site = SiteRepository().create_site(owner, SiteInput(name="Finca privada", location={"latitude": 4.679, "longitude": -74.123}))
    row = DetectionInput.model_validate(detection(site_id=str(site.id)))
    repo.batch(owner, [row])
    area = MapQuery(west=-74.2, south=4.6, east=-74.0, north=4.7, species=row.species)

    def seen_by(viewer):
        return next(r for r in repo.map(viewer, area).detections if r.id == row.id)

    assert (seen_by(owner).own, seen_by(owner).site_name) == (True, "Finca privada")
    assert (seen_by(other).own, seen_by(other).site_name) == (False, None)
