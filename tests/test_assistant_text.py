"""Consent gates, private HTTP errors and closed free-text actions with local provider stand-ins."""
import copy
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from birdnet_api.app import app
from birdnet_api.auth import second_factors
from birdnet_api.beta import free_tutor, tutor as tutor_module
from birdnet_api.beta.contracts import TokenUsage
from birdnet_api.beta.free_tutor import answer_question
from birdnet_api.beta.interpretation import interpret, literal_species
from birdnet_api.beta.text_contracts import InterpretInput, TutorQuestionInput
from birdnet_api.beta.text_model import StructuredCompletion
from birdnet_api.beta.text_routes import current_time, text_usage, text_writer
from birdnet_api.errors import ErrorCode, raise_error
from birdnet_api.settings import get_settings
from tests.test_api import api  # noqa: F401
from tests.test_assistant_text_contracts import CONSENT
from tests.test_language_texts import FACTS, MemoryUsage

CURRENT = datetime(2026, 11, 14, 15, tzinfo=UTC)
USAGE = TokenUsage(input_tokens=20, output_tokens=10, total_tokens=35)


def ready(intent="species", **changes):
    return {"status": "ready", "plan": {"intent": intent, "species": None, "period": {"kind": "all"}, "review": "all", **changes}, "reason_code": None}


class StructuredStub:
    model = "test-structured-model"

    def __init__(self, *answers, configured=True):
        self.answers = list(answers)
        self.requests = []
        self.configured = configured

    def ensure_configured(self):
        if not self.configured:
            raise_error(ErrorCode.LLM_NOT_CONFIGURED)

    def write(self, instructions, facts, schema):
        self.requests.append((instructions, copy.deepcopy(facts), schema))
        value = self.answers.pop(0)
        if isinstance(value, Exception):
            raise value
        return StructuredCompletion(value, USAGE)


class Counter(MemoryUsage):
    def __init__(self, per_person=100):
        super().__init__(per_person)
        self.refunds = 0

    def give_back(self, owner, current):
        self.refunds += 1
        super().give_back(owner, current)


@pytest.fixture
def text_api(api):  # noqa: F811
    client, _, headers, owner = api
    counter, writer = Counter(), StructuredStub()
    app.dependency_overrides[text_usage] = lambda: counter
    app.dependency_overrides[text_writer] = lambda: writer
    app.dependency_overrides[current_time] = lambda: CURRENT
    return client, headers, owner, counter, writer


def body_for(path, **changes):
    body = {"text": "Show yesterday", "consent": CONSENT}
    if path.endswith("tutor-question"):
        body["topic"] = "confidence"
    return {**body, **changes}


def test_policy_is_authenticated_without_provider_or_quota(text_api):
    client, headers, _, counter, writer = text_api
    path = "/v1/assistant/text-policy"
    assert client.get(path).status_code == 401
    response = client.get(path, headers=headers())
    assert response.status_code == 200
    assert response.json() == {"version": CONSENT["version"], "provider": "Google Gemini", "terms_url": "https://aistudio.google.com/docs/terms", "max_text_characters": 900, "max_output_tokens": 1200}
    assert counter.spent == {} and writer.requests == []


@pytest.mark.parametrize("path", ["/v1/assistant/interpret", "/v1/assistant/tutor-question"])
@pytest.mark.parametrize("consent,status,code", [
    (None, 422, "consent_required"),
    ({"version": CONSENT["version"], "accepted": False}, 422, "consent_required"),
    ({"version": CONSENT["version"], "accepted": 1}, 422, "consent_required"),
    ({"version": CONSENT["version"], "accepted": "true"}, 422, "consent_required"),
    ({"version": "outdated", "accepted": True}, 409, "consent_version_changed"),
    ({"version": CONSENT["version"]}, 422, "consent_required"),
])
def test_consent_gate_precedes_any_source_provider_or_quota(text_api, path, consent, status, code, monkeypatch):
    client, headers, _, counter, writer = text_api
    monkeypatch.setattr(free_tutor, "public_topic", lambda _: pytest.fail("A public source was accessed before consent"))
    response = client.post(path, json=body_for(path, consent=consent), headers=headers())
    assert response.status_code == status and response.json()["code"] == code
    assert counter.spent == {} and writer.requests == []
    missing = body_for(path)
    del missing["consent"]
    assert client.post(path, json=missing, headers=headers()).json()["code"] == "consent_required"


