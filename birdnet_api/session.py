"""Claims of the session that made the current request, shared by authentication and the RLS transactions."""
from contextvars import ContextVar
from typing import Final

# Postgres role Supabase gives signed-in users. Their access tokens carry it as both audience and role claim,
# and the repository assumes it so that RLS evaluates every query as that user.
AUTHENTICATED_ROLE: Final = "authenticated"
SUBJECT_CLAIM: Final = "sub"
ROLE_CLAIM: Final = "role"
# Authenticator assurance level: `aal1` after the password or Google, `aal2` after the code of an authenticator app.
# Supabase treats a token without it as `aal1`.
ASSURANCE_CLAIM: Final = "aal"
FIRST_FACTOR: Final = "aal1"
SECOND_FACTOR: Final = "aal2"
# Authentication methods of the session, each with the Unix time it was used (password, Google, e-mail code,
# authenticator app). Refreshing the token keeps them, so they tell when the person last proved who they are.
METHODS_CLAIM: Final = "amr"

# Level of the verified session of this request. Authentication sets it and the RLS transactions put it in the claims,
# so the database applies the second step to the API's queries exactly as to anyone else's.
session_assurance: ContextVar[str] = ContextVar("session_assurance", default=FIRST_FACTOR)
# Unix time of the latest authentication of that session; None when its token names none.
session_signed_in_at: ContextVar[int | None] = ContextVar("session_signed_in_at", default=None)
