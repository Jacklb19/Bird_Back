"""Repeat the idempotency and ownership checks against actual PostgreSQL and RLS."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException

from birdnet_api.contracts import DetectionInput, MapQuery
from birdnet_api.exports import ExportRepository
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
    body, truncated = ExportRepository().export_csv(owner, mine.id, None, None, None)
    assert body.count("\n") == 5 and not truncated
    with pytest.raises(HTTPException) as error:
        ExportRepository().export_csv(other, mine.id, None, None, None)
    assert error.value.status_code == 404


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
    assert [str(r.id) for r in thrush.recent] == [mine[1]["id"], mine[0]["id"]]
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


def test_unshared_detections_reach_only_their_author(database):
    from datetime import UTC, datetime

    from birdnet_api.contracts import SiteInput
    from birdnet_api.records import RecordRepository
    from birdnet_api.sites import SiteRepository

    repo, owner, other = database
    sites = SiteRepository()
    site = sites.create_site(owner, SiteInput(name="Jardín", location={"latitude": 4.679, "longitude": -74.123}))
    kept = DetectionInput.model_validate(detection(site_id=str(site.id), shared=False))
    shared = DetectionInput.model_validate(detection())
    theirs = DetectionInput.model_validate(detection())
    repo.batch(owner, [kept, shared])
    repo.batch(other, [theirs])
    area = MapQuery(west=-74.2, south=4.6, east=-74.0, north=4.7)

    def on_map(viewer):
        return {row.id for row in repo.map(viewer, area).detections} & {kept.id, shared.id, theirs.id}

    def readable(viewer):
        with repo.transaction(viewer) as connection:
            rows = connection.execute("SELECT id FROM public.detections WHERE id = ANY(%s)", ([kept.id, shared.id, theirs.id],)).fetchall()
        return {row["id"] for row in rows}

    # Hidden from other people on the map and by RLS itself, whatever query reaches the table.
    assert on_map(other) == readable(other) == {shared.id, theirs.id}
    assert on_map(owner) == readable(owner) == {kept.id, shared.id, theirs.id}
    # The author keeps everything: map marker, site statistics, export and personal record.
    assert next(row.own for row in repo.map(owner, area).detections if row.id == kept.id)
    assert sites.stats(owner, site.id, "all", None, None, datetime(2026, 10, 10, tzinfo=UTC), "UTC").detections == 1
    assert str(kept.id) in ExportRepository().export_csv(owner, site.id, None, None, None)[0]
    # The whole record exports only the caller's own rows, never shared ones of other people.
    everything = ExportRepository().export_csv(owner, None, None, None, None)[0]
    assert str(kept.id) in everything and str(shared.id) in everything and str(theirs.id) not in everything
    assert RecordRepository().summary(owner, "UTC").detections == 2

    # The switch changes every row of the caller and nobody else's.
    assert repo.set_sharing(other, False) == 1
    assert on_map(owner) == readable(owner) == {kept.id, shared.id}
    assert on_map(other) == {shared.id, theirs.id}
    assert (repo.set_sharing(owner, True), repo.set_sharing(owner, True)) == (1, 0)
    assert on_map(other) == readable(other) == {kept.id, shared.id, theirs.id}
    assert repo.set_sharing(owner, False) == 2
    assert on_map(other) == readable(other) == {theirs.id}
    # A queue retry that still says "shared" must not undo the choice.
    shared.shared = True
    assert repo.batch(owner, [shared]).existing_ids == [shared.id]
    assert on_map(other) == {theirs.id}


def test_review_keeps_the_model_species_and_survives_a_retry(database):
    from birdnet_api.domain import ReviewAction
    from birdnet_api.records import RecordRepository

    repo, owner, other = database
    row = DetectionInput.model_validate(detection(species="Turdus fuscater"))
    repo.batch(owner, [row])
    assert repo.review(other, row.id, ReviewAction.DISCARD, None) is None
    corrected = repo.review(owner, row.id, ReviewAction.CORRECT, "Zonotrichia capensis")
    assert (corrected.species, corrected.model_species, corrected.status, corrected.reviewed_by) == ("Zonotrichia capensis", "Turdus fuscater", "corrected", "person")
    # A second correction still remembers what the model said, not the first correction.
    again = repo.review(owner, row.id, ReviewAction.CORRECT, "Troglodytes aedon")
    assert (again.species, again.model_species) == ("Troglodytes aedon", "Turdus fuscater")
    # The phone retries the batch with the model's species: it is the same detection, not an identifier conflict.
    assert repo.batch(owner, [row]).existing_ids == [row.id]
    # Correcting back to the model's species is a confirmation.
    back = repo.review(owner, row.id, ReviewAction.CORRECT, "Turdus fuscater")
    assert (back.species, back.model_species, back.status) == ("Turdus fuscater", None, "verified")
    repo.review(owner, row.id, ReviewAction.CORRECT, "Zonotrichia capensis")
    confirmed = repo.review(owner, row.id, ReviewAction.CONFIRM, None)
    assert (confirmed.species, confirmed.model_species, confirmed.status) == ("Turdus fuscater", None, "verified")
    assert RecordRepository().species_record(owner, "Turdus fuscater", "UTC").recent[0].reviewed_by == "person"
    discarded = repo.review(owner, row.id, ReviewAction.DISCARD, None)
    assert (discarded.species, discarded.status) == ("Turdus fuscater", "discarded")
    assert RecordRepository().species_record(owner, "Turdus fuscater", "UTC").detections == 0


def test_recordings_respect_quota_and_ownership(database):
    from birdnet_api.recordings import RecordingRepository
    from birdnet_api.records import RecordRepository

    repo, owner, other = database
    saved = RecordingRepository()
    rows = [DetectionInput.model_validate(detection()) for _ in range(2)]
    repo.batch(owner, rows)
    first, second = (f"{owner}/{row.id}.webm" for row in rows)
    saved.link(owner, rows[0].id, first, 600, 1000)
    assert saved.has_room(owner, rows[1].id, 400, 1000) and not saved.has_room(owner, rows[1].id, 401, 1000)
    with pytest.raises(HTTPException) as error:
        saved.link(owner, rows[1].id, second, 401, 1000)
    assert error.value.status_code == 507
    # Replacing a detection's own clip does not count it twice.
    saved.link(owner, rows[0].id, first, 1000, 1000)
    assert saved.usage(owner) == (1, 1000)
    assert [row.has_recording for row in RecordRepository().species_record(owner, rows[0].species, "UTC").recent].count(True) == 1
    # Another account neither links to the detection nor sees its clip.
    with pytest.raises(HTTPException) as error:
        saved.link(other, rows[0].id, f"{other}/{rows[0].id}.webm", 10, 1000)
    assert error.value.status_code == 404
    assert saved.path(other, rows[0].id) is None and saved.usage(other) == (0, 0) and saved.unlink_all(other, [first]) == 0
    # The constraint is the last line of defence: a clip can only live in its owner's folder, with its size.
    with psycopg.connect(TEST_DATABASE_URL) as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute("UPDATE public.detections SET ruta_grabacion=%s, bytes_grabacion=10 WHERE id=%s", (f"{other}/{rows[1].id}.webm", rows[1].id))
    assert saved.unlink_all(owner, saved.paths(owner)) == 1
    assert saved.usage(owner) == (0, 0)


def test_walks_are_private_idempotent_and_counted_from_reviewed_songs(database):
    from birdnet_api.contracts import WalkInput
    from birdnet_api.domain import ReviewAction
    from birdnet_api.walks import WalkRepository

    repo, owner, other = database
    walks = WalkRepository()
    walk_id = uuid4()
    walk = WalkInput.model_validate({"name": "Humedal", "started_at": "2026-10-06T11:00:00Z", "ended_at": "2026-10-06T13:00:00Z",
                                     "track": [[4.679, -74.123, 0], [4.680, -74.123, 300], [4.680, -74.124, 900]]})
    heard = [detection(recorded_at="2026-10-06T11:30:00Z"), detection(recorded_at="2026-10-06T12:00:00Z"),
             detection(species="Sturnella magna", recorded_at="2026-10-06T12:10:00Z"),
             detection(species="Pyrocephalus rubinus", recorded_at="2026-10-06T12:20:00Z", location={"latitude": 4.68, "longitude": -74.124})]
    before = detection(recorded_at="2026-10-06T10:59:59Z")
    repo.batch(owner, [DetectionInput.model_validate(row) for row in [*heard, before]])
    repo.batch(other, [DetectionInput.model_validate(detection(recorded_at="2026-10-06T12:00:00Z"))])
    repo.review(owner, heard[2]["id"], ReviewAction.DISCARD, None)
    repo.review(owner, heard[3]["id"], ReviewAction.CORRECT, "Zonotrichia capensis")

    saved = walks.save(owner, walk_id, walk, quota=1)
    assert saved.points == 3 and saved.distance_m == pytest.approx(111.195 + 110.85, abs=0.5)
    # Discarded songs and other people's never count; a correction counts as the bird the person heard.
    assert saved.detections == 3
    assert [(s.species, s.detections) for s in saved.species] == [("Turdus fuscater", 2), ("Zonotrichia capensis", 1)]
    assert walks.save(owner, walk_id, walk, quota=1) == saved
    detail = walks.detail(owner, walk_id)
    assert detail.track == walk.track
    assert {(p.species, p.latitude, p.longitude, p.detections) for p in detail.places} == {
        ("Turdus fuscater", 4.679, -74.123, 2), ("Zonotrichia capensis", 4.68, -74.124, 1)}

    # The quota refuses a new walk, never the same one again.
    with pytest.raises(HTTPException) as error:
        walks.save(owner, uuid4(), walk, quota=1)
    assert error.value.status_code == 507
    # Another person can neither take the identifier nor read, change or delete the walk, even past the API.
    with pytest.raises(HTTPException) as error:
        walks.save(other, walk_id, walk, quota=10)
    assert error.value.status_code == 409
    assert walks.detail(other, walk_id) is None and walks.page(other, None, 10).walks == [] and walks.rename(other, walk_id, "Mía") is None
    with repo.transaction(other) as connection:
        assert connection.execute("SELECT count(*) AS n FROM public.walks").fetchone()["n"] == 0
        assert connection.execute("UPDATE public.walks SET nombre='forged' WHERE id=%s", (walk_id,)).rowcount == 0
        assert connection.execute("DELETE FROM public.walks WHERE id=%s", (walk_id,)).rowcount == 0
    walks.delete(other, walk_id)
    assert walks.usage(owner) == 1

    # The walk's CSV holds its period only, discarded songs included with their state.
    body, _ = ExportRepository().export_csv(owner, None, walk_id, None, None)
    assert body.count("\n") == 5 and "discarded" in body and before["id"] not in body
    with pytest.raises(HTTPException) as error:
        ExportRepository().export_csv(other, None, walk_id, None, None)
    assert error.value.status_code == 404

    assert walks.rename(owner, walk_id, "Al alba").name == "Al alba"
    assert walks.rename(owner, walk_id, None).name is None
    assert [w.id for w in walks.page(owner, None, 10).walks] == [walk_id]
    assert walks.page(owner, walk.started_at, 10).walks == []
    # Deleting the walk keeps its songs.
    assert walks.delete_all(owner) == 1 and walks.usage(owner) == 0
    assert stored_detections(owner) == 5


def stored_detections(owner):
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        return connection.execute("SELECT count(*) FROM public.detections WHERE user_id=%s", (owner,)).fetchone()[0]


def test_walk_rows_reject_paths_beyond_their_bounds(database):
    repo, owner, _ = database
    for name, path in (("x" * 81, "[]"), ("Bien", '{"a": 1}'), ("Bien", "[" + ",".join(["[4.6,-74.1]"] * 2001) + "]")):
        with pytest.raises(HTTPException), repo.transaction(owner) as connection:
            connection.execute("INSERT INTO public.walks (id,user_id,nombre,inicio,fin,recorrido) VALUES (%s,%s,%s,now(),now(),%s::jsonb)",
                               (uuid4(), owner, name, path))
    with pytest.raises(HTTPException), repo.transaction(owner) as connection:
        connection.execute("INSERT INTO public.walks (id,user_id,nombre,inicio,fin) VALUES (%s,%s,'Bien',now(),now() - interval '1 second')",
                           (uuid4(), owner))