@pytest.mark.parametrize("path", ["/v1/assistant/interpret", "/v1/assistant/tutor-question"])
def test_router_keeps_private_errors_for_surrogates_extras_and_malformed_json(text_api, path, caplog):
    client, headers, _, counter, writer = text_api
    secret = "private-text-canary"
    cases = [json.dumps(body_for(path, text=secret, messages=[secret])),
             json.dumps(body_for(path, text="\ud800")), json.dumps(body_for(path, text="\udfff")),
             json.dumps(body_for(path, text=" ")), json.dumps(body_for(path, text="🐦" * 901)),
             '{"text":"private-text-canary", invalid json']
    for content in cases:
        response = client.post(path, content=content, headers={**headers(), "Content-Type": "application/json"})
        assert response.status_code == 422 and response.json()["code"] == "invalid_assistant_request"
        assert set(response.json()) == {"detail", "code"}
        assert secret not in response.text and "input" not in response.text and "ctx" not in response.text
    assert counter.spent == {} and writer.requests == [] and secret not in caplog.text
    # Existing routes retain their established FastAPI validation body.
    old = client.post("/v1/assistant/query", json={"intent": "species", "text": secret}, headers=headers())
    assert old.status_code == 422 and isinstance(old.json()["detail"], list) and "code" not in old.json()


@pytest.mark.parametrize("path", ["/v1/assistant/interpret", "/v1/assistant/tutor-question"])
def test_text_routes_require_auth_and_aal2_before_generation(text_api, path):
    client, headers, owner, counter, writer = text_api
    assert client.post(path, json=body_for(path)).status_code == 401
    app.dependency_overrides[second_factors]().people.add(owner)
    assert client.post(path, json=body_for(path), headers=headers()).status_code == 403
    assert not counter.spent and not writer.requests


@pytest.mark.parametrize("intent", ["detections", "species", "outings", "observations", "collections", "wishlist"])
def test_interpret_returns_each_reviewable_query_without_private_data(text_api, intent):
    client, headers, owner, counter, writer = text_api
    writer.answers.append(ready(intent))
    response = client.post("/v1/assistant/interpret", json={"text": "Mis registros", "locale": "en", "tz": "America/New_York", "limit": 1, "consent": CONSENT}, headers=headers())
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {"status", "query", "plan", "model", "generated_at", "usage"}
    assert result["plan"]["intent"] == intent
    assert result["status"] == "ready" and result["query"]["intent"] == intent
    assert result["query"]["limit"] == 1 and result["query"]["tz"] == "America/New_York"
    assert result["usage"]["total_tokens"] == 35 and counter.spent[owner] == 1
    _, facts, _ = writer.requests[0]
    assert facts == {"text": "Mis registros", "locale": "en"}
    assert str(owner) not in json.dumps(facts) and "tz" not in facts and "limit" not in facts


def test_follow_up_sends_only_the_active_plan_and_may_keep_its_species(text_api):
    client, headers, _, _, writer = text_api
    context = {"intent": "detections", "species": "Turdus fuscater", "period": {"kind": "relative", "value": "this_week"}, "review": "all", "compare_period": None}
    writer.answers.append(ready("detections", species="Turdus fuscater", period={"kind": "relative", "value": "yesterday"}))
    response = client.post("/v1/assistant/interpret", json=body_for("/interpret", text="¿Y ayer?", context=context), headers=headers())
    assert response.status_code == 200 and response.json()["query"]["species"] == "Turdus fuscater"
    assert response.json()["plan"]["period"] == {"kind": "relative", "value": "yesterday"}
    _, facts, _ = writer.requests[0]
    # The text, the language and the plan's descriptors: no result, identifier, note or place reaches the provider.
    assert facts == {"text": "¿Y ayer?", "locale": "es", "context": context}


@pytest.mark.parametrize("context", [
    {"intent": "detections", "species": None, "period": {"kind": "all"}, "review": "all", "note": "private"},
    {"intent": "detections", "species": None, "period": {"kind": "all"}, "review": "all", "outing_id": str(uuid4())},
    {"intent": "observations", "species": None, "period": {"kind": "all"}, "review": "pending"},
    {"intent": "detections", "species": "Private place", "period": {"kind": "all"}, "review": "all"},
])
def test_context_with_private_or_invalid_fields_is_refused_before_the_provider(text_api, context):
    client, headers, _, counter, writer = text_api
    response = client.post("/v1/assistant/interpret", json=body_for("/interpret", context=context), headers=headers())
    assert response.status_code == 422 and response.json()["code"] == "invalid_assistant_request"
    assert counter.spent == {} and writer.requests == []


def test_a_species_neither_written_nor_in_the_context_is_never_executed(text_api):
    client, headers, owner, counter, writer = text_api
    context = {"intent": "detections", "species": "Zonotrichia capensis", "period": {"kind": "all"}, "review": "all"}
    writer.answers.append(ready("detections", species="Turdus fuscater"))
    response = client.post("/v1/assistant/interpret", json=body_for("/interpret", text="¿Y ayer?", context=context), headers=headers())
    assert response.status_code == 503 and counter.spent[owner] == 0 and counter.refunds == 1


