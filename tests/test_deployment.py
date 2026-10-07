"""Keep test servers, credentials and model binaries outside the function package."""
import json
from pathlib import Path

from tests.test_api import api

BACKEND = Path(__file__).resolve().parents[1]


def test_routes_live_at_the_api_root(api):
    client, _, _, _, _ = api
    assert client.get("/v1/health").json() == {"status": "ok"}
    assert client.get("/v1/model/latest").status_code == 200


def test_shipped_manifest_is_complete():
    manifest = json.loads((BACKEND / "birdnet_api" / "model_manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["sample_rate"] == 48000 and manifest["num_classes"] > 0 and len(manifest["sha256"]) == 64


def test_function_has_explicit_budget_and_exclusions():
    config = json.loads((BACKEND / "vercel.json").read_text(encoding="utf-8-sig"))
    function = config["functions"]["api/index.py"]
    assert function["maxDuration"] == 60
    assert "tests/**" in function["excludeFiles"]
    assert config["rewrites"][0] == {"source": "/(.*)", "destination": "/api/index.py"}

