"""Database boundaries for private beta metadata, server versions and minimal deletion marks."""
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException

from birdnet_api.contracts import DetectionInput, SiteInput, WalkInput
from birdnet_api.session import SECOND_FACTOR, session_assurance
from birdnet_api.sites import SiteRepository
from birdnet_api.walks import WalkRepository
from tests.prepare_database import TEST_DATABASE_URL
from tests.test_api import detection
from tests.test_postgres import database, pytestmark  # noqa: F401


def create_outing(connection, owner, identifier=None):
    identifier = identifier or uuid4()
    connection.execute("INSERT INTO public.outings(id,owner_id,kind,started_at) VALUES (%s,%s,'stationary','2026-11-14T11:00:00Z')", (identifier, owner))
    return identifier


def test_beta_foreign_references_fail_even_outside_rls(database):  # noqa: F811
    repo, owner, other = database
    item = DetectionInput.model_validate(detection())
    repo.batch(other, [item])
    with psycopg.connect(TEST_DATABASE_URL) as c:
        mine = create_outing(c, owner)
        theirs = create_outing(c, other)
    for statement, values in (
        ("INSERT INTO public.outing_detections(owner_id,outing_id,detection_id) VALUES (%s,%s,%s)", (owner, mine, item.id)),
        ("INSERT INTO public.observations(id,owner_id,species,observed_at,outing_id) VALUES (%s,%s,'Turdus fuscater',now(),%s)", (uuid4(), owner, theirs)),
    ):
        with psycopg.connect(TEST_DATABASE_URL) as c, pytest.raises(psycopg.errors.ForeignKeyViolation):
            c.execute(statement, values)
    with repo.transaction(other) as c:
        assert c.execute("SELECT * FROM public.outings WHERE id=%s", (mine,)).fetchone() is None


def test_ledger_cannot_be_forged_and_delete_retry_cannot_resurrect(database):  # noqa: F811
    repo, owner, _ = database
    with repo.transaction(owner) as c:
        identifier = create_outing(c, owner)
        assert c.execute("SELECT version FROM public.outings WHERE id=%s", (identifier,)).fetchone()["version"] == 1
        c.execute("UPDATE public.outings SET title='Changed',version=700 WHERE id=%s", (identifier,))
        assert c.execute("SELECT version FROM public.outings WHERE id=%s", (identifier,)).fetchone()["version"] == 2
        assert not c.execute("SELECT private.delete_beta_entity('outing',%s,1) AS deleted", (identifier,)).fetchone()["deleted"]
        assert c.execute("SELECT private.delete_beta_entity('outing',%s,2) AS deleted", (identifier,)).fetchone()["deleted"]
        assert c.execute("SELECT private.delete_beta_entity('outing',%s,2) AS deleted", (identifier,)).fetchone()["deleted"]
        mark = c.execute("SELECT * FROM public.beta_deletions WHERE entity_id=%s", (identifier,)).fetchone()
        assert set(mark) == {"owner_id", "entity_kind", "entity_id", "version", "deleted_at"}
        assert mark["version"] == 3
    with pytest.raises(HTTPException), repo.transaction(owner) as c:
        create_outing(c, owner, identifier)
    for command in (
        "INSERT INTO public.beta_deletions(owner_id,entity_kind,entity_id,version) VALUES (%s,'outing',%s,1)",
        "UPDATE public.beta_deletions SET version=1 WHERE owner_id=%s AND entity_id=%s",
        "DELETE FROM public.beta_deletions WHERE owner_id=%s AND entity_id=%s",
    ):
        with pytest.raises(HTTPException), repo.transaction(owner) as c:
            c.execute(command, (owner, identifier))


def test_cancelled_creation_is_remembered_without_private_content(database):  # noqa: F811
    repo, owner, _ = database
    identifier = uuid4()
    with repo.transaction(owner) as c:
        assert c.execute("SELECT private.delete_beta_entity('observation',%s,1) AS deleted", (identifier,)).fetchone()["deleted"]
    with pytest.raises(HTTPException), repo.transaction(owner) as c:
        c.execute("INSERT INTO public.observations(id,owner_id,species,observed_at) VALUES (%s,%s,'Turdus fuscater',now())", (identifier, owner))