def test_a_comparison_resolves_two_bounded_periods_or_asks_which(text_api):
    client, headers, _, _, writer = text_api
    writer.answers.append(ready("compare", period={"kind": "relative", "value": "this_week"}, compare_period={"kind": "relative", "value": "last_week"}))
    result = client.post("/v1/assistant/interpret", json=body_for("/interpret", text="Esta semana frente a la pasada"), headers=headers()).json()
    assert result["query"]["intent"] == "compare"
    assert result["query"]["first"] == {"since": "2026-11-09T05:00:00Z", "until": "2026-11-16T05:00:00Z"}
    assert result["query"]["second"] == {"since": "2026-11-02T05:00:00Z", "until": "2026-11-09T05:00:00Z"}
    writer.answers.append(ready("compare", compare_period={"kind": "relative", "value": "last_week"}))
    unbounded = client.post("/v1/assistant/interpret", json=body_for("/interpret", text="Todo frente a la semana pasada"), headers=headers()).json()
    assert unbounded["status"] == "clarification" and unbounded["reason_code"] == "ambiguous_period"


def test_interpret_resolves_period_and_requires_explicit_confirmation_elsewhere(text_api):
    client, headers, _, _, writer = text_api
    writer.answers.append(ready(period={"kind": "relative", "value": "yesterday"}))
    result = client.post("/v1/assistant/interpret", json=body_for("/interpret"), headers=headers()).json()
    assert result["query"]["since"] == "2026-11-13T05:00:00Z" and result["query"]["until"] == "2026-11-14T05:00:00Z"
    assert "totals" not in result and "species" not in result and len(writer.requests) == 1


@pytest.mark.parametrize("status,code", [("clarification", "ambiguous_species"), ("clarification", "private_reference"),
    ("rejected", "unsupported_operation"), ("rejected", "identification_request")])
def test_usable_clarifications_and_rejections_consume_one_unit(text_api, status, code):
    client, headers, owner, counter, writer = text_api
    writer.answers.append({"status": status, "plan": None, "reason_code": code})
    result = client.post("/v1/assistant/interpret", json=body_for("/interpret"), headers=headers()).json()
    assert result["status"] == status and result["reason_code"] == code and "query" not in result
    assert counter.spent[owner] == 1 and counter.refunds == 0


@pytest.mark.parametrize("plan", [
    ready(species="Turdus fuscater"),
    ready("outings", species="Turdus fuscater"),
    ready("collections", period={"kind": "relative", "value": "today"}),
    {**ready(), "sql": "SELECT private-note"},
    ready(period={"kind": "dates", "first": "9999-12-31", "last": "9999-12-31"}),
])
def test_invalid_or_invented_plan_is_never_executed_and_is_refunded(text_api, plan):
    client, headers, owner, counter, writer = text_api
    writer.answers.append(plan)
    response = client.post("/v1/assistant/interpret", json=body_for("/interpret"), headers=headers())
    assert response.status_code == 503 and response.json()["code"] == "model_output_invalid"
    assert counter.spent[owner] == 0 and counter.refunds == 1 and "query" not in response.json()


def test_literal_species_is_checked_without_accepting_prefixes_or_hyphenated_epithets():
    for text in ("Turdus fuscater", "¿TURDUS   FUSCATER?", "Turdus\nfuscater."):
        assert literal_species("Turdus fuscater", text)
    for text in ("Turdus fuscaterus", "Turdus fuscater-other", "Other-Turdus fuscater", "I heard a black bird"):
        assert not literal_species("Turdus fuscater", text)
    assert literal_species("Poecile atricapillus-carolinensis", "Poecile atricapillus-carolinensis")


def test_literal_unknown_species_is_a_usable_rejection_while_known_literal_is_allowed():
    owner, counter = uuid4(), Counter()
    writer = StructuredStub(ready(species="Inventus birdus"), ready(species="Turdus fuscater"))
    unknown = interpret(owner, InterpretInput(text="Inventus birdus", consent=CONSENT), CURRENT, counter, writer)
    assert unknown.status == "rejected" and unknown.reason_code == "unsupported_filter"
    known = interpret(owner, InterpretInput(text="turdus fuscater", consent=CONSENT), CURRENT, counter, writer)
    assert known.status == "ready" and known.query.species == "Turdus fuscater"
    assert counter.spent[owner] == 2 and counter.refunds == 0


def test_missing_configuration_precedes_quota_and_public_source(text_api, monkeypatch):
    client, headers, _, counter, writer = text_api
    writer.configured = False
    monkeypatch.setattr(free_tutor, "public_topic", lambda _: pytest.fail("Unexpected source access"))
    for path in ("/v1/assistant/interpret", "/v1/assistant/tutor-question"):
        response = client.post(path, json=body_for(path), headers=headers())
        assert response.status_code == 503 and response.json()["detail"] == ErrorCode.LLM_NOT_CONFIGURED.message
    assert counter.spent == {} and writer.requests == []


