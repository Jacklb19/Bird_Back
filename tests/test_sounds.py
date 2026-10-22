"""Reference sounds from xeno-canto (ADR-42), notes contract (ADR-40), the threatened list (ADR-41) and error reports (ADR-43)."""
import json
from uuid import uuid4

import httpx
import pytest

from birdnet_api import sounds
from birdnet_api.app import app, notes
from birdnet_api.contracts import DetectionNote
from birdnet_api.domain import MAX_NOTE_LENGTH
from birdnet_api.monitoring import without_personal_data
from birdnet_api.settings import get_settings
from birdnet_api.threatened import parse, threatened_species
from tests.test_api import api  # noqa: F401  (fixture)

SPECIES = "Turdus fuscater"


def recording(number, **changes):
    return {"id": str(number), "gen": "Turdus", "sp": "fuscater", "rec": "Ana Pérez", "cnt": "Colombia", "type": "song", "q": "A",
            "length": "0:24", "lic": "//creativecommons.org/licenses/by-nc-sa/4.0/", "file": f"https://xeno-canto.org/{number}/download",
            "lat": "4.6", "lon": "-74.1", "loc": "Bogotá", **changes}


def test_choice_keeps_what_the_licence_asks_and_drops_places():
    body = {"recordings": [
        recording(1, q="B"), recording(2, type="call"), recording(3, length="0:40"), recording(4),
        recording(5, gen="Turdus", sp="ignobilis"), recording(6, lic="https://example.com/licence"), recording(7, rec=" "),
    ]}
    chosen = sounds.choose(SPECIES, body)
    assert [sound.id for sound in chosen.sounds] == ["XC4", "XC3", "XC2"]
    first = chosen.sounds[0]
    assert (first.recordist, first.license.code, first.license.url) == ("Ana Pérez", "CC BY-NC-SA 4.0", "https://creativecommons.org/licenses/by-nc-sa/4.0/")
    assert (first.page_url, first.audio_url, first.length_seconds) == ("https://xeno-canto.org/4", "https://xeno-canto.org/4/download", 24)
    assert "lat" not in json.dumps(chosen.model_dump()) and "Bogotá" not in json.dumps(chosen.model_dump())
    assert not chosen.restricted


def test_restricted_species_are_said_not_hidden():
    redacted = recording(9, file="restricted_species", _meta={"redacted_fields": {"file": "restricted_species", "lat": "restricted_species"}})
    chosen = sounds.choose(SPECIES, {"recordings": [redacted]})
    assert chosen.restricted and chosen.sounds == []
    assert not sounds.choose(SPECIES, {"recordings": []}).restricted


@pytest.mark.parametrize(("value", "code"), [
    ("https://creativecommons.org/licenses/by/4.0/", "CC BY 4.0"), ("//creativecommons.org/licenses/by-sa/3.0/", "CC BY-SA 3.0"),
    ("https://creativecommons.org/publicdomain/zero/1.0/", "CC0 1.0"), ("https://creativecommons.org/publicdomain/mark/1.0/", None),
    ("https://creativecommons.org.evil/licenses/by/4.0/", None), (None, None)])
def test_licences_are_read_only_from_creative_commons(value, code):
    parsed = sounds.parse_license(value)
    assert (parsed.code if parsed else None) == code


@pytest.mark.parametrize(("value", "seconds"), [("0:24", 24), ("4:08", 248), ("1:02:03", 3723), ("0:00", None), ("24", None), (None, None)])
def test_lengths(value, seconds):
    assert sounds.parse_length(value) == seconds


def test_sounds_route_is_public_cached_and_says_when_it_cannot_answer(api, monkeypatch):  # noqa: F811
    client, *_ = api
    assert client.get(f"/v1/species/{SPECIES}/sounds").status_code == 503
    assert client.get("/v1/species/not a name/sounds").status_code == 422
    monkeypatch.setenv("XENO_CANTO_API_KEY", "secret-key")
    get_settings.cache_clear()
    calls = []

    def answer(request):
        calls.append(request.url)
        return httpx.Response(200, json={"recordings": [recording(1)]})

    transport = httpx.MockTransport(answer)
    original = httpx.Client
    monkeypatch.setattr(sounds.httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs))
    response = client.get(f"/v1/species/{SPECIES}/sounds")
    assert response.status_code == 200 and response.json()["sounds"][0]["id"] == "XC1"
    assert response.headers["Vercel-CDN-Cache-Control"].startswith("max-age=604800")
    assert calls[0].params["query"] == 'sp:"Turdus fuscater" grp:birds q:">C" len:"<60"' and calls[0].params["key"] == "secret-key"
    # The key never goes back to the phone.
    assert "secret-key" not in response.text
    monkeypatch.setattr(sounds.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(lambda _: httpx.Response(500)), **kwargs))
    assert client.get(f"/v1/species/{SPECIES}/sounds").status_code == 502


class MemoryNotes:
    def __init__(self, owner):
        self.owner, self.saved = owner, []

    def save(self, owner, detection, note):
        if owner != self.owner:
            return None
        self.saved.append(note)
        return DetectionNote(id=detection, note=note.note, individuals=note.individuals)


def test_note_contract_bounds_and_trims(api):  # noqa: F811
    client, _, headers, owner = api
    memory = MemoryNotes(owner)
    app.dependency_overrides[notes] = lambda: memory
    url = "/v1/detections/00000000-0000-4000-8000-000000000001/note"
    assert client.put(url, json={"note": "Dos machos", "individuals": 2}).status_code == 401
    for body in ({"note": "x" * (MAX_NOTE_LENGTH + 1)}, {"individuals": 0}, {"individuals": 1000}, {"individuals": "2"}, {"note": "x", "shared": True}):
        assert client.put(url, json=body, headers=headers()).status_code == 422
    response = client.put(url, json={"note": "  Cantaba en el borde  \n", "individuals": None}, headers=headers())
    assert response.json() == {"id": "00000000-0000-4000-8000-000000000001", "note": "Cantaba en el borde", "individuals": None}
    assert client.put(url, json={"note": "   "}, headers=headers()).json()["note"] is None
    assert client.put(url, json={"individuals": 3}, headers=headers(uuid4())).status_code == 404


def test_threatened_list_is_shipped_and_validated():
    assert "Vultur gryphus" in threatened_species()
    assert parse({"species": {"Vultur gryphus": "VU"}}) == frozenset({"Vultur gryphus"})
    for document in ({}, {"species": []}, {"species": {"Turdus fuscater": "LC"}}):
        with pytest.raises(ValueError):
            parse(document)


def test_error_reports_carry_no_personal_data():
    event = {"request": {"method": "GET", "url": "https://api/v1/walks/0a1b2c3d-0000-4000-8000-000000000001?west=-74.1&south=4.6",
                         "headers": {"Authorization": "Bearer x"}, "cookies": "a=b", "data": {"note": "x"}, "query_string": "west=-74.1"},
             "user": {"id": "x", "ip_address": "1.2.3.4"}, "server_name": "host", "breadcrumbs": {"values": []}, "exception": {}}
    cleaned = without_personal_data(event, {})
    assert cleaned["request"] == {"method": "GET", "url": "https://api/v1/walks/{id}"}
    assert {"user", "server_name", "breadcrumbs"}.isdisjoint(cleaned) and "exception" in cleaned


def test_starting_error_reports_leaves_the_settings_to_the_server():
    # Regression: it runs when the API is imported; caching the settings then kept a server from setting its own.
    from birdnet_api.monitoring import start_error_reports

    get_settings.cache_clear()
    start_error_reports()
    assert get_settings.cache_info().currsize == 0
