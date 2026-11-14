"""Error reports retain code locations without carrying runtime values to a third party."""
import json
from typing import cast

from sentry_sdk.types import Event

from birdnet_api.monitoring import without_personal_data


def test_error_report_drops_messages_extra_context_and_local_variables():
    secret = "private-canary"
    event = cast(Event, {
        "event_id": "event", "level": "error", "message": secret, "logentry": {"params": [secret]},
        "extra": {"note": secret}, "contexts": {"trace": {"data": secret}}, "tags": {"owner": secret},
        "user": {"email": secret}, "breadcrumbs": [{"message": secret}], "server_name": secret,
        "exception": {"values": [{"type": "ValueError", "value": secret, "stacktrace": {
            "frames": [{"filename": "birdnet_api/app.py", "function": "batch", "lineno": 1, "vars": {"token": secret}, "abs_path": secret}]
        }}]},
        "request": {"url": "https://user:password@example.invalid/v1/walks/00000000-0000-4000-8000-000000000001?token=private-canary#private-canary", "method": "GET", "data": secret},
    })
    cleaned = without_personal_data(event, {})
    assert secret not in json.dumps(cleaned)
    assert cleaned["exception"]["values"][0]["stacktrace"]["frames"] == [{"filename": "birdnet_api/app.py", "function": "batch", "lineno": 1}]
    assert cleaned["request"] == {"url": "https://example.invalid/v1/walks/{id}", "method": "GET"}
    assert cleaned["event_id"] == "event"
