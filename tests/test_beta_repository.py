"""Exercise the beta synchronization contract against PostgreSQL with real owner policies."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from birdnet_api.app import app
from birdnet_api.auth import authenticate
from birdnet_api.beta.contracts import (
    CollectionInput, DeletionQuery, DescendingQuery, EventQuery, ObservationInput, ObservationQuery,
    OutingInput, WishlistQuery,
)
from birdnet_api.beta.repository import JournalRepository, OrganizationRepository
from birdnet_api.contracts import DetectionInput
from birdnet_api.domain import ReviewAction
from tests.test_api import detection
from tests.test_postgres import database, pytestmark  # noqa: F401


def outing(**changes):
    return OutingInput.model_validate({"title": "Garden visit", "kind": "stationary", "started_at": "2026-11-14T11:00:00Z",
        "ended_at": None, "note": None, "site_id": None, "walk_id": None, **changes})


def observation(**changes):
    return ObservationInput.model_validate({"species": "Turdus fuscater", "observed_at": "2026-11-14T11:00:00Z",
        "location": {"latitude": 4.6789, "longitude": -74.1234}, "individuals": 2, "note": None, "outing_id": None, **changes})


def test_concurrent_creation_and_canonical_retries_preserve_one_version(database):  # noqa: F811
    _, owner, _ = database
    repo, identifier = JournalRepository(), uuid4()
    payload = outing(title=" Garden   visit ", note="  ")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: repo.save_outing(owner, identifier, payload), range(12)))
    assert all(row.version == 1 and row.id == identifier for row in results)
    assert len({row.created_at for row in results}) == 1
    assert repo.save_outing(owner, identifier, outing()).version == 1
    assert len(repo.outings(owner, DescendingQuery()).outings) == 1
    visual = repo.save_observation(owner, uuid4(), observation())
    retry = repo.save_observation(owner, visual.id, observation(location={"latitude": 4.678900, "longitude": -74.123400}))
    assert retry == visual and retry.source == "manual"
    assert "confidence" not in retry.model_dump()


def test_stale_update_conflicts_but_identical_acknowledgement_can_be_retried(database):  # noqa: F811
    _, owner, _ = database
    repo, identifier = JournalRepository(), uuid4()
    repo.save_outing(owner, identifier, outing())
    revised = repo.save_outing(owner, identifier, outing(title="Wetland", expected_version=1))
    assert revised.version == 2
    assert repo.save_outing(owner, identifier, outing(title="Wetland", expected_version=1)) == revised
    with pytest.raises(HTTPException) as error:
        repo.save_outing(owner, identifier, outing(title="Another edit", expected_version=1))
    assert error.value.status_code == 409
    assert repo.outing(owner, identifier) == revised
    with pytest.raises(HTTPException) as error:
        repo.remove(owner, "outing", identifier, 1)
    assert error.value.status_code == 409


def test_concurrent_distinct_updates_can_accept_only_one_expected_version(database):  # noqa: F811
    _, owner, _ = database
    repo, identifier = JournalRepository(), uuid4()
    repo.save_outing(owner, identifier, outing())

    def update(title):
        try:
            return repo.save_outing(owner, identifier, outing(title=title, expected_version=1)).version
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ("First edit", "Second edit")))
    assert sorted(results) == [2, 409]
    assert repo.outing(owner, identifier).version == 2


def test_lost_create_then_delete_cannot_be_resurrected_by_a_delayed_queue(database):  # noqa: F811
    _, owner, _ = database
    repo, identifier = JournalRepository(), uuid4()
    repo.save_outing(owner, identifier, outing(note="Private note"))
    repo.remove(owner, "outing", identifier, 1)
    repo.remove(owner, "outing", identifier, 1)
    with pytest.raises(HTTPException) as error:
        repo.save_outing(owner, identifier, outing(note="Private note"))
    assert error.value.status_code == 409 and error.value.detail == "Entity was deleted"
    marks = repo.deletions(owner, DeletionQuery())
    assert len(marks.deletions) == 1 and marks.deletions[0].version == 2
    assert set(marks.deletions[0].model_dump()) == {"kind", "id", "version", "deleted_at"}
    missing = uuid4()
    repo.remove(owner, "observation", missing, 1)
    with pytest.raises(HTTPException):
        repo.save_observation(owner, missing, observation())


def test_explicit_membership_counts_do_not_multiply_and_timeline_keeps_discarded(database):  # noqa: F811
    detections, owner, _ = database
    repo = JournalRepository()
    first = repo.save_outing(owner, uuid4(), outing())
    second = repo.save_outing(owner, uuid4(), outing())
    rows = [DetectionInput.model_validate(detection(species=species)) for species in ("Turdus fuscater", "Zonotrichia capensis", "Turdus fuscater")]
    detections.batch(owner, rows)
    for row in rows:
        repo.link_detection(owner, first.id, row.id)
        repo.link_detection(owner, first.id, row.id)
    for _ in range(3):
        repo.save_observation(owner, uuid4(), observation(outing_id=str(first.id)))
    detections.review(owner, rows[-1].id, ReviewAction.DISCARD, None)
    counted = repo.outing(owner, first.id)
    assert (counted.detections, counted.observations, counted.species) == (2, 3, 2)
    assert repo.outing(owner, second.id).detections == 0
    timeline = repo.timeline(owner, first.id, EventQuery()).events
    assert len(timeline) == 6
    assert any(event.kind == "detection" and event.status == "discarded" for event in timeline)
    with pytest.raises(HTTPException) as error:
        repo.link_detection(owner, second.id, rows[0].id)
    assert error.value.status_code == 409
    repo.unlink_detection(owner, first.id, rows[0].id)
    repo.unlink_detection(owner, first.id, rows[0].id)
    repo.link_detection(owner, second.id, rows[0].id)
    assert repo.outing(owner, second.id).detections == 1
    repo.remove(owner, "outing", first.id, 1)
    assert detections.get_owned(owner, rows[1].id) is not None
    remaining = repo.observations(owner, ObservationQuery()).observations
    assert len(remaining) == 3 and all(row.outing_id is None and row.version == 2 for row in remaining)


def test_triple_event_cursor_handles_same_uuid_and_time_in_different_tables(database):  # noqa: F811
    detections, owner, _ = database
    repo, organization = JournalRepository(), OrganizationRepository()
    identifier = uuid4()
    target = repo.save_outing(owner, uuid4(), outing())
    row = DetectionInput.model_validate(detection(id=str(identifier), recorded_at="2026-11-14T11:00:00Z"))
    detections.batch(owner, [row])
    repo.link_detection(owner, target.id, identifier)
    repo.save_observation(owner, identifier, observation(outing_id=str(target.id)))
    collection = organization.save_collection(owner, uuid4(), CollectionInput(name="Birds", note=None))
    for kind in ("detection", "observation"):
        organization.link_item(owner, collection.id, kind, identifier)
        organization.link_item(owner, collection.id, kind, identifier)
    assert organization.collection(owner, collection.id).items == 2
    for get_page, field in ((lambda query: repo.timeline(owner, target.id, query), "events"),
                            (lambda query: organization.items(owner, collection.id, query), "items")):
        first = get_page(EventQuery(limit=1))
        event = getattr(first, field)[0]
        assert event.kind == "detection" and first.has_more
        second = get_page(EventQuery(limit=1, after=event.recorded_at, after_id=event.id, after_kind=event.kind))
        assert getattr(second, field)[0].kind == "observation" and not second.has_more
    repo.remove(owner, "observation", identifier, 1)
    assert organization.collection(owner, collection.id).items == 1


def test_descending_and_deletion_cursors_do_not_lose_tied_rows(database):  # noqa: F811
    _, owner, _ = database
    repo = JournalRepository()
    identifiers = sorted([uuid4(), uuid4(), uuid4()])
    for identifier in identifiers:
        repo.save_outing(owner, identifier, outing())
    first = repo.outings(owner, DescendingQuery(limit=2))
    last = first.outings[-1]
    second = repo.outings(owner, DescendingQuery(limit=2, before=last.started_at, after_id=last.id))
    assert [row.id for row in first.outings + second.outings] == identifiers
    assert first.has_more and not second.has_more
    # The ledger shares a timestamp and UUID across kinds; kind is the final tie breaker.
    same = uuid4()
    with repo.transaction(owner) as c:
        c.execute("SELECT private.delete_beta_entity('outing',%s,1)", (same,))
        c.execute("SELECT private.delete_beta_entity('collection',%s,1)", (same,))
    marks = repo.deletions(owner, DeletionQuery(limit=1))
    mark = marks.deletions[-1]
    tail = repo.deletions(owner, DeletionQuery(limit=1, after=mark.deleted_at, after_id=mark.id, after_kind=mark.kind))
    assert [mark.kind, tail.deletions[0].kind] == ["collection", "outing"]


def test_organization_is_private_and_foreign_references_are_rejected(database):  # noqa: F811
    detections, owner, other = database
    repo = OrganizationRepository()
    target = repo.save_outing(owner, uuid4(), outing())
    foreign = repo.save_outing(other, uuid4(), outing())
    shared = DetectionInput.model_validate(detection(shared=True))
    detections.batch(other, [shared])
    visual = repo.save_observation(owner, uuid4(), observation(outing_id=str(target.id)))
    collection = repo.save_collection(owner, uuid4(), CollectionInput(name=" Private   collection ", note=" Note "))
    for action in (
        lambda: repo.link_detection(owner, target.id, shared.id),
        lambda: repo.save_observation(owner, uuid4(), observation(outing_id=str(foreign.id))),
        lambda: repo.link_item(other, collection.id, "observation", visual.id),
        lambda: repo.link_item(owner, collection.id, "detection", shared.id),
        lambda: repo.collection(other, collection.id),
    ):
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 404
    assert not repo.collections(other, DescendingQuery()).collections
    assert not repo.observations(other, ObservationQuery()).observations
    repo.link_item(owner, collection.id, "observation", visual.id)
    repo.remove(owner, "collection", collection.id, 1)
    assert repo.observation(owner, visual.id) == visual
    wished = repo.wish(owner, "Turdus fuscater")
    assert repo.wish(owner, wished.species) == wished
    assert not repo.wishlist(other, WishlistQuery()).species
    assert repo.wishlist(owner, WishlistQuery()).species == [wished]
    repo.unwish(other, wished.species)
    assert repo.wishlist(owner, WishlistQuery()).species == [wished]
    for species in ("Invented bird",):
        with pytest.raises(HTTPException) as error:
            repo.wish(owner, species)
        assert error.value.status_code == 404


def test_beta_http_boundaries_and_optimistic_sync_use_real_database(database):  # noqa: F811
    _, owner, other = database
    app.dependency_overrides[authenticate] = lambda: owner
    try:
        with TestClient(app) as client:
            identifier = str(uuid4())
            body = outing().model_dump(mode="json")
            saved = client.put(f"/v1/outings/{identifier}", json=body)
            assert saved.status_code == 200 and saved.json()["version"] == 1
            assert client.get("/v1/outings", params={"after_id": identifier}).status_code == 422
            assert client.put(f"/v1/outings/{identifier}", json={**body, "expected_version": True}).status_code == 422
            assert client.put(f"/v1/outings/{identifier}", json={**body, "title": "Updated", "expected_version": 1}).json()["version"] == 2
            assert client.delete(f"/v1/outings/{identifier}").status_code == 422
            assert client.delete(f"/v1/outings/{identifier}", params={"expected_version": 1}).status_code == 409
            app.dependency_overrides[authenticate] = lambda: other
            assert client.get(f"/v1/outings/{identifier}").status_code == 404
            assert client.get("/v1/outings").json()["outings"] == []
            app.dependency_overrides[authenticate] = lambda: owner
            assert client.delete(f"/v1/outings/{identifier}", params={"expected_version": 2}).status_code == 204
            assert client.put(f"/v1/outings/{identifier}", json=body).status_code == 409
            assert len(client.get("/v1/deletions").json()["deletions"]) == 1
    finally:
        app.dependency_overrides.clear()
