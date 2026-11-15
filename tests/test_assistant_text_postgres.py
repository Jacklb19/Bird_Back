"""Explicitly confirmed natural queries use PostgreSQL/RLS and never return rows to the provider."""
from fastapi.testclient import TestClient

from birdnet_api.app import app
from birdnet_api.auth import authenticate
from birdnet_api.beta.contracts import TutorInput
from birdnet_api.beta.text_routes import current_time, text_writer
from birdnet_api.beta.tutor import tutor
from birdnet_api.contracts import DetectionInput
from birdnet_api.llm_usage import UsageRepository, quota_day
from tests.test_api import detection
from tests.test_assistant_text import CURRENT, StructuredStub, ready
from tests.test_assistant_text_contracts import CONSENT
from tests.test_beta_tutor import StubTutor
from tests.test_postgres import database, pytestmark  # noqa: F401


def units(owner):
    with UsageRepository().trusted_transaction() as connection:
        row = connection.execute("SELECT textos FROM public.llm_usage WHERE user_id=%s AND dia=%s", (owner, quota_day(CURRENT).date())).fetchone()
    return row["textos"] if row else 0


def test_interpret_confirm_query_paginate_uses_rls_without_another_provider_call(database, monkeypatch):  # noqa: F811
    repo, owner, other = database
    monkeypatch.setenv("LLM_DAILY_QUOTA", "100000")
    rows = [DetectionInput.model_validate(detection(recorded_at="2026-11-13T12:00:00Z")) for _ in range(2)]
    repo.batch(owner, rows)
    repo.batch(other, [DetectionInput.model_validate(detection(recorded_at="2026-11-13T12:00:00Z", shared=True))])
    writer = StructuredStub(ready("detections", species="Turdus fuscater", period={"kind": "relative", "value": "yesterday"}))
    app.dependency_overrides[authenticate] = lambda: owner
    app.dependency_overrides[text_writer] = lambda: writer
    app.dependency_overrides[current_time] = lambda: CURRENT
    try:
        with TestClient(app) as client:
            response = client.post("/v1/assistant/interpret", json={"text": "Mis detecciones de Turdus fuscater ayer", "consent": CONSENT, "limit": 1})
            assert response.status_code == 200 and units(owner) == 1
            plan = response.json()["query"]
            assert "totals" not in response.json()
            before_execution = list(writer.requests)
            first = client.post("/v1/assistant/query", json=plan).json()
            last = first["detections"][-1]
            second = client.post("/v1/assistant/query", json={**plan, "before": last["recorded_at"], "after_id": last["id"]}).json()
            assert first["totals"] == second["totals"] == {"detections": 2, "species": 1}
            assert first["has_more"] and not second["has_more"]
            assert {row["id"] for row in first["detections"] + second["detections"]} == {str(row.id) for row in rows}
            assert units(owner) == 1 and writer.requests == before_execution
            assert writer.requests[0][1] == {"text": "Mis detecciones de Turdus fuscater ayer", "locale": "es"}
    finally:
        app.dependency_overrides.clear()


def test_new_text_actions_share_real_v1_quota_and_refund_invalid_model_decisions(database, monkeypatch):  # noqa: F811
    _, owner, _ = database
    monkeypatch.setenv("LLM_USER_DAILY_QUOTA", "2")
    monkeypatch.setenv("LLM_DAILY_QUOTA", "100000")
    writer = StructuredStub(ready(species="Turdus fuscater"), ready())
    app.dependency_overrides[authenticate] = lambda: owner
    app.dependency_overrides[text_writer] = lambda: writer
    app.dependency_overrides[current_time] = lambda: CURRENT
    try:
        with TestClient(app) as client:
            bad = client.post("/v1/assistant/interpret", json={"text": "Show species", "consent": CONSENT})
            assert bad.status_code == 503 and bad.json()["code"] == "model_output_invalid" and units(owner) == 0
            tutor(owner, TutorInput(topic="confidence"), CURRENT, UsageRepository(), StubTutor())
            assert units(owner) == 1
            assert client.post("/v1/assistant/interpret", json={"text": "Show species", "consent": CONSENT}).status_code == 200
            limited = client.post("/v1/assistant/tutor-question", json={"topic": "confidence", "text": "Explain the model score", "consent": CONSENT})
            assert limited.status_code == 429 and units(owner) == 2 and len(writer.requests) == 2
    finally:
        app.dependency_overrides.clear()
