"""HTTP contracts of the profile and its photo, with Storage replaced by a local transport."""
from functools import partial
from uuid import uuid4

import httpx
import pytest

from birdnet_api.app import app, avatars, profiles
from birdnet_api.profiles import EMPTY_PROFILE, StoredProfile
from birdnet_api.settings import get_settings
from birdnet_api.storage import AvatarStorage
from tests.test_api import api  # noqa: F401  (fixture)

WEBP = b"RIFF" + (12).to_bytes(4, "little") + b"WEBPVP8 " + bytes(4)


class MemoryProfiles:
    def __init__(self):
        self.rows = {}

    def get(self, owner):
        return self.rows.get(owner, EMPTY_PROFILE)

    def update(self, owner, changes):
        current = self.rows.get(owner, EMPTY_PROFILE)
        self.rows[owner] = StoredProfile(**{"alias": current.alias, "avatar_path": current.avatar_path, "created_at": current.created_at, **changes})
        return self.rows[owner]


@pytest.fixture
def profile_api(api, monkeypatch):  # noqa: F811
    client, _, _, headers, owner = api
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
    # The client fixture already read the settings at startup.
    get_settings.cache_clear()
    objects = {}

    def serve(request):
        path = request.url.path.removeprefix("/storage/v1")
        if path.startswith("/object/upload/sign/"):
            return httpx.Response(200, json={"url": f"{path}?token=up"})
        if path.startswith("/object/sign/"):
            key = path.removeprefix("/object/sign/avatars/")
            return httpx.Response(200, json={"signedURL": f"{path}?token=down"}) if key in objects else httpx.Response(400, json={"error": "not_found"})
        key = path.removeprefix("/object/avatars/")
        return httpx.Response(200, content=objects[key]) if key in objects else httpx.Response(404)

    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(serve)))
    repo = MemoryProfiles()
    app.dependency_overrides[profiles] = lambda: repo
    app.dependency_overrides[avatars] = AvatarStorage
    return client, repo, objects, headers, owner


def test_profile_reads_empty_and_updates_partially(profile_api):
    client, _, _, headers, _ = profile_api
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers=headers()).json() == {"alias": None, "avatar_url": None, "created_at": None}
    assert client.patch("/v1/me", json={"alias": "  Ana   María "}, headers=headers()).json()["alias"] == "Ana María"
    # An omitted field keeps its value; null clears it.
    assert client.patch("/v1/me", json={}, headers=headers()).json()["alias"] == "Ana María"
    assert client.patch("/v1/me", json={"alias": None}, headers=headers()).json()["alias"] is None


@pytest.mark.parametrize("body", [{"alias": "   "}, {"alias": "x" * 41}, {"alias": 7}, {"nickname": "Ana"}], ids=["blank", "long", "number", "extra"])
def test_profile_rejects_invalid_alias(profile_api, body):
    client, repo, _, headers, _ = profile_api
    assert client.patch("/v1/me", json=body, headers=headers()).status_code == 422
    assert repo.rows == {}


def test_profile_body_is_bounded(profile_api):
    client, _, _, headers, _ = profile_api
    assert client.patch("/v1/me", content=b" " * (128 * 1024 + 1), headers=headers()).status_code == 413


@pytest.mark.parametrize("body", [{"content_type": "image/png", "size_bytes": 1000}, {"content_type": "image/webp", "size_bytes": 0},
                                  {"content_type": "image/webp", "size_bytes": 200_001}, {"content_type": "image/webp", "size_bytes": "1000"}],
                         ids=["type", "empty", "oversize", "string-size"])
def test_avatar_upload_declaration_is_validated(profile_api, body):
    client, _, _, headers, _ = profile_api
    assert client.post("/v1/me/avatar-url", json=body, headers=headers()).status_code == 422


def test_avatar_links_only_own_verified_webp(profile_api):
    client, repo, objects, headers, owner = profile_api
    signed = client.post("/v1/me/avatar-url", json={"content_type": "image/webp", "size_bytes": len(WEBP)}, headers=headers()).json()
    path = f"{owner}/avatar.webp"
    assert signed == {"upload_url": f"https://project.supabase.co/storage/v1/object/upload/sign/avatars/{path}?token=up", "avatar_path": path}
    # Not uploaded yet, then not a WebP image, then someone else's folder.
    assert client.patch("/v1/me", json={"avatar_path": path}, headers=headers()).status_code == 422
    objects[path] = b"<svg/>"
    assert client.patch("/v1/me", json={"avatar_path": path}, headers=headers()).status_code == 422
    assert client.patch("/v1/me", json={"avatar_path": f"{uuid4()}/avatar.webp"}, headers=headers()).status_code == 422
    assert repo.rows == {}
    objects[path] = WEBP
    linked = client.patch("/v1/me", json={"avatar_path": path}, headers=headers())
    assert linked.status_code == 200
    assert linked.json()["avatar_url"] == f"https://project.supabase.co/storage/v1/object/sign/avatars/{path}?token=down"
    # A photo that disappeared from Storage leaves the rest of the profile readable.
    del objects[path]
    assert client.get("/v1/me", headers=headers()).json()["avatar_url"] is None


def test_avatar_requires_storage(profile_api, monkeypatch):
    client, repo, _, headers, owner = profile_api
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY")
    get_settings.cache_clear()
    assert client.post("/v1/me/avatar-url", json={"content_type": "image/webp", "size_bytes": 1000}, headers=headers()).status_code == 503
    repo.rows[owner] = StoredProfile(alias="Ana", avatar_path=f"{owner}/avatar.webp", created_at=None)
    assert client.get("/v1/me", headers=headers()).json() == {"alias": "Ana", "avatar_url": None, "created_at": None}
