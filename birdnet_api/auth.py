"""Accept only Supabase access tokens with verified signatures and required claims, and the second step when it is on."""
from functools import lru_cache
from typing import Final, Protocol
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import ErrorCode, raise_error
from .repository import DetectionRepository
from .session import ASSURANCE_CLAIM, AUTHENTICATED_ROLE, FIRST_FACTOR, ROLE_CLAIM, SECOND_FACTOR, SUBJECT_CLAIM, session_assurance
from .settings import get_settings

# Supabase signs sessions with asymmetric keys published as JWKS; HS256 with the shared secret is only for
# projects that still use legacy keys.
ASYMMETRIC_JWT_ALGORITHMS: Final = frozenset({"RS256", "ES256"})
LEGACY_JWT_ALGORITHM: Final = "HS256"
JWKS_PATH: Final = "/.well-known/jwks.json"
# The key set is kept this long, and a token naming an unknown key forces a new download at most once per cooldown:
# such tokens need no valid signature, so without the cooldown anyone could make the API call the issuer per request.
# Supabase publishes a new key before signing with it, so the cooldown does not lock out rotated sessions.
JWKS_CACHE_SECONDS: Final = 300.0
JWKS_REFRESH_COOLDOWN_SECONDS: Final = 60.0
REQUIRED_JWT_CLAIMS: Final = (SUBJECT_CLAIM, "exp", "iat", "iss", "aud")
ASSURANCE_LEVELS: Final = frozenset({FIRST_FACTOR, SECOND_FACTOR})

bearer = HTTPBearer(auto_error=False)


class SecondFactors(Protocol):
    def has_second_factor(self, person: UUID) -> bool: ...


def second_factors() -> SecondFactors:
    return DetectionRepository()


@lru_cache
def signing_keys(issuer: str, timeout_seconds: float) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{issuer}{JWKS_PATH}", lifespan=JWKS_CACHE_SECONDS, cooldown_duration=JWKS_REFRESH_COOLDOWN_SECONDS,
                           timeout=timeout_seconds)


async def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), factors: SecondFactors = Depends(second_factors)) -> UUID:
    # Async so that the level it records stays in this request's context for the endpoint; the blocking work
    # (signing keys, database) runs in the thread pool.
    person, level = await run_in_threadpool(verified_session, credentials, factors)
    session_assurance.set(level)
    return person


def verified_session(credentials: HTTPAuthorizationCredentials | None, factors: SecondFactors) -> tuple[UUID, str]:
    person, level = verified_claims(credentials)
    # A session that gave the code needs no lookup; one that did not is refused if the account asks for the code,
    # so a stolen password alone reaches nothing, not even the deletion of the account.
    if level != SECOND_FACTOR and factors.has_second_factor(person):
        raise_error(ErrorCode.SECOND_FACTOR_REQUIRED)
    return person, level


def verified_claims(credentials: HTTPAuthorizationCredentials | None) -> tuple[UUID, str]:
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
        level = claims.get(ASSURANCE_CLAIM, FIRST_FACTOR)
        if level not in ASSURANCE_LEVELS:
            raise jwt.InvalidTokenError()
        return UUID(claims[SUBJECT_CLAIM]), level
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        raise_error(ErrorCode.SESSION_INVALID)
