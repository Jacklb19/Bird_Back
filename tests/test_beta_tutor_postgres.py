"""Tutor generations share the existing per-person/project ledger with species cards."""
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from birdnet_api import beta, species_cards
from birdnet_api.beta.contracts import TutorInput
from birdnet_api.beta.tutor import tutor
from birdnet_api.domain import CardLanguage
from birdnet_api.llm_usage import UsageRepository
from birdnet_api.settings import get_settings
from tests.test_beta_tutor import StubTutor
from tests.test_language_texts import FACTS, MemoryCards, StubWriter
from tests.test_postgres import database, pytestmark  # noqa: F401


def test_tutor_and_cards_consume_the_same_real_quota(database, monkeypatch):  # noqa: F811
    _, owner, _ = database
    current = datetime(2026, 11, 17, 15, tzinfo=UTC)
    monkeypatch.setenv("LLM_USER_DAILY_QUOTA", "2")
    monkeypatch.setenv("LLM_DAILY_QUOTA", "100000")
    get_settings.cache_clear()
    monkeypatch.setattr(species_cards, "gather", lambda *_: FACTS)
    monkeypatch.setattr(beta.tutor, "gather", lambda *_: FACTS)
    usage = UsageRepository()
    card_writer = StubWriter()
    card_writer.answers = [["Una ficha pública."]]
    species_cards.card(owner, "Turdus fuscater", CardLanguage.ES, current, MemoryCards(), usage, card_writer)
    writer = StubTutor()
    tutor(owner, TutorInput(topic="confidence"), current, usage, writer)
    with pytest.raises(HTTPException) as error:
        tutor(owner, TutorInput(topic="confidence"), current, usage, writer)
    assert error.value.status_code == 429 and len(writer.requests) == 1
    usage.give_back(owner, current)
    with pytest.raises(HTTPException) as error:
        tutor(owner, TutorInput(topic="confidence"), current, usage, StubTutor(fail=True))
    assert error.value.status_code == 503
    tutor(owner, TutorInput(topic="confidence"), current, usage, writer)
