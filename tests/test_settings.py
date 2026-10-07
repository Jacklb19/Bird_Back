"""Configuration: defaults, normalization, errors that name the variable, and the manifest origin allow-list."""
from functools import partial

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from birdnet_api import manifest
from birdnet_api.app import app
from birdnet_api.errors import ErrorCode
from birdnet_api.settings import BUNDLED_MODEL_MANIFEST, DEFAULT_MAP_RESULT_LIMIT, DEFAULT_TIME_ZONE, Settings, SettingsError


def test_missing_values_leave_features_unconfigured_and_tunables_at_defaults():
    settings = Settings.from_env({"DATABASE_URL": "  ", "SUPABASE_URL": "https://Project.supabase.co/", "MODEL_RESOURCE_BASE_URL": "https://cdn.example/models/"})
    assert settings.database_url is None and settings.supabase_auth_issuer is None and settings.model_manifest_url is None
    assert settings.supabase_url == "https://Project.supabase.co" and settings.model_resource_base_url == "https://cdn.example/models"
    assert settings.model_manifest_path == BUNDLED_MODEL_MANIFEST
    assert (settings.map_result_limit, settings.default_time_zone) == (DEFAULT_MAP_RESULT_LIMIT, DEFAULT_TIME_ZONE)
    overridden = Settings.from_env({"MAP_RESULT_LIMIT": "10", "STORAGE_TIMEOUT_SECONDS": "2.5", "DEFAULT_TIME_ZONE": "UTC"})
    assert (overridden.map_result_limit, overridden.storage_timeout_seconds, overridden.default_time_zone) == (10, 2.5, "UTC")


@pytest.mark.parametrize(("environ", "hosts"), [
    ({}, set()),
    ({"MODEL_MANIFEST_URL": "https://cdn.example/manifest.json"}, {"cdn.example"}),
    ({"SUPABASE_URL": "https://project.supabase.co", "MODEL_MANIFEST_URL": "https://cdn.example/manifest.json"}, {"project.supabase.co"}),
    ({"SUPABASE_URL": "https://project.supabase.co", "MODEL_MANIFEST_ALLOWED_HOSTS": "A.supabase.co, b.supabase.co"}, {"a.supabase.co", "b.supabase.co"}),
], ids=["none", "manifest-host", "project-host", "explicit"])
def test_manifest_hosts_default_to_the_project(environ, hosts):
    assert Settings.from_env(environ).model_manifest_allowed_hosts == hosts


@pytest.mark.parametrize(("name", "value"), [
    ("MAP_RESULT_LIMIT", "many"),
    ("EXPORT_ROW_LIMIT", "0"),
    ("DATABASE_CONNECT_TIMEOUT_SECONDS", "2.5"),
    ("JWKS_TIMEOUT_SECONDS", "nan"),
    ("STORAGE_TIMEOUT_SECONDS", "-1"),
    ("MAX_METADATA_BODY_BYTES", "5000000"),
    ("DEFAULT_TIME_ZONE", "Mars/Base"),
    ("DEFAULT_TIME_ZONE", "America"),
    ("MODEL_MANIFEST_ALLOWED_HOSTS", "https://project.supabase.co"),
    ("MODEL_MANIFEST_ALLOWED_HOSTS", "project.supabase.co:443"),
])
def test_malformed_values_name_the_variable(name, value):
    with pytest.raises(SettingsError, match=name):
        Settings.from_env({name: value})


def test_invalid_configuration_stops_startup_and_requests(monkeypatch):
    monkeypatch.setenv("MAP_RESULT_LIMIT", "0")
    with pytest.raises(SettingsError), TestClient(app):
        pass
    client = TestClient(app)
    for response in (client.get("/v1/model/latest"), client.post("/v1/sites", json={})):
        assert response.status_code == 503 and response.json() == {"detail": ErrorCode.CONFIGURATION_INVALID.message}


def test_requests_use_the_configured_limits(monkeypatch):
    monkeypatch.setenv("MAX_METADATA_BODY_BYTES", "1024")
    monkeypatch.setenv("MODEL_RESOURCE_BASE_URL", "https://cdn.example/models/")
    with TestClient(app) as client:
        assert client.post("/v1/sites", content=b" " * 1025).status_code == 413
        assert client.get("/v1/model/latest").json()["model_file"] == "https://cdn.example/models/birdnet_model.onnx"


def test_remote_manifest_comes_only_from_allowed_tls_hosts(monkeypatch):
    requested = []

    def serve(request):
        requested.append(request.url.host)
        return httpx.Response(200, json={"model_file": "model.onnx", "labels_file": "https://cdn.example/labels.txt"})

    monkeypatch.setattr(manifest.httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(serve)))
    project = {"SUPABASE_URL": "https://project.supabase.co", "MODEL_RESOURCE_BASE_URL": "https://project.supabase.co/models"}
    served = manifest.load_manifest(Settings.from_env({**project, "MODEL_MANIFEST_URL": "https://project.supabase.co/manifest.json"}))
    assert served == {"model_file": "https://project.supabase.co/models/model.onnx", "labels_file": "https://cdn.example/labels.txt"}
    for url in ("https://foreign.example/manifest.json", "http://project.supabase.co/manifest.json"):
        with pytest.raises(HTTPException) as error:
            manifest.load_manifest(Settings.from_env({**project, "MODEL_MANIFEST_URL": url}))
        assert error.value.status_code == 503
    with pytest.raises(HTTPException):
        manifest.load_manifest(Settings.from_env({**project, "MODEL_MANIFEST_URL": "https://project.supabase.co/manifest.json", "MAX_MANIFEST_BYTES": "8"}))
    assert requested == ["project.supabase.co"] * 2
