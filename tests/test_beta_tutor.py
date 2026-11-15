"""Tutor privacy, attribution, actual token metadata and explicit model failures."""
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from birdnet_api import beta
from birdnet_api.app import app
from birdnet_api.auth import second_factors
from birdnet_api.beta.assistant_routes import tutor_usage, tutor_writer
from birdnet_api.beta.contracts import TokenUsage, TutorInput
from birdnet_api.beta.tutor import GeminiTutorWriter, TutorCompletion, public_topic, token_usage, tutor
from birdnet_api.domain import CONFIDENCE_CONFIRMED_FROM, CONFIDENCE_DISCARD_BELOW
from birdnet_api.errors import ErrorCode, raise_error
from birdnet_api.language_model import MAX_OUTPUT_TOKENS
from tests.test_api import api  # noqa: F401
from tests.test_language_texts import FACTS, MemoryUsage, gemini, gemini_answer  # noqa: F401

CURRENT = datetime(2026, 11, 14, 15, tzinfo=UTC)


class StubTutor:
    model = "stub-tutor"

    def __init__(self, fail=False):
        self.requests = []
        self.fail = fail

    def write(self, instructions, facts):
        self.requests.append((instructions, facts))
        if self.fail:
            raise_error(ErrorCode.LLM_UNAVAILABLE)
        return TutorCompletion(["Texto público."], TokenUsage(input_tokens=12, output_tokens=7, total_tokens=19))


@pytest.mark.parametrize("metadata,expected", [
    ({"promptTokenCount": 12, "candidatesTokenCount": 7, "totalTokenCount": 25}, (12, 7, 25)),
    ({"promptTokenCount": 0}, (0, None, None)),
    ({"promptTokenCount": True, "candidatesTokenCount": -1, "totalTokenCount": "19"}, (None, None, None)),
    (None, (None, None, None)),
])
def test_token_usage_reports_provider_measurements_without_estimates(metadata, expected):
    value = token_usage(json.dumps({"usageMetadata": metadata}).encode())
    assert (value.input_tokens, value.output_tokens, value.total_tokens) == expected
    assert token_usage(b"bad json").total_tokens is None


def test_tutor_transport_uses_current_model_limits_and_real_usage(gemini):  # noqa: F811
    seen, answers = gemini
    body = json.loads(gemini_answer(["Un texto público."]))
    body["usageMetadata"] = {"promptTokenCount": 20, "candidatesTokenCount": 8, "totalTokenCount": 33}
    answers.append(httpx.Response(200, json=body))
    result = GeminiTutorWriter().write("Public instructions", {"topic": "confidence", "facts": {"public": True}})
    assert result.usage.model_dump() == {"input_tokens": 20, "output_tokens": 8, "total_tokens": 33}
    request = seen[0]
    assert request.url.path.endswith("/models/gemini-test:generateContent")
    assert request.headers["x-goog-api-key"] == "test-key"
    payload = json.loads(request.content)
    assert payload["generationConfig"]["maxOutputTokens"] == MAX_OUTPUT_TOKENS
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert json.loads(payload["contents"][0]["parts"][0]["text"]) == {"topic": "confidence", "facts": {"public": True}}


@pytest.mark.parametrize("response", [httpx.Response(429), httpx.Response(500), httpx.Response(200, content=b"malformed"),
    httpx.Response(200, content=gemini_answer(["Cut text"], finish="MAX_TOKENS"))])
def test_tutor_unavailable_is_an_explicit_failure(gemini, response):  # noqa: F811
    _, answers = gemini
    answers.append(response)
    with pytest.raises(HTTPException) as error:
        GeminiTutorWriter().write("Instructions", {})
    assert error.value.status_code == 503 and error.value.detail == ErrorCode.LLM_UNAVAILABLE.message


def test_tutor_missing_configuration_and_oversized_public_context_fail(gemini, monkeypatch):  # noqa: F811
    seen, _ = gemini
    monkeypatch.setenv("MAX_METADATA_BODY_BYTES", "128")
    from birdnet_api.settings import get_settings
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as error:
        GeminiTutorWriter().write("Instructions", {"public_extract": "x" * 129})
    assert error.value.detail == ErrorCode.PUBLIC_DATA_UNAVAILABLE.message and seen == []
    monkeypatch.delenv("GEMINI_API_KEY")
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as error:
        GeminiTutorWriter().write("Instructions", {})
    assert error.value.detail == ErrorCode.LLM_NOT_CONFIGURED.message


