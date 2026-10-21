"""Texts of the language model: the answer parser, the figure check of reports and the HTTP contracts of cards and
reports, with the model, the public sources and the database replaced by local stand-ins.

The caps and the RLS of the tables are SQL; test_postgres.py checks them against PostgreSQL.
"""
import json
from datetime import UTC, datetime, timedelta
from functools import partial
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from birdnet_api import species_cards
from birdnet_api.app import app, now, site_reports, sites, usage, writer
from birdnet_api.app import species_cards as card_repository
from birdnet_api.contracts import SiteStats, SpeciesStat
from birdnet_api.domain import SITE_REPORT_INSTRUCTIONS_VERSION, SPECIES_CARD_INSTRUCTIONS_VERSION, StatsPeriod
from birdnet_api.errors import ErrorCode, raise_error
from birdnet_api.language_model import GeminiWriter, parse_answer
from birdnet_api.public_sources import Source, SpeciesFacts, parse_summary, parse_taxon
from birdnet_api.settings import get_settings
from birdnet_api.site_reports import allowed_numbers, fingerprint, numbers_in, only_given_numbers, report_facts
from tests.test_api import api  # noqa: F401  (fixture)

CURRENT = datetime(2026, 10, 21, 15, tzinfo=UTC)
MODEL = "stub-model"


def gemini_answer(paragraphs, finish="STOP", thought=None):
    parts = ([{"text": thought, "thought": True}] if thought else []) + [{"text": json.dumps({"paragraphs": paragraphs})}]
    return json.dumps({"candidates": [{"content": {"parts": parts}, "finishReason": finish}]}).encode()


def test_answer_parser_keeps_only_a_finished_text_of_paragraphs():
    assert parse_answer(gemini_answer(["Uno.", "  Dos   tres. "])) == ["Uno.", "Dos tres."]
    # The model's reasoning is not part of the answer.
    assert parse_answer(gemini_answer(["Uno."], thought="thinking")) == ["Uno."]
    assert parse_answer(gemini_answer(["Uno."], finish="MAX_TOKENS")) is None
    assert parse_answer(gemini_answer(["Uno."], finish="SAFETY")) is None
    assert parse_answer(gemini_answer([])) is None
    assert parse_answer(gemini_answer(["x"] * 5)) is None
    assert parse_answer(gemini_answer(["x" * 901])) is None
    assert parse_answer(b'{"candidates": []}') is None
    assert parse_answer(b"not json") is None


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    get_settings.cache_clear()
    seen = []
    answers = []

    def serve(request):
        seen.append(request)
        return answers.pop(0)

    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(serve)))
    yield seen, answers
    get_settings.cache_clear()


