"""Closed journal queries keep totals deterministic and private prose out of every result."""
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from birdnet_api.app import app
from birdnet_api.auth import authenticate
from birdnet_api.beta.contracts import AssistantQuestion, CollectionInput
from birdnet_api.beta.query import QueryRepository
from birdnet_api.contracts import DetectionInput
from birdnet_api.domain import ReviewAction
from tests.test_api import detection
from tests.test_beta_repository import observation, outing
from tests.test_postgres import database, pytestmark  # noqa: F401

QUESTION = TypeAdapter(AssistantQuestion)


def ask(repo, owner, intent, **filters):
    return repo.query(owner, QUESTION.validate_python({"intent": intent, **filters}))


@pytest.fixture
def journal_data(database):  # noqa: F811
    detections, owner, other = database
    repo = QueryRepository()
    target = repo.save_outing(owner, uuid4(), outing(note="Private visit note"))
    rows = [DetectionInput.model_validate(detection(species=species, confidence=confidence, status=status, recorded_at=moment))
        for species, confidence, status, moment in (
            ("Turdus fuscater", 0.9, "confirmed", "2026-11-14T11:00:00Z"),
            ("Turdus fuscater", 0.7, "provisional", "2026-11-14T12:00:00Z"),
            ("Zonotrichia capensis", 0.9, "confirmed", "2026-11-14T12:00:00Z"),
            ("Zonotrichia capensis", 0.9, "confirmed", "2026-11-14T13:00:00Z"),
        )]
    detections.batch(owner, rows)
    detections.batch(other, [DetectionInput.model_validate(detection(recorded_at="2026-11-14T12:00:00Z"))])
    detections.review(owner, rows[-1].id, ReviewAction.DISCARD, None)
    for row in rows:
        repo.link_detection(owner, target.id, row.id)
    for _ in range(2):
        repo.save_observation(owner, uuid4(), observation(outing_id=str(target.id), note="Private visual note"))
    repo.save_collection(owner, uuid4(), CollectionInput(name="Private collection", note="Private collection note"))
    for species in ("Turdus fuscater", "Zonotrichia capensis"):
        repo.wish(owner, species)
    return repo, owner, other, target, rows


def test_acoustic_totals_do_not_change_with_cursor_and_pending_is_explicit(journal_data):
    repo, owner, _, _, _ = journal_data
    first = ask(repo, owner, "detections", limit=2)
    last = first.detections[-1]
    second = ask(repo, owner, "detections", limit=2, before=last.recorded_at, after_id=last.id)
    assert first.totals.model_dump() == second.totals.model_dump() == {"detections": 4, "species": 2}
    assert first.has_more and not second.has_more
    assert len({row.id for row in first.detections + second.detections}) == 4
    assert first.detections[0].status == "discarded"
    pending = ask(repo, owner, "detections", review="pending")
    assert pending.totals.detections == 1 and pending.detections[0].status == "provisional"
    assert pending.filters.model_dump()["review"] == "pending" and pending.usage is None


def test_period_is_inclusive_at_start_exclusive_at_end_and_species_is_filtered(journal_data):
    repo, owner, _, _, rows = journal_data
    result = ask(repo, owner, "detections", since="2026-11-14T12:00:00Z", until="2026-11-14T13:00:00Z", species="Turdus fuscater")
    assert [row.id for row in result.detections] == [rows[1].id]
    assert result.totals.model_dump() == {"detections": 1, "species": 1}
    assert ask(repo, owner, "outings", since="2026-11-14T12:00:00Z").totals.outings == 0
    assert ask(repo, owner, "observations", until="2026-11-14T11:00:00Z").totals.observations == 0


def test_species_query_uses_only_non_discarded_acoustics(journal_data):
    repo, owner, _, _, _ = journal_data
    first = ask(repo, owner, "species", limit=1)
    assert first.totals.model_dump() == {"detections": 3, "species": 2}
    assert first.species[0].species == "Turdus fuscater"
    assert first.species[0].detections == 2 and first.species[0].best_confidence == 0.9
    second = ask(repo, owner, "species", limit=1, after_species=first.species[0].species)
    assert second.totals == first.totals and second.species[0].detections == 1
    assert second.species[0].species == "Zonotrichia capensis" and not second.has_more


