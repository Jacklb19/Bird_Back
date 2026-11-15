"""Strict opt-in text requests and closed provider decisions, separate from the existing beta API."""
from datetime import date, datetime
from typing import Annotated, Literal, TypedDict

from pydantic import BeforeValidator, Field, StrictBool, StrictStr, field_validator, model_validator

from ..contracts import StrictModel
from ..domain import CardLanguage
from ..language_model import MAX_OUTPUT_TOKENS, MAX_PARAGRAPH_LENGTH, MAX_PARAGRAPHS, MIN_PARAGRAPHS
from .contracts import AssistantBase, AssistantQuestion, SpeciesName, TokenUsage, TutorAnswer, TutorInput, TutorTopic

CONSENT_VERSION = "assistant-text-v1"
TERMS_URL = "https://aistudio.google.com/docs/terms"
ClarificationCode = Literal["ambiguous_request", "ambiguous_species", "ambiguous_period", "private_reference"]
RejectionCode = Literal["outside_scope", "unsupported_filter", "unsupported_operation", "identification_request"]
TutorRejectionCode = Literal["outside_topic", "identification_request", "private_statistics_request", "insufficient_public_facts"]
RelativePeriodName = Literal["today", "yesterday", "this_week", "last_week", "this_month", "last_month", "this_year", "last_year", "last_7_days", "last_30_days"]


def _text(value: object) -> object:
    if isinstance(value, str):
        # JSON permits escaped lone surrogates; reject them before quota or UTF-8 serialization.
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Text must contain valid Unicode") from None
        return value.strip()
    return value


QuestionText = Annotated[StrictStr, BeforeValidator(_text), Field(min_length=1, max_length=MAX_PARAGRAPH_LENGTH)]


class TextConsent(StrictModel):
    version: StrictStr
    accepted: StrictBool


class TextPolicy(StrictModel):
    version: str = CONSENT_VERSION
    provider: str = "Google Gemini"
    terms_url: str = TERMS_URL
    max_text_characters: int = MAX_PARAGRAPH_LENGTH
    max_output_tokens: int = MAX_OUTPUT_TOKENS


class InterpretInput(AssistantBase):
    text: QuestionText
    consent: TextConsent


class TutorQuestionInput(TutorInput):
    text: QuestionText
    consent: TextConsent


class AllPeriod(StrictModel):
    kind: Literal["all"]


class RelativePeriod(StrictModel):
    kind: Literal["relative"]
    value: RelativePeriodName


class DatePeriod(StrictModel):
    kind: Literal["dates"]
    first: date
    last: date

    @field_validator("first", "last", mode="before")
    @classmethod
    def iso_date(cls, value: object) -> date:
        # date.fromisoformat also accepts compact/week forms; this contract accepts YYYY-MM-DD only.
        if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
            raise ValueError("Expected an ISO calendar date")
        return date.fromisoformat(value)

    @model_validator(mode="after")
    def ordered(self) -> "DatePeriod":
        if self.first > self.last:
            raise ValueError("First date must not follow last date")
        return self


InterpretPeriod = Annotated[AllPeriod | RelativePeriod | DatePeriod, Field(discriminator="kind")]


class ReadPlan(StrictModel):
    intent: Literal["detections", "species", "outings", "observations", "collections", "wishlist"]
    species: SpeciesName | None
    period: InterpretPeriod
    review: Literal["all", "pending"]

    @model_validator(mode="after")
    def allowed_filters(self) -> "ReadPlan":
        if self.intent != "detections" and self.review != "all":
            raise ValueError("Review only applies to detections")
        if self.intent in ("outings", "collections") and self.species is not None:
            raise ValueError("Species does not apply to this intent")
        if self.intent in ("collections", "wishlist") and self.period.kind != "all":
            raise ValueError("Period does not apply to this intent")
        return self


class ReadyDecision(StrictModel):
    status: Literal["ready"]
    plan: ReadPlan
    reason_code: None


class ClarificationDecision(StrictModel):
    status: Literal["clarification"]
    plan: None
    reason_code: ClarificationCode


class RejectedDecision(StrictModel):
    status: Literal["rejected"]
    plan: None
    reason_code: RejectionCode


InterpretDecision = Annotated[ReadyDecision | ClarificationDecision | RejectedDecision, Field(discriminator="status")]


class MeasuredResponse(StrictModel):
    model: str
    generated_at: datetime
    usage: TokenUsage


class MeasurementFields(TypedDict):
    model: str
    generated_at: datetime
    usage: TokenUsage


class ReadyInterpretation(MeasuredResponse):
    status: Literal["ready"] = "ready"
    query: AssistantQuestion


class ClarifiedInterpretation(MeasuredResponse):
    status: Literal["clarification"] = "clarification"
    reason_code: ClarificationCode


class RejectedInterpretation(MeasuredResponse):
    status: Literal["rejected"] = "rejected"
    reason_code: RejectionCode


InterpretationResponse = Annotated[ReadyInterpretation | ClarifiedInterpretation | RejectedInterpretation, Field(discriminator="status")]


class AnsweredTutorDecision(StrictModel):
    status: Literal["answered"]
    paragraphs: Annotated[list[QuestionText], Field(min_length=MIN_PARAGRAPHS, max_length=MAX_PARAGRAPHS)]
    reason_code: None


class RejectedTutorDecision(StrictModel):
    status: Literal["rejected"]
    paragraphs: Annotated[list[str], Field(max_length=0)]
    reason_code: TutorRejectionCode


TutorDecision = Annotated[AnsweredTutorDecision | RejectedTutorDecision, Field(discriminator="status")]


class AnsweredTutorResponse(StrictModel):
    status: Literal["answered"] = "answered"
    answer: TutorAnswer


class RejectedTutorResponse(MeasuredResponse):
    status: Literal["rejected"] = "rejected"
    reason_code: TutorRejectionCode
    topic: TutorTopic
    species: str | None
    locale: CardLanguage


TutorQuestionResponse = Annotated[AnsweredTutorResponse | RejectedTutorResponse, Field(discriminator="status")]
