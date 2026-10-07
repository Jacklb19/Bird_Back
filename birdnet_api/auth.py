"""Accept only Supabase access tokens with verified signatures and required claims."""
from functools import lru_cache
from typing import Final
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import ErrorCode, raise_error
from .settings import get_settings

# Supabase signs sessions with asymmetric keys published as JWKS; HS256 with the shared secret is only for
# projects that still use legacy keys.
ASYMMETRIC_JWT_ALGORITHMS: Final = frozenset({"RS256", "ES256"})
LEGACY_JWT_ALGORITHM: Final = "HS256"
JWKS_PATH: Final = "/.well-known/jwks.json"
# Postgres role Supabase gives signed-in users. Their access tokens carry it as both audience and role claim,
# and the repository assumes it so that RLS evaluates every query as that user.
AUTHENTICATED_ROLE: Final = "authenticated"
SUBJECT_CLAIM: Final = "sub"
ROLE_CLAIM: Final = "role"
REQUIRED_JWT_CLAIMS: Final = (SUBJECT_CLAIM, "exp", "iat", "iss", "aud")

bearer = HTTPBearer(auto_error=False)


@lru_cache
def signing_keys(issuer: str, timeout_seconds: float) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{issuer}{JWKS_PATH}", timeout=timeout_seconds)


def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> UUID:
    if credentials is None:
        raise_error(ErrorCode.AUTH_REQUIRED)
    settings = get_settings()
    issuer = settings.supabase_auth_issuer
    if not issuer:
        raise_error(ErrorCode.AUTH_NOT_CONFIGURED)
    try:
        token = credentials.credentials
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm in ASYMMETRIC_JWT_ALGORITHMS:
            key = signing_keys(issuer, settings.jwks_timeout_seconds).get_signing_key_from_jwt(token).key
        elif algorithm == LEGACY_JWT_ALGORITHM and settings.supabase_jwt_secret:
            key = settings.supabase_jwt_secret
        else:
            raise jwt.InvalidAlgorithmError()
        claims = jwt.decode(token, key, algorithms=[algorithm], issuer=issuer, audience=AUTHENTICATED_ROLE, options={"require": list(REQUIRED_JWT_CLAIMS)})
        if claims.get(ROLE_CLAIM) != AUTHENTICATED_ROLE:
            raise jwt.InvalidTokenError()
        return UUID(claims[SUBJECT_CLAIM])
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        raise_error(ErrorCode.SESSION_INVALID)