def test_manual_queries_and_outings_preserve_types_without_prose_or_location(journal_data):
    repo, owner, other, target, _ = journal_data
    result = ask(repo, owner, "observations", outing_id=str(target.id), limit=1)
    assert result.totals.model_dump() == {"observations": 2, "species": 1}
    visual = result.observations[0]
    assert set(visual.model_dump()) == {"id", "version", "source", "species", "observed_at", "individuals", "outing_id"}
    assert visual.source == "manual" and result.has_more
    tail = ask(repo, owner, "observations", outing_id=str(target.id), limit=1, before=visual.observed_at, after_id=visual.id)
    assert tail.totals == result.totals and tail.observations[0].id != visual.id
    out = ask(repo, owner, "outings").outings[0]
    assert (out.detections, out.observations, out.species) == (3, 2, 2)
    assert "note" not in out.model_dump() and "site_id" not in out.model_dump()
    with pytest.raises(HTTPException) as error:
        ask(repo, other, "observations", outing_id=str(target.id))
    assert error.value.status_code == 404


def test_organization_totals_and_cursor_are_scoped_to_the_person(journal_data):
    repo, owner, other, _, _ = journal_data
    collections = ask(repo, owner, "collections")
    assert collections.totals.collections == 1 and collections.filters.model_dump() == {}
    assert "note" not in collections.collections[0].model_dump()
    result = ask(repo, owner, "wishlist", limit=1)
    tail = ask(repo, owner, "wishlist", limit=1, after_species=result.wishlist[0].species)
    assert result.totals.species == tail.totals.species == 2
    assert result.has_more and not tail.has_more
    assert ask(repo, owner, "wishlist", species="Turdus fuscater").totals.species == 1
    for intent in ("outings", "observations", "collections", "wishlist"):
        assert not getattr(ask(repo, other, intent), intent)
    own = ask(repo, other, "detections")
    assert own.totals.detections == 1


@pytest.mark.parametrize("intent", ["detections", "species", "outings", "observations", "collections", "wishlist"])
def test_http_query_union_rejects_prose_and_never_uses_the_provider(database, intent, monkeypatch):  # noqa: F811
    _, owner, _ = database
    monkeypatch.setenv("GEMINI_API_KEY", "")
    app.dependency_overrides[authenticate] = lambda: owner
    try:
        with TestClient(app) as client:
            response = client.post("/v1/assistant/query", json={"intent": intent})
            assert response.status_code == 200
            body = response.json()
            assert set(body) == {"intent", "filters", "totals", intent, "has_more", "usage"}
            assert body[intent] == [] and body["usage"] is None
            for extra in ({"question": "Private note"}, {"messages": []}, {"sql": "DROP TABLE public.outings"}, {"limit": True}):
                assert client.post("/v1/assistant/query", json={"intent": intent, **extra}).status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_comparison_counts_presence_per_period_apart_from_observations_and_only_the_owner(journal_data, database):  # noqa: F811
    repo, owner, other, _, _ = journal_data
    detections, _, _ = database
    detections.batch(owner, [DetectionInput.model_validate(detection(species="Colibri coruscans", recorded_at="2026-11-07T12:00:00Z"))])
    detections.batch(other, [DetectionInput.model_validate(detection(species="Pyrocephalus rubinus", recorded_at="2026-11-14T12:00:00Z"))])
    week = {"since": "2026-11-09T05:00:00Z", "until": "2026-11-16T05:00:00Z"}
    before = {"since": "2026-11-02T05:00:00Z", "until": "2026-11-09T05:00:00Z"}
    result = ask(repo, owner, "compare", first=week, second=before)
    # The discarded song does not count; the two visual observations are reported apart, never as songs.
    assert result.first.model_dump(include={"detections", "species", "observations", "observed_species"}) == {
        "detections": 3, "species": 2, "observations": 2, "observed_species": 1}
    assert result.second.model_dump(include={"detections", "species", "observations", "observed_species"}) == {
        "detections": 1, "species": 1, "observations": 0, "observed_species": 0}
    assert result.only_first == ["Turdus fuscater", "Zonotrichia capensis"] and result.only_second == ["Colibri coruscans"]
    assert "Pyrocephalus rubinus" not in result.only_first and not result.has_more
    limited = ask(repo, owner, "compare", first=week, second=before, limit=1)
    assert limited.only_first == ["Turdus fuscater"] and limited.has_more
    assert ask(repo, owner, "compare", first=week, second=before, species="Turdus fuscater").only_first == ["Turdus fuscater"]


@pytest.mark.parametrize("periods", [
    {"first": {"since": "2026-11-09T05:00:00Z"}, "second": {"since": "2026-11-02T05:00:00Z", "until": "2026-11-09T05:00:00Z"}},
    {"first": {"since": "2026-11-09T05:00:00Z", "until": "2026-11-09T05:00:00Z"}, "second": {"since": "2026-11-02T05:00:00Z", "until": "2026-11-09T05:00:00Z"}},
])
def test_comparison_needs_two_bounded_ordered_periods(periods):
    with pytest.raises(ValueError):
        QUESTION.validate_python({"intent": "compare", **periods})
