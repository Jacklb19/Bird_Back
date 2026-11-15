"""Boundary and cursor regressions for the frozen beta contract."""
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from birdnet_api.beta.contracts import AssistantQuestion, CollectionInput, EventQuery, ObservationInput, OutingInput, TutorInput


def outing(**changes):
    return {"title": None, "kind": "stationary", "started_at": "2026-11-14T11:00:00Z", "ended_at": None,
            "note": None, "site_id": None, "walk_id": None, **changes}


@pytest.mark.parametrize("note", [None, "", "  ", "\n\t"])
def test_empty_notes_have_one_canonical_form(note):
    assert OutingInput.model_validate(outing(note=note)).note is None
    assert CollectionInput.model_validate({"name": "  Garden   birds ", "note": note}).model_dump() == {
        "expected_version": None, "name": "Garden birds", "note": None}


@pytest.mark.parametrize("change", [
    {"started_at": "2026-11-14T11:00:00"}, {"ended_at": "2026-11-14T10:59:59Z"},
    {"walk_id": str(uuid4())}, {"kind": "stream"}, {"owner_id": str(uuid4())},
    {"expected_version": True}, {"expected_version": "1"}, {"expected_version": 0},
])
def test_outing_rejects_ambiguous_or_unapproved_fields(change):
    with pytest.raises(ValidationError):
        OutingInput.model_validate(outing(**change))


def test_visual_observation_has_no_automatic_confidence_or_media_upload():
    body = {"species": "Turdus fuscater", "observed_at": "2026-11-14T11:00:00Z", "location": None,
            "individuals": None, "note": None, "outing_id": None}
    assert ObservationInput.model_validate(body).location is None
    for extra in ({"confidence": 1.0}, {"photo": "private-file"}, {"individuals": True},
                  {"location": {"latitude": 4.67891, "longitude": -74.1234}}):
        with pytest.raises(ValidationError):
            ObservationInput.model_validate({**body, **extra})


def test_event_cursor_requires_time_identifier_and_kind_even_when_ids_collide():
    full = {"after": "2026-11-14T11:00:00Z", "after_id": str(uuid4()), "after_kind": "observation"}
    assert EventQuery.model_validate(full).after_kind == "observation"
    for field in full:
        with pytest.raises(ValidationError):
            EventQuery.model_validate({k: v for k, v in full.items() if k != field})


@pytest.mark.parametrize("body", [
    {"intent": "sql", "sql": "select * from profiles"},
    {"intent": "detections", "question": "My private place"},
    {"intent": "outings", "review": "pending"},
    {"intent": "species", "before": "2026-11-14T11:00:00Z"},
    {"intent": "wishlist", "since": "2026-11-14T11:00:00Z"},
    {"intent": "observations", "since": "2026-11-14T11:00:00Z", "until": "2026-11-14T11:00:00Z"},
])
def test_assistant_only_accepts_the_defined_read_plans(body):
    with pytest.raises(ValidationError):
        TypeAdapter(AssistantQuestion).validate_python(body)


def test_tutor_only_accepts_public_topics_with_the_required_species_context():
    assert TutorInput.model_validate({"topic": "confidence"}).species is None
    for body in ({"topic": "identify_audio"}, {"topic": "confidence", "question": "Private data"},
                 {"topic": "species_facts"}, {"topic": "confidence", "species": "Turdus fuscater"}):
        with pytest.raises(ValidationError):
            TutorInput.model_validate(body)