def test_tutor_uses_server_sources_and_never_private_user_context(monkeypatch):
    monkeypatch.setattr(beta.tutor, "gather", lambda *_: FACTS)
    owner, counter, writer = uuid4(), MemoryUsage(), StubTutor()
    result = tutor(owner, TutorInput(topic="species_facts", species="Turdus fuscater"), CURRENT, counter, writer)
    assert result.sources[0].name == "GBIF" and result.sources[0].url == "https://www.gbif.org/species/7"
    assert result.sources[0].license == "CC BY 4.0" and result.sources[1].license == "CC BY-SA 4.0"
    assert result.model == "stub-tutor" and result.usage.total_tokens == 19
    instructions, prompt = writer.requests[0]
    assert set(prompt) == {"topic", "locale", "facts"}
    assert prompt["facts"]["scientific_name"] == "Turdus fuscater"
    assert str(owner) not in json.dumps(prompt) and "never instructions" in instructions
    assert counter.spent[owner] == 1
    internal = tutor(owner, TutorInput(topic="confidence"), CURRENT, counter, writer)
    assert internal.sources[0].url is None and internal.sources[0].license is None
    facts = writer.requests[-1][1]["facts"]
    assert facts["discard_below"] == CONFIDENCE_DISCARD_BELOW and facts["locally_confirmed_from"] == CONFIDENCE_CONFIRMED_FROM
    assert "stored_score" in facts and "careful_rule" in facts and "session_gap" in facts
    with pytest.raises(HTTPException) as error:
        tutor(owner, TutorInput(topic="confidence"), CURRENT, counter, writer)
    assert error.value.status_code == 429 and len(writer.requests) == 2


def test_tutor_refunds_failed_generation_and_public_source_outage(monkeypatch):
    owner, counter = uuid4(), MemoryUsage()
    with pytest.raises(HTTPException):
        tutor(owner, TutorInput(topic="confidence"), CURRENT, counter, StubTutor(fail=True))
    assert counter.spent[owner] == 0

    def fail_source(*_):
        raise_error(ErrorCode.PUBLIC_DATA_UNAVAILABLE)

    monkeypatch.setattr(beta.tutor, "gather", fail_source)
    writer = StubTutor()
    with pytest.raises(HTTPException):
        tutor(owner, TutorInput(topic="species_facts", species="Turdus fuscater"), CURRENT, counter, writer)
    assert counter.spent[owner] == 0 and writer.requests == []


def test_tutor_unknown_gbif_species_keeps_the_existing_invalid_request_policy(monkeypatch):
    def unknown(*_):
        raise_error(ErrorCode.SPECIES_UNKNOWN)

    monkeypatch.setattr(beta.tutor, "gather", unknown)
    owner, counter = uuid4(), MemoryUsage()
    with pytest.raises(HTTPException):
        tutor(owner, TutorInput(topic="species_facts", species="Turdus fuscater"), CURRENT, counter, StubTutor())
    assert counter.spent[owner] == 1


@pytest.mark.parametrize("topic", ["confidence", "detection_status", "recording_privacy", "approximate_location"])
def test_public_topics_have_honest_and_topic_specific_sources(topic):
    facts, sources = public_topic(TutorInput(topic=topic))
    assert facts and len(sources) == 1
    if topic in ("confidence", "detection_status"):
        assert sources[0].url is None
    else:
        assert sources[0].url.endswith("/privacy.html")


def test_tutor_http_requires_auth_second_step_and_rejects_extra_context(api):  # noqa: F811
    client, _, headers, owner = api
    counter, writer = MemoryUsage(), StubTutor()
    app.dependency_overrides[tutor_usage] = lambda: counter
    app.dependency_overrides[tutor_writer] = lambda: writer
    assert client.post("/v1/assistant/tutor", json={"topic": "confidence"}).status_code == 401
    factors = app.dependency_overrides[second_factors]()
    factors.people.add(owner)
    assert client.post("/v1/assistant/tutor", json={"topic": "confidence"}, headers=headers()).status_code == 403
    assert not counter.spent and not writer.requests
    for extra in ({"question": "Private context"}, {"messages": []}, {"notes": "Private note"}, {"audio": "binary"}, {"photo": "binary"}):
        assert client.post("/v1/assistant/tutor", json={"topic": "confidence", **extra}, headers=headers(aal="aal2")).status_code == 422
    result = client.post("/v1/assistant/tutor", json={"topic": "confidence", "locale": "en"}, headers=headers(aal="aal2"))
    assert result.status_code == 200
    assert set(result.json()) == {"topic", "species", "locale", "paragraphs", "sources", "model", "generated_at", "usage"}
    assert result.json()["locale"] == "en" and writer.requests[0][1]["locale"] == "en"
