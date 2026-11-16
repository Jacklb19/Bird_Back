"""Exercise the actual new REST adapter with mock HTTP, including malformed and blocked responses."""
import json
from functools import partial
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from birdnet_api.beta.interpretation import SCHEMA, interpret
from birdnet_api.beta.text_contracts import InterpretInput
from birdnet_api.beta.text_errors import TextError, TextErrorCode
from birdnet_api.beta.text_model import GeminiStructuredWriter
from birdnet_api.errors import ErrorCode
from birdnet_api.language_model import MAX_OUTPUT_TOKENS
from tests.test_assistant_text import CURRENT, Counter, ready
from tests.test_assistant_text_contracts import CONSENT
from tests.test_language_texts import gemini  # noqa: F401


def response_for(value, finish="STOP", **extra):
    return {"candidates": [{"content": {"parts": [{"text": "private thought", "thought": True}, {"text": json.dumps(value)}]}, "finishReason": finish}], **extra}


def test_real_text_transport_keeps_only_explicit_text_and_uses_current_limits(gemini):  # noqa: F811
    seen, answers = gemini
    answers.append(httpx.Response(200, json=response_for(ready(), usageMetadata={"promptTokenCount": 13, "candidatesTokenCount": 8, "totalTokenCount": 26})))
    owner, counter = uuid4(), Counter()
    result = interpret(owner, InterpretInput(text="Only this explicit text", tz="America/New_York", limit=2, consent=CONSENT), CURRENT, counter, GeminiStructuredWriter())
    assert result.status == "ready" and result.usage.model_dump() == {"input_tokens": 13, "output_tokens": 8, "total_tokens": 26}
    request = seen[0]
    assert request.headers["x-goog-api-key"] == "test-key" and request.url.path.endswith("/models/gemini-test:generateContent")
    body = json.loads(request.content)
    assert body["generationConfig"]["maxOutputTokens"] == MAX_OUTPUT_TOKENS
    assert body["generationConfig"]["responseSchema"] == SCHEMA and body["generationConfig"]["responseMimeType"] == "application/json"
    assert "tools" not in body and "cachedContent" not in body
    facts = json.loads(body["contents"][0]["parts"][0]["text"])
    assert facts == {"text": "Only this explicit text", "locale": "es"}
    assert str(owner) not in request.content.decode() and "America/New_York" not in request.content.decode()
    assert counter.spent[owner] == 1 and counter.refunds == 0


@pytest.mark.parametrize("body", [
    b"private-canary malformed JSON", b'{"candidates":[]}',
    json.dumps(response_for(ready(), finish="MAX_TOKENS")).encode(),
    json.dumps(response_for(ready(), finish="SAFETY")).encode(),
    json.dumps(response_for(ready(), finish=None)).encode(),
    json.dumps(response_for({"status": "ready", "plan": {"sql": "private-canary"}, "reason_code": None})).encode(),
])
def test_real_transport_invalid_decisions_refund_without_retries_or_echo(gemini, body, caplog):  # noqa: F811
    seen, answers = gemini
    answers.append(httpx.Response(200, content=body))
    owner, counter = uuid4(), Counter()
    with pytest.raises(TextError) as error:
        interpret(owner, InterpretInput(text="Explicit text", consent=CONSENT), CURRENT, counter, GeminiStructuredWriter())
    assert error.value.kind is TextErrorCode.MODEL_OUTPUT_INVALID
    assert counter.spent[owner] == 0 and counter.refunds == 1 and len(seen) == 1
    assert "private-canary" not in str(error.value) and "private-canary" not in caplog.text


@pytest.mark.parametrize("status", [429, 500])
def test_upstream_error_refunds_one_unit_and_never_retries(gemini, status):  # noqa: F811
    seen, answers = gemini
    answers.append(httpx.Response(status, text="private-canary upstream diagnostic"))
    owner, counter = uuid4(), Counter()
    with pytest.raises(HTTPException) as error:
        interpret(owner, InterpretInput(text="Explicit text", consent=CONSENT), CURRENT, counter, GeminiStructuredWriter())
    assert error.value.status_code == 503 and error.value.detail == ErrorCode.LLM_UNAVAILABLE.message
    assert counter.spent[owner] == 0 and counter.refunds == 1 and len(seen) == 1


def test_real_transport_timeout_is_private_and_refunded(monkeypatch, caplog):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    original_client = httpx.Client
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-canary transport detail", request=request)

    monkeypatch.setattr(httpx, "Client", partial(original_client, transport=httpx.MockTransport(timeout)))
    owner, counter = uuid4(), Counter()
    with pytest.raises(HTTPException) as error:
        interpret(owner, InterpretInput(text="Explicit text", consent=CONSENT), CURRENT, counter, GeminiStructuredWriter())
    assert error.value.detail == ErrorCode.LLM_UNAVAILABLE.message and "private-canary" not in caplog.text
    assert len(calls) == 1 and counter.spent[owner] == 0 and counter.refunds == 1


def test_provider_usage_absent_or_invalid_stays_unknown(gemini):  # noqa: F811
    _, answers = gemini
    for metadata in (None, {"promptTokenCount": True, "candidatesTokenCount": -1, "totalTokenCount": "20"}):
        answers.append(httpx.Response(200, json=response_for(ready(), usageMetadata=metadata)))
        result = interpret(uuid4(), InterpretInput(text="Show my species", consent=CONSENT), CURRENT, Counter(), GeminiStructuredWriter())
        assert result.usage.model_dump() == {"input_tokens": None, "output_tokens": None, "total_tokens": None}


def test_local_quota_exhaustion_never_calls_the_real_adapter(gemini):  # noqa: F811
    seen, _ = gemini
    with pytest.raises(HTTPException) as error:
        interpret(uuid4(), InterpretInput(text="Show my species", consent=CONSENT), CURRENT, Counter(per_person=0), GeminiStructuredWriter())
    assert error.value.status_code == 429 and seen == []
