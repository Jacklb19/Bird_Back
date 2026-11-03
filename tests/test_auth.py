"""Signing keys of the issuer: tokens naming unknown keys must not make the API download the key set each time."""
import io
import json
import time
from contextlib import contextmanager

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jwt import jwks_client

from birdnet_api.auth import JWKS_REFRESH_COOLDOWN_SECONDS, signing_keys, verified_claims
from birdnet_api.settings import get_settings

ISSUER = "https://auth.invalid/auth/v1"


@pytest.fixture
def issuer(monkeypatch):
    monkeypatch.setenv("SUPABASE_AUTH_ISSUER", ISSUER)
    get_settings.cache_clear()
    signing_keys.cache_clear()
    key = ec.generate_private_key(ec.SECP256R1())
    published = {"keys": [{**json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key())), "kid": "current", "use": "sig", "alg": "ES256"}]}
    downloads = []
    clock = [time.monotonic()]

    class Opener:
        @contextmanager
        def open(self, request, timeout):
            downloads.append(request.full_url)
            yield io.BytesIO(json.dumps(published).encode())

    monkeypatch.setattr(jwks_client.urllib.request, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(jwks_client.time, "monotonic", lambda: clock[0])

    def token(kid):
        now = int(time.time())
        claims = {"sub": "00000000-0000-4000-8000-000000000001", "iss": ISSUER, "aud": "authenticated", "role": "authenticated", "iat": now, "exp": now + 600}
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid}))

    yield token, downloads, clock
    get_settings.cache_clear()
    signing_keys.cache_clear()


def test_unknown_key_ids_refresh_the_key_set_at_most_once_per_cooldown(issuer):
    token, downloads, clock = issuer
    assert verified_claims(token("current"))[1] == "aal1"
    assert downloads == [f"{ISSUER}/.well-known/jwks.json"]
    for attempt in range(20):
        with pytest.raises(HTTPException) as refused:
            verified_claims(token(f"forged-{attempt}"))
        assert refused.value.status_code == 401
    assert len(downloads) == 1
    clock[0] += JWKS_REFRESH_COOLDOWN_SECONDS + 1
    for attempt in range(5):
        with pytest.raises(HTTPException):
            verified_claims(token(f"later-{attempt}"))
    assert len(downloads) == 2
    # The known key keeps working without another download.
    assert verified_claims(token("current"))[1] == "aal1"
    assert len(downloads) == 2
