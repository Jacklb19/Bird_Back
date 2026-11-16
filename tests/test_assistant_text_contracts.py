"""New opt-in boundaries and calendar semantics do not depend on a text provider."""
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from birdnet_api.beta.periods import period_bounds
from birdnet_api.beta.text_contracts import (
    CONSENT_VERSION, InterpretDecision, InterpretInput, InterpretPeriod, ReadPlan, TextConsent,
    TutorDecision, TutorQuestionInput,
)
from birdnet_api.beta.text_errors import TextError, TextErrorCode, require_consent

CONSENT = {"version": CONSENT_VERSION, "accepted": True}
CURRENT = datetime(2026, 11, 14, 15, tzinfo=UTC)
PERIOD = TypeAdapter(InterpretPeriod)


@pytest.mark.parametrize("value", [False, 0, 1, "true", None])
def test_consent_is_an_explicit_strict_boolean(value):
    if type(value) is bool:
        with pytest.raises(TextError) as error:
            require_consent(TextConsent(version=CONSENT_VERSION, accepted=value))
        assert error.value.kind is TextErrorCode.CONSENT_REQUIRED
    else:
        with pytest.raises(ValidationError):
            TextConsent.model_validate({"version": CONSENT_VERSION, "accepted": value})
    with pytest.raises(TextError) as error:
        require_consent(TextConsent(version="older-notice", accepted=True))
    assert error.value.kind is TextErrorCode.CONSENT_VERSION_CHANGED


@pytest.mark.parametrize("text", ["", "  \n ", "x" * 901, 1, True, ["text"], "\ud800", "\udfff"])
def test_text_has_a_unicode_code_point_bound_before_serialization(text):
    with pytest.raises(ValidationError):
        InterpretInput.model_validate({"text": text, "consent": CONSENT})
    assert InterpretInput.model_validate({"text": "  " + "🐦" * 900 + "  ", "consent": CONSENT}).text == "🐦" * 900


def test_request_extras_and_private_attachments_are_not_supported():
    for extra in ({"messages": []}, {"audio": "binary"}, {"notes": "private"}, {"location": [4, -74]}, {"limit": True}):
        with pytest.raises(ValidationError):
            InterpretInput.model_validate({"text": "Show yesterday", "consent": CONSENT, **extra})
    with pytest.raises(ValidationError):
        TutorQuestionInput.model_validate({"topic": "confidence", "text": "Explain", "consent": CONSENT, "history": []})


@pytest.mark.parametrize("value,first,after", [
    ("today", "2026-11-14T05:00:00+00:00", "2026-11-15T05:00:00+00:00"),
    ("yesterday", "2026-11-13T05:00:00+00:00", "2026-11-14T05:00:00+00:00"),
    ("this_week", "2026-11-09T05:00:00+00:00", "2026-11-16T05:00:00+00:00"),
    ("last_week", "2026-11-02T05:00:00+00:00", "2026-11-09T05:00:00+00:00"),
    ("this_month", "2026-11-01T05:00:00+00:00", "2026-12-01T05:00:00+00:00"),
    ("last_month", "2026-10-01T05:00:00+00:00", "2026-11-01T05:00:00+00:00"),
    ("this_year", "2026-01-01T05:00:00+00:00", "2027-01-01T05:00:00+00:00"),
    ("last_year", "2025-01-01T05:00:00+00:00", "2026-01-01T05:00:00+00:00"),
])
def test_relative_calendar_periods_have_complete_local_boundaries(value, first, after):
    period = PERIOD.validate_python({"kind": "relative", "value": value})
    assert period_bounds(period, CURRENT, "America/Bogota") == (datetime.fromisoformat(first), datetime.fromisoformat(after))


def test_calendar_midnight_leap_year_and_dst_are_resolved_locally():
    today = PERIOD.validate_python({"kind": "relative", "value": "today"})
    midnight = datetime(2026, 11, 14, 5, tzinfo=UTC)
    start, end = period_bounds(today, midnight, "America/Bogota")
    assert start == midnight and end > midnight
    dates = PERIOD.validate_python({"kind": "dates", "first": "2024-02-29", "last": "2024-02-29"})
    assert period_bounds(dates, CURRENT, "UTC") == (datetime(2024, 2, 29, tzinfo=UTC), datetime(2024, 3, 1, tzinfo=UTC))
    for current, hours in ((datetime(2026, 3, 8, 12, tzinfo=UTC), 23), (datetime(2026, 11, 1, 12, tzinfo=UTC), 25)):
        first, after = period_bounds(today, current, "America/New_York")
        assert after - first == timedelta(hours=hours)
        for value, days in (("last_7_days", 7), ("last_30_days", 30)):
            rolling = PERIOD.validate_python({"kind": "relative", "value": value})
            lower, upper = period_bounds(rolling, current, "America/New_York")
            assert upper == current and upper - lower == timedelta(days=days)


@pytest.mark.parametrize("period", [
    {"kind": "dates", "first": "2026-11-14", "last": "2026-11-13"},
    {"kind": "dates", "first": "2026-02-29", "last": "2026-03-01"},
    {"kind": "dates", "first": 0, "last": "2026-01-01"},
    {"kind": "dates", "first": "20261114", "last": "2026-11-14"},
    {"kind": "all", "value": "yesterday"},
    {"kind": "relative", "value": "arbitrary SQL"},
])
def test_periods_refuse_implicit_coercions_and_unknown_shapes(period):
    with pytest.raises(ValidationError):
        PERIOD.validate_python(period)


@pytest.mark.parametrize("intent,species,period,review", [
    ("outings", "Turdus fuscater", {"kind": "all"}, "all"),
    ("collections", None, {"kind": "relative", "value": "today"}, "all"),
    ("wishlist", None, {"kind": "relative", "value": "today"}, "all"),
    ("species", None, {"kind": "all"}, "pending"),
    ("observations", None, {"kind": "all"}, "pending"),
])
def test_provider_plan_semantics_are_checked_beyond_json_shape(intent, species, period, review):
    with pytest.raises(ValidationError):
        ReadPlan.model_validate({"intent": intent, "species": species, "period": period, "review": review})


def test_decision_unions_require_exact_status_reason_and_body():
    for body in (
        {"status": "ready", "plan": None, "reason_code": None},
        {"status": "clarification", "plan": None, "reason_code": "free private explanation"},
        {"status": "rejected", "plan": None, "reason_code": "outside_scope", "sql": "SELECT 1"},
    ):
        with pytest.raises(ValidationError):
            TypeAdapter(InterpretDecision).validate_python(body)
    for body in (
        {"status": "answered", "paragraphs": [], "reason_code": None},
        {"status": "rejected", "paragraphs": ["Private echo"], "reason_code": "outside_topic"},
        {"status": "answered", "paragraphs": ["x" * 901], "reason_code": None},
    ):
        with pytest.raises(ValidationError):
            TypeAdapter(TutorDecision).validate_python(body)
