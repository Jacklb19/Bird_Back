"""Errors of the deployed API reported to Sentry (ADR-43), without anything that identifies a person.

Off unless SENTRY_DSN is set. Only the error, its trace and the route are sent: no headers, cookies, bodies, query
strings (the map's area is a place), identifiers in paths, user or IP address. Vercel's own logs keep requests for one
hour on the free plan, too little to find an error after the fact.
"""
import logging
import os
import re
from typing import Any, Final

import sentry_sdk
from sentry_sdk.types import Event, Hint

from .settings import Settings, SettingsError

# Detections, walks and sites are named by UUID in paths; a path keeps its shape without them.
IDENTIFIER: Final = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
IDENTIFIER_PLACEHOLDER: Final = "{id}"
# Request fields Sentry's integrations may add; only the method and the cleaned URL stay.
REQUEST_FIELDS_KEPT: Final = frozenset({"method", "url"})

logger = logging.getLogger(__name__)


def start_error_reports() -> None:
    # Read without the process cache: this runs at import, before a server may set its environment (the tests' does).
    try:
        settings = Settings.from_env(os.environ)
    except SettingsError:
        # The application reports malformed configuration itself at startup.
        return
    if not settings.sentry_dsn:
        return
    # Local variables of a trace would carry tokens, notes and identifiers; the trace itself is enough.
    try:
        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.sentry_environment, send_default_pii=False, include_local_variables=False,
                        traces_sample_rate=0, max_breadcrumbs=0, before_send=without_personal_data)
    except Exception as error:  # Reporting errors must never keep the API from starting.
        # A malformed DSN (pasted with quotes, say) took the whole API down; now reports stay off and the log says why.
        logger.error("Error reports are off: SENTRY_DSN was not accepted (%s)", type(error).__name__)


def without_personal_data(event: Event, _: Hint) -> Event | None:
    request: dict[str, Any] = event.get("request") or {}
    if request:
        cleaned = {key: value for key, value in request.items() if key in REQUEST_FIELDS_KEPT}
        if isinstance(cleaned.get("url"), str):
            cleaned["url"] = IDENTIFIER.sub(IDENTIFIER_PLACEHOLDER, cleaned["url"].split("?", 1)[0])
        event["request"] = cleaned
    for field in ("user", "server_name", "breadcrumbs"):
        event.pop(field, None)
    return event