@pytest.mark.parametrize("topic", ["confidence", "detection_status", "recording_privacy", "approximate_location", "species_facts"])
def test_free_tutor_answers_only_selected_public_context_with_server_sources(text_api, monkeypatch, topic):
    client, headers, owner, counter, writer = text_api
    monkeypatch.setattr(tutor_module, "gather", lambda *_: FACTS)
    writer.answers.append({"status": "answered", "paragraphs": ["Explicación pública."], "reason_code": None})
    body = {"topic": topic, "text": "Explain the public facts", "consent": CONSENT, "locale": "en"}
    if topic == "species_facts":
        body["species"] = "Turdus fuscater"
    response = client.post("/v1/assistant/tutor-question", json=body, headers=headers())
    assert response.status_code == 200
    answer = response.json()["answer"]
    assert answer["topic"] == topic and answer["model"] == writer.model and answer["usage"]["total_tokens"] == 35
    _, facts, _ = writer.requests[0]
    assert set(facts) == {"topic", "locale", "question", "facts"}
    assert facts["question"] == body["text"] and str(owner) not in json.dumps(facts)
    assert counter.spent[owner] == 1
    if topic in ("confidence", "detection_status"):
        assert answer["sources"][0]["url"] is None
    elif topic == "species_facts":
        assert answer["sources"][0]["url"] == FACTS.sources[0].url


@pytest.mark.parametrize("code", ["outside_topic", "identification_request", "private_statistics_request", "insufficient_public_facts"])
def test_free_tutor_rejects_with_fixed_code_and_keeps_usable_generation_charge(text_api, code):
    client, headers, owner, counter, writer = text_api
    writer.answers.append({"status": "rejected", "paragraphs": [], "reason_code": code})
    result = client.post("/v1/assistant/tutor-question", json=body_for("/tutor-question"), headers=headers()).json()
    assert result["status"] == "rejected" and result["reason_code"] == code and "answer" not in result
    assert set(result) == {"status", "reason_code", "topic", "species", "locale", "model", "generated_at", "usage"}
    assert counter.spent[owner] == 1 and counter.refunds == 0


@pytest.mark.parametrize("value", [
    {"status": "answered", "paragraphs": ["private-canary"], "reason_code": None, "sources": ["invented-url"]},
    {"status": "answered", "paragraphs": ["\ud800"], "reason_code": None},
    {"status": "rejected", "paragraphs": ["private-canary"], "reason_code": "outside_topic"},
])
def test_free_tutor_invalid_response_is_refunded_without_echo(text_api, value):
    client, headers, owner, counter, writer = text_api
    writer.answers.append(value)
    response = client.post("/v1/assistant/tutor-question", json=body_for("/tutor-question"), headers=headers())
    assert response.status_code == 503 and response.json()["code"] == "model_output_invalid"
    assert "private-canary" not in response.text and counter.spent[owner] == 0 and counter.refunds == 1


def test_free_tutor_public_source_failure_refunds_and_does_not_call_writer(monkeypatch):
    owner, counter, writer = uuid4(), Counter(), StructuredStub()

    def failure(_):
        raise_error(ErrorCode.PUBLIC_DATA_UNAVAILABLE)

    monkeypatch.setattr(free_tutor, "public_topic", failure)
    with pytest.raises(HTTPException) as error:
        answer_question(owner, TutorQuestionInput(topic="confidence", text="Explain", consent=CONSENT), CURRENT, counter, writer)
    assert error.value.status_code == 502 and counter.spent[owner] == 0 and counter.refunds == 1 and not writer.requests


def test_a_previous_account_acceptance_does_not_authorize_another_request(text_api):
    client, headers, owner, counter, writer = text_api
    writer.answers.append(ready())
    assert client.post("/v1/assistant/interpret", json=body_for("/interpret"), headers=headers()).status_code == 200
    other = uuid4()
    response = client.post("/v1/assistant/interpret", json={"text": "Show birds"}, headers=headers(other))
    assert response.status_code == 422 and response.json()["code"] == "consent_required"
    assert counter.spent == {owner: 1} and len(writer.requests) == 1


def test_global_metadata_body_bound_prevents_generation(text_api, monkeypatch):
    client, headers, _, counter, writer = text_api
    monkeypatch.setenv("MAX_METADATA_BODY_BYTES", "128")
    get_settings.cache_clear()
    response = client.post("/v1/assistant/interpret", json=body_for("/interpret", text="x" * 200), headers=headers())
    assert response.status_code == 413 and not counter.spent and not writer.requests
