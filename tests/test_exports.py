"""Exports stay below the platform response budget and continue across tied timestamps."""
import csv
import io
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from birdnet_api.contracts import DetectionInput
from birdnet_api.exports import ExportRepository
from birdnet_api.settings import DEFAULT_EXPORT_BODY_BYTES
from tests.prepare_database import TEST_DATABASE_URL
from tests.test_api import detection
from tests.test_postgres import database  # noqa: F401 (fixture)


def test_export_enforces_utf8_bytes_even_when_all_rows_fit_the_row_limit():
    row = {"id": uuid4(), "especie": "Turdus fuscater", "confianza": 0.9, "estado": "confirmada",
           "momento": datetime(2026, 11, 14, tzinfo=UTC), "latitude": 4.679, "longitude": -74.123,
           "version_modelo": "birdnet-v2.4", "nombre": None, "especie_modelo": None,
           "revisada_por": None, "nota": "á" * 500, "individuos": 2}

    class Connection:
        def execute(self, *_):
            return self

        def fetchall(self):
            return [row] * 20_000

    class MemoryExport(ExportRepository):
        @contextmanager
        def transaction(self, owner):
            yield Connection()

    page = MemoryExport().export_page(uuid4(), None, None, None, None)
    assert len(page.body.encode("utf-8")) <= DEFAULT_EXPORT_BODY_BYTES
    assert page.truncated and page.after == row["momento"] and page.after_id == row["id"]
    assert len(list(csv.DictReader(io.StringIO(page.body)))) < 20_000


@pytest.mark.skipif(os.environ.get("BIRDNET_TEST_DATABASE_URL") != TEST_DATABASE_URL, reason="Isolated local database only")
def test_export_cursor_keeps_tied_times_and_discarded_rows(database, monkeypatch):  # noqa: F811
    repo, owner, _ = database
    rows = [DetectionInput.model_validate(detection()) for _ in range(5)]
    repo.batch(owner, rows)
    repo.review(owner, rows[0].id, "discard", None)
    monkeypatch.setenv("EXPORT_ROW_LIMIT", "2")
    exporter = ExportRepository()
    after = after_id = None
    found = []
    while True:
        page = exporter.export_page(owner, None, None, None, None, after, after_id)
        found.extend(csv.DictReader(io.StringIO(page.body)))
        if not page.truncated:
            break
        after, after_id = page.after, page.after_id
    assert {row["id"] for row in found} == {str(row.id) for row in rows}
    assert len(found) == 5 and any(row["status"] == "discarded" for row in found)