def test_gemini_writer_sends_the_facts_and_reports_a_missing_answer(gemini):
    seen, answers = gemini
    answers.append(httpx.Response(200, content=gemini_answer(["Texto."])))
    assert GeminiWriter().write("Instructions", {"species": "Turdus fuscater"}) == ["Texto."]
    request = seen[0]
    assert request.url.path.endswith("/models/gemini-test:generateContent")
    assert request.headers["x-goog-api-key"] == "test-key"
    body = json.loads(request.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "Instructions"
    assert json.loads(body["contents"][0]["parts"][0]["text"]) == {"species": "Turdus fuscater"}
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    # The project's free quota, an error and an unusable text all read as "retry later", never as an empty text.
    for response in (httpx.Response(429), httpx.Response(500), httpx.Response(200, content=gemini_answer(["x"], finish="MAX_TOKENS"))):
        answers.append(response)
        with pytest.raises(HTTPException) as error:
            GeminiWriter().write("Instructions", {})
        assert error.value.detail == ErrorCode.LLM_UNAVAILABLE.message


def test_gemini_writer_without_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as error:
        GeminiWriter().write("Instructions", {})
    assert error.value.status_code == 503 and error.value.detail == ErrorCode.LLM_NOT_CONFIGURED.message


def test_public_source_parsers_trust_only_an_exact_bird_species():
    exact = {"matchType": "EXACT", "rank": "SPECIES", "kingdom": "Animalia", "class": "Aves", "usageKey": 7, "order": "Passeriformes", "family": "Turdidae", "genus": "Turdus"}
    assert parse_taxon(exact) == (7, "Passeriformes", "Turdidae", "Turdus")
    assert parse_taxon({**exact, "acceptedUsageKey": 9}) == (9, "Passeriformes", "Turdidae", "Turdus")
    assert parse_taxon({**exact, "matchType": "FUZZY"}) is None
    assert parse_taxon({**exact, "class": "Insecta"}) is None
    assert parse_taxon({**exact, "usageKey": True}) is None
    page = {"type": "standard", "extract": " Texto. ", "content_urls": {"desktop": {"page": "https://es.wikipedia.org/wiki/Turdus_fuscater"}}}
    assert parse_summary(page) == ("Texto.", "https://es.wikipedia.org/wiki/Turdus_fuscater")
    assert parse_summary({**page, "type": "disambiguation"}) is None
    assert parse_summary({**page, "content_urls": {"desktop": {"page": "http://insecure"}}}) is None


def stats(detections=12, species=None, previous=3, missing=None, period=StatsPeriod.MONTH):
    hourly = [0] * 24
    hourly[6] = detections
    species = species if species is not None else [SpeciesStat(species="Turdus fuscater", detections=detections, days=4, first_seen=CURRENT - timedelta(days=3), is_new=True)]
    return SiteStats(period=period, since=CURRENT - timedelta(days=30) if period.days else None, until=CURRENT, species_count=len(species),
                     previous_species_count=previous if period.days else None, detections=detections, active_days=4, hourly=hourly,
                     species=species, missing=missing if missing is not None else ["Zonotrichia capensis"])


def test_report_figures_are_computed_before_the_model_sees_them():
    facts = report_facts(stats(), "America/Bogota", {"Turdus fuscater": "Mirla patiamarilla"})
    assert facts["species"][0]["name"] == "Mirla patiamarilla"
    assert facts["busiest_hour"] == "06:00-07:00" and facts["detections_in_busiest_hour"] == 12
    assert facts["difference_in_species_detected"] == 2 and facts["more_or_fewer_species_than_previous_period"] == "fewer"
    # Species not heard lately are labelled as not detected, never as absent.
    assert facts["species_detected_before_but_not_in_this_period"] == ["Zonotrichia capensis"]
    assert facts["from_date"] == "2026-09-21" and facts["to_date"] == "2026-10-21"


def test_report_text_may_only_write_numbers_of_the_figures():
    facts = report_facts(stats(), "America/Bogota", {})
    assert {12, 4, 6, 7, 30, 2026, 21, 3, 2} <= allowed_numbers(facts)
    assert only_given_numbers(["Se oyeron 12 cantos en 4 días, sobre todo de 06:00 a 07:00, del 21 de septiembre de 2026."], facts)
    assert not only_given_numbers(["El 50 % de los cantos fueron de mirla."], facts)
    assert not only_given_numbers(["Hubo 13 cantos."], facts)
    assert numbers_in("1.234 cantos y 1,234 más") == {1234}


def test_fingerprint_ignores_the_moving_period_but_not_the_figures():
    first = stats()
    later = first.model_copy(update={"since": first.since + timedelta(minutes=5), "until": first.until + timedelta(minutes=5)})
    assert fingerprint(first, "America/Bogota") == fingerprint(later, "America/Bogota")
    assert fingerprint(first, "America/Bogota") != fingerprint(stats(detections=13), "America/Bogota")
    assert fingerprint(first, "America/Bogota") != fingerprint(first, "UTC")


class StubWriter:
    model = MODEL

    def __init__(self):
        self.answers = []
        self.calls = []

    def write(self, instructions, facts):
        self.calls.append(facts)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class MemoryUsage:
    def __init__(self, per_person=2):
        self.spent = {}
        self.per_person = per_person

    def spend(self, owner, current, per_person, total):
        if self.spent.get(owner, 0) >= self.per_person:
            raise_error(ErrorCode.LLM_QUOTA_EXCEEDED)
        self.spent[owner] = self.spent.get(owner, 0) + 1

    def give_back(self, owner, current):
        self.spent[owner] -= 1


class MemoryCards:
    def __init__(self):
        self.rows = {}

    def get(self, species, language):
        return self.rows.get((species, language.value))

    def save(self, species, language, paragraphs, sources, model):
        row = {"especie": species, "idioma": language.value, "parrafos": paragraphs, "fuentes": [s.model_dump() for s in sources],
               "modelo": model, "version_instrucciones": SPECIES_CARD_INSTRUCTIONS_VERSION, "generated_at": CURRENT}
        self.rows[(species, language.value)] = row
        return row


FACTS = SpeciesFacts("Turdus fuscater", "Passeriformes", "Turdidae", "Turdus", "LC", "La mirla patiamarilla...", "es",
                     (Source("GBIF", "https://www.gbif.org/species/7", "CC BY 4.0"), Source("Wikipedia", "https://es.wikipedia.org/wiki/Turdus_fuscater", "CC BY-SA 4.0")))


@pytest.fixture
def texts(api, monkeypatch):  # noqa: F811
    client, _, headers, owner = api
    model, counter, cards = StubWriter(), MemoryUsage(), MemoryCards()
    asked = []

    def gather(species, language):
        asked.append(species)
        if species == "Unknown bird":
            raise_error(ErrorCode.SPECIES_UNKNOWN)
        return FACTS

    monkeypatch.setattr(species_cards, "gather", gather)
    app.dependency_overrides[writer] = lambda: model
    app.dependency_overrides[usage] = lambda: counter
    app.dependency_overrides[card_repository] = lambda: cards
    app.dependency_overrides[now] = lambda: CURRENT
    return client, headers, owner, model, counter, cards, asked


def test_a_card_is_written_once_and_read_by_anyone(texts):
    client, headers, owner, model, counter, _, asked = texts
    url = "/v1/species/Turdus fuscater/card"
    assert client.get(url, params={"locale": "es"}).status_code == 404
    # Writing needs an account; reading does not.
    assert client.post(url, params={"locale": "es"}).status_code == 401
    model.answers.append(["La mirla patiamarilla (Turdus fuscater) es un ave de la familia Turdidae."])
    written = client.post(url, params={"locale": "es"}, headers=headers())
    assert written.status_code == 200
    body = written.json()
    assert body["paragraphs"] == ["La mirla patiamarilla (Turdus fuscater) es un ave de la familia Turdidae."]
    assert [s["name"] for s in body["sources"]] == ["GBIF", "Wikipedia"] and body["stale"] is False
    # Only public facts reach the model, with the Red List code spelled out.
    assert model.calls[0]["iucn_red_list_category"] == "LC (Least Concern)" and str(owner) not in json.dumps(model.calls[0])
    again = client.post(url, params={"locale": "es"}, headers=headers(uuid4()))
    assert again.json() == body and len(model.calls) == 1 and sum(counter.spent.values()) == 1
    assert client.get(url, params={"locale": "es"}).json() == body
    assert asked == ["Turdus fuscater"]


def test_card_names_are_checked_before_anything_is_asked(texts):
    client, headers, _, model, counter, _, asked = texts
    for name in ("turdus fuscater", "Turdus", "Turdus fuscater 2", "Turdus fuscater fuscater"):
        assert client.post(f"/v1/species/{name}/card", headers=headers()).status_code == 422
    assert client.post("/v1/species/Turdus fuscater/card", params={"locale": "fr"}, headers=headers()).status_code == 422
    assert asked == [] and model.calls == [] and counter.spent == {}
    # A well-formed name GBIF does not know counts against the caller, so guessing names is not free.
    assert client.post("/v1/species/Unknown bird/card", headers=headers()).status_code == 422
    assert model.calls == [] and sum(counter.spent.values()) == 1


def test_card_quota_and_failures(texts):
    client, headers, owner, model, counter, cards, _ = texts
    model.answers.append(HTTPException(503, ErrorCode.LLM_UNAVAILABLE.message))
    assert client.post("/v1/species/Turdus fuscater/card", headers=headers()).status_code == 503
    # A text the model did not write is given back.
    assert counter.spent[owner] == 0
    counter.spent[owner] = counter.per_person
    assert client.post("/v1/species/Turdus fuscater/card", headers=headers()).status_code == 429
    # A stale card is written again; until then it is still shown, marked stale.
    counter.spent[owner] = 0
    model.answers.append(["Primera."])
    client.post("/v1/species/Turdus fuscater/card", headers=headers())
    cards.rows[("Turdus fuscater", "es")]["generated_at"] = CURRENT - timedelta(days=181)
    assert client.get("/v1/species/Turdus fuscater/card").json()["stale"] is True
    model.answers.append(["Segunda."])
    assert client.post("/v1/species/Turdus fuscater/card", headers=headers()).json()["paragraphs"] == ["Segunda."]


class MemoryReports:
    def __init__(self):
        self.rows = {}

    def get(self, owner, site_id, period, language):
        return self.rows.get((owner, site_id, period.value, language.value))

    def save(self, owner, site_id, figures, tz, language, paragraphs, model):
        row = {"periodo": figures.period.value, "idioma": language.value, "zona_horaria": tz, "cifras": figures.model_dump(mode="json"),
               "huella": fingerprint(figures, tz), "parrafos": paragraphs, "modelo": model,
               "version_instrucciones": SITE_REPORT_INSTRUCTIONS_VERSION, "generated_at": CURRENT}
        self.rows[(owner, site_id, figures.period.value, language.value)] = row
        return row


class StubSites:
    def __init__(self):
        self.figures = stats()
        self.owner = None

    def stats(self, owner, site_id, period, since, previous, until, tz):
        if owner != self.owner:
            raise_error(ErrorCode.SITE_NOT_FOUND)
        return self.figures


@pytest.fixture
def reports(texts):
    client, headers, owner, model, counter, _, _ = texts
    stub, stored = StubSites(), MemoryReports()
    stub.owner = owner
    app.dependency_overrides[sites] = lambda: stub
    app.dependency_overrides[site_reports] = lambda: stored
    return client, headers, owner, model, counter, stub, stored


def test_a_report_is_written_only_when_asked_and_reused_while_figures_stay(reports):
    client, headers, owner, model, counter, stub, _ = reports
    site = uuid4()
    url = f"/v1/sites/{site}/report"
    assert client.get(url, params={"period": "month"}, headers=headers()).status_code == 404
    model.answers.append(["Este mes se oyeron 12 cantos de 1 especie en 4 días."])
    body = {"period": "month", "tz": "America/Bogota", "locale": "es", "names": {"Turdus fuscater": "Mirla patiamarilla"}}
    written = client.post(url, json=body, headers=headers())
    assert written.status_code == 200 and written.json()["stats"]["detections"] == 12 and written.json()["stale"] is False
    # Names replace scientific ones; the site's name and place are never part of what the model reads.
    assert model.calls[0]["species"][0]["name"] == "Mirla patiamarilla"
    assert set(model.calls[0]) & {"site", "site_name", "latitude", "longitude"} == set()
    assert client.post(url, json=body, headers=headers()).json() == written.json() and len(model.calls) == 1
    stub.figures = stats(detections=13)
    assert client.get(url, params={"period": "month", "tz": "America/Bogota"}, headers=headers()).json()["stale"] is True
    assert counter.spent[owner] == 1
    # Another person's site is not found, and nothing is spent on it.
    assert client.post(url, json=body, headers=headers(uuid4())).status_code == 404


def test_a_report_with_invented_numbers_is_refused(reports):
    client, headers, owner, model, counter, _, stored = reports
    model.answers.extend([["El 80 % fueron mirlas."], ["Hubo 99 cantos."]])
    response = client.post(f"/v1/sites/{uuid4()}/report", json={"period": "month"}, headers=headers())
    assert response.status_code == 503 and len(model.calls) == 2
    assert counter.spent[owner] == 0 and stored.rows == {}


def test_a_report_needs_something_heard_and_valid_names(reports):
    client, headers, _, model, _, stub, _ = reports
    stub.figures = stats(detections=0, species=[], missing=[])
    assert client.post(f"/v1/sites/{uuid4()}/report", json={"period": "month"}, headers=headers()).status_code == 422
    for names in ({"Turdus fuscater": "Mirla <b>"}, {"Turdus fuscater": "Mirla 2"}, {f"Species {i}": "Ave" for i in range(51)}):
        assert client.post(f"/v1/sites/{uuid4()}/report", json={"period": "month", "names": names}, headers=headers()).status_code == 422
    assert model.calls == []
