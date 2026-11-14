"""Errors of the deployed API reported to Sentry (ADR-43), without anything that identifies a person.

Off unless SENTRY_DSN is set. Only the error, its trace and the route are sent: no headers, cookies, bodies, query
strings (the map's area is a place), identifiers in paths, user or IP address. Vercel's own logs keep requests for one
hour on the free plan, too little to find an error after the fact.
"""
import logging
import os
import re
from typing import Final, cast
from urllib.parse import urlsplit, urlunsplit

import sentry_sdk
from sentry_sdk.types import Event, Hint

from .settings import Settings, SettingsError

# Detections, walks and sites are named by UUID in paths; a path keeps its shape without them.
IDENTIFIER: Final = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
IDENTIFIER_PLACEHOLDER: Final = "{id}"
EVENT_FIELDS_KEPT: Final = frozenset({"event_id", "timestamp", "platform", "level", "release", "environment", "sdk"})
FRAME_FIELDS_KEPT: Final = frozenset({"filename", "module", "function", "lineno", "colno", "in_app"})

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
    # Unknown metadata, log parameters and exception messages can contain notes, tokens or upstream response bodies.
    # Keep the exception's type and code location, never its runtime values or arbitrary context fields.
    cleaned: dict[str, object] = {key: value for key, value in event.items() if key in EVENT_FIELDS_KEPT}
    request = event.get("request") or {}
    if request:
        safe_request: dict[str, object] = {}
        if request.get("method"):
            safe_request["method"] = request["method"]
        url = request.get("url")
        if isinstance(url, str):
            try:
                parsed = urlsplit(url)
                safe_request["url"] = urlunsplit((parsed.scheme, parsed.hostname or "", IDENTIFIER.sub(IDENTIFIER_PLACEHOLDER, parsed.path), "", ""))
            except ValueError:
                pass
        cleaned["request"] = safe_request
    exceptions = []
    for error in (event.get("exception") or {}).get("values", []):
        frames = [{key: value for key, value in frame.items() if key in FRAME_FIELDS_KEPT}
                  for frame in (error.get("stacktrace") or {}).get("frames", [])]
        exceptions.append({"type": error.get("type", "Error"), "value": "[redacted]", "stacktrace": {"frames": frames}})
    if exceptions or "exception" in event:
        cleaned["exception"] = {"values": exceptions}
    return cast(Event, cleaned)
