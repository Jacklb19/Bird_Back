"""Account deletion against local stand-ins of Storage and Auth: order, batches, retries and failures.

That removing the user cascades through every table is checked against PostgreSQL in test_postgres.py.
"""
import json
import time
from functools import partial

import httpx
import pytest

from birdnet_api.app import accounts, app
from birdnet_api.accounts import AccountDeletion
from birdnet_api.settings import get_settings
from tests.test_api import api  # noqa: F401  (fixture)

CONFIRMED = {"confirmation": "DELETE"}


def signed_in(seconds_ago=0):
    """Claim of a session whose password was given `seconds_ago`; deletion asks for a recent one."""
    return {"amr": [{"method": "password", "timestamp": int(time.time()) - seconds_ago}]}


@pytest.fixture
def deletion(api, monkeypatch):  # noqa: F811
    client, _, headers, owner = api
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
    get_settings.cache_clear()
    state = {"objects": {"avatars": {f"{owner}/avatar.webp"}, "recordings": {f"{owner}/{n}.webm" for n in range(1500)}},
             "users": {str(owner)}, "log": [], "fail": None}

    def serve(request):
        path = request.url.path
        assert request.headers["apikey"] == "test-service-key"
        if state["fail"] and state["fail"] in path:
            return httpx.Response(500)
        if path.startswith("/storage/v1/object/list/"):
            bucket = path.rsplit("/", 1)[1]
            body = json.loads(request.content)
            folder = sorted(key for key in state["objects"][bucket] if key.startswith(body["prefix"] + "/"))
            page = folder[body["offset"]:body["offset"] + body["limit"]]
            return httpx.Response(200, json=[{"name": key.split("/", 1)[1], "id": key} for key in page])
        if path.startswith("/storage/v1/object/") and request.method == "DELETE":
            bucket = path.rsplit("/", 1)[1]
            keys = json.loads(request.content)["prefixes"]
            state["log"].append(("remove", bucket, len(keys)))
            state["objects"][bucket] -= set(keys)
            return httpx.Response(200, json=[])
        if path.startswith("/auth/v1/admin/users/") and request.method == "DELETE":
            user = path.rsplit("/", 1)[1]
            state["log"].append(("user", user))
            if user not in state["users"]:
                return httpx.Response(404)
            # Supabase refuses to delete a user who still owns Storage objects.
            if any(key.startswith(user) for keys in state["objects"].values() for key in keys):
                return httpx.Response(400)
            state["users"].discard(user)
            return httpx.Response(200, json={})
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(serve)))
    app.dependency_overrides[accounts] = AccountDeletion
    yield client, headers, owner, state
    get_settings.cache_clear()


def test_account_deletion_removes_storage_first_then_the_user(deletion):
    client, headers, owner, state = deletion
    response = client.request("DELETE", "/v1/me", json=CONFIRMED, headers=headers(**signed_in()))
    assert response.status_code == 204
    assert state["objects"] == {"avatars": set(), "recordings": set()} and state["users"] == set()
    # At most a thousand objects per removal, every removal before the user.
    assert state["log"] == [("remove", "avatars", 1), ("remove", "recordings", 1000), ("remove", "recordings", 500), ("user", str(owner))]
    # Repeating after a lost answer succeeds: nothing is left to list and the user is already gone.
    assert client.request("DELETE", "/v1/me", json=CONFIRMED, headers=headers(**signed_in())).status_code == 204


def test_account_deletion_needs_the_confirmation_and_a_session(deletion):
    client, headers, _, state = deletion
    assert client.request("DELETE", "/v1/me", headers=headers(**signed_in())).status_code == 422
    assert client.request("DELETE", "/v1/me", json={"confirmation": "delete"}, headers=headers(**signed_in())).status_code == 422
    assert client.request("DELETE", "/v1/me", json=CONFIRMED).status_code == 401
    assert state["log"] == []


def test_account_deletion_needs_a_recent_sign_in(deletion):
    client, headers, owner, state = deletion
    # A session open for long (a borrowed phone, a stolen token) or naming no sign-in deletes nothing.
    for claims in (signed_in(seconds_ago=11 * 60), {}, {"amr": "password"}, {"amr": [{"method": "password", "timestamp": "now"}]}):
        response = client.request("DELETE", "/v1/me", json=CONFIRMED, headers=headers(**claims))
        assert response.status_code == 403
    assert state["log"] == [] and str(owner) in state["users"]
    # The latest method counts: an old password followed by a fresh authenticator code is recent.
    second = {"amr": [{"method": "password", "timestamp": int(time.time()) - 3600}, {"method": "totp", "timestamp": int(time.time()) - 30}]}
    assert client.request("DELETE", "/v1/me", json=CONFIRMED, headers=headers(**second)).status_code == 204


@pytest.mark.parametrize("failing", ["/object/list/recordings", "/admin/users/"])
def test_a_failed_step_reports_the_account_as_not_deleted(deletion, failing):
    client, headers, owner, state = deletion
    state["fail"] = failing
    response = client.request("DELETE", "/v1/me", json=CONFIRMED, headers=headers(**signed_in()))
    assert response.status_code == 502
    # The user stays in both cases, so the account can be deleted again; nothing in the database was touched.
    assert str(owner) in state["users"]
    state["fail"] = None
    assert client.request("DELETE", "/v1/me", json=CONFIRMED, headers=headers(**signed_in())).status_code == 204
    assert state["users"] == set()