def test_profile_cascade_removes_entities_and_tombstones_without_recreating_marks(database):  # noqa: F811
    repo, owner, _ = database
    with repo.transaction(owner) as c:
        first, second = create_outing(c, owner), create_outing(c, owner)
        c.execute("SELECT private.delete_beta_entity('outing',%s,1)", (first,))
        c.execute("INSERT INTO public.collections(id,owner_id,name,note) VALUES (%s,%s,'Private name','Private note')", (uuid4(), owner))
        c.execute("INSERT INTO public.observations(id,owner_id,species,observed_at,outing_id) VALUES (%s,%s,'Turdus fuscater',now(),%s)", (uuid4(), owner, second))
    with psycopg.connect(TEST_DATABASE_URL) as c:
        c.execute("DELETE FROM auth.users WHERE id=%s", (owner,))
        for name in ("outings", "observations", "collections", "beta_deletions"):
            assert c.execute(psycopg.sql.SQL("SELECT count(*) FROM public.{} WHERE owner_id=%s").format(psycopg.sql.Identifier(name)), (owner,)).fetchone()[0] == 0


def test_reference_deletions_only_clear_links_and_advance_versions(database):  # noqa: F811
    repo, owner, _ = database
    sites, walks = SiteRepository(), WalkRepository()
    site = sites.create_site(owner, SiteInput.model_validate({"name": "Garden", "location": {"latitude": 4.6789, "longitude": -74.1234}}))
    walk = walks.save(owner, uuid4(), WalkInput.model_validate({"started_at": "2026-11-14T11:00:00Z", "ended_at": "2026-11-14T12:00:00Z", "track": []}), quota=5)
    outing_id, observation_id = uuid4(), uuid4()
    with repo.transaction(owner) as c:
        c.execute("INSERT INTO public.outings(id,owner_id,kind,started_at,site_id,walk_id) VALUES (%s,%s,'walk',now(),%s,%s)", (outing_id, owner, site.id, walk.id))
        c.execute("INSERT INTO public.observations(id,owner_id,species,observed_at,outing_id) VALUES (%s,%s,'Turdus fuscater',now(),%s)", (observation_id, owner, outing_id))
    sites.delete(owner, site.id)
    walks.delete(owner, walk.id)
    with repo.transaction(owner) as c:
        row = c.execute("SELECT owner_id,site_id,walk_id,version FROM public.outings WHERE id=%s", (outing_id,)).fetchone()
        assert row == {"owner_id": owner, "site_id": None, "walk_id": None, "version": 3}
        assert c.execute("SELECT private.delete_beta_entity('outing',%s,3) AS deleted", (outing_id,)).fetchone()["deleted"]
        row = c.execute("SELECT owner_id,outing_id,version FROM public.observations WHERE id=%s", (observation_id,)).fetchone()
        assert row == {"owner_id": owner, "outing_id": None, "version": 2}


def test_narrow_deletion_function_requires_the_second_factor(database):  # noqa: F811
    repo, owner, _ = database
    with repo.transaction(owner) as c:
        identifier = create_outing(c, owner)
    with psycopg.connect(TEST_DATABASE_URL) as c:
        c.execute("INSERT INTO auth.mfa_factors(user_id,status) VALUES (%s,'verified')", (owner,))
    with pytest.raises(HTTPException), repo.transaction(owner) as c:
        c.execute("SELECT private.delete_beta_entity('outing',%s,1)", (identifier,))
    token = session_assurance.set(SECOND_FACTOR)
    try:
        with repo.transaction(owner) as c:
            assert c.execute("SELECT private.delete_beta_entity('outing',%s,1) AS deleted", (identifier,)).fetchone()["deleted"]
    finally:
        session_assurance.reset(token)
