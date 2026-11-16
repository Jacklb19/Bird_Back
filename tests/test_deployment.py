"""Keep test servers, credentials and model binaries outside the function package."""
import json
import tomllib
from pathlib import Path

from birdnet_api.domain import AUDIO_SAMPLE_RATE_HZ, AUDIO_WINDOW_SAMPLES
from birdnet_api.model_species import MODEL_SPECIES_FILE, model_species
from tests.test_api import api  # noqa: F401 (fixture)

BACKEND = Path(__file__).resolve().parents[1]
# Versioned files stay under 1 MB (AGENTS.md).
MAX_VERSIONED_BYTES = 1_000_000


def test_selected_python_manifest_declares_runtime_dependencies():
    required = {
        line.strip() for line in (BACKEND / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    manifest = BACKEND / "pyproject.toml"
    # Vercel prioritizes this file even when it contains only development-tool settings.
    declared = set(tomllib.loads(manifest.read_text(encoding="utf-8")).get("project", {}).get("dependencies", [])) if manifest.exists() else required
    assert any(dependency.startswith("fastapi==") for dependency in required)
    assert required <= declared, "The manifest selected by Vercel must include every locked runtime dependency"


def test_routes_live_at_the_api_root(api):  # noqa: F811
    client, _, _, _ = api
    assert client.get("/v1/health").json() == {"status": "ok"}
    assert client.get("/v1/model/latest").status_code == 200
    # No published schema or docs: they listed every route to anyone (audit of 2026-11-02).
    assert all(client.get(path).status_code == 404 for path in ("/openapi.json", "/docs", "/redoc"))


def test_shipped_manifest_is_complete():
    manifest = json.loads((BACKEND / "birdnet_api" / "model_manifest.json").read_text(encoding="utf-8-sig"))
    # The bundled model must take the window the client records.
    assert manifest["sample_rate"] == AUDIO_SAMPLE_RATE_HZ and manifest["window_samples"] == AUDIO_WINDOW_SAMPLES
    assert manifest["num_classes"] > 0 and len(manifest["sha256"]) == 64


def test_function_has_explicit_budget_and_exclusions():
    config = json.loads((BACKEND / "vercel.json").read_text(encoding="utf-8-sig"))
    function = config["functions"]["api/index.py"]
    assert function["maxDuration"] == 60
    assert "tests/**" in function["excludeFiles"]
    assert config["rewrites"][0] == {"source": "/(.*)", "destination": "/api/index.py"}



def test_shipped_model_species_list_is_valid_and_small():
    species = model_species()
    assert "Turdus fuscater" in species and "Engine" not in species and len(species) > 6000
    assert MODEL_SPECIES_FILE.stat().st_size < MAX_VERSIONED_BYTES
