"""Accept only Supabase access tokens with verified signatures and required claims."""
import os
from functools import lru_cache
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)


@lru_cache
def signing_keys(issuer: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json", timeout=10)


def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> UUID:
    if credentials is None:
        raise HTTPException(401, "Authentication required")
    issuer = os.environ.get("SUPABASE_AUTH_ISSUER")
    if not issuer:
        raise HTTPException(503, "Authentication is not configured")
    try:
        token = credentials.credentials
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm in ("RS256", "ES256"):
            key = signing_keys(issuer).get_signing_key_from_jwt(token).key
        elif algorithm == "HS256" and os.environ.get("SUPABASE_JWT_SECRET"):
            key = os.environ["SUPABASE_JWT_SECRET"]
        else:
            raise jwt.InvalidAlgorithmError()
        claims = jwt.decode(token, key, algorithms=[algorithm], issuer=issuer, audience="authenticated", options={"require": ["sub", "exp", "iat", "iss", "aud"]})
        if claims.get("role") != "authenticated":
            raise jwt.InvalidTokenError()
        return UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        raise HTTPException(401, "Invalid or expired session") from None
