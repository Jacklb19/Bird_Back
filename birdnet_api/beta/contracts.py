"""The frozen beta API contract; visual evidence never masquerades as acoustic inference."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BeforeValidator, Field, model_validator

from ..contracts import AwareTimestamp, Location, StrictModel, TimeZoneName, _default_time_zone
from ..domain import (
    DEFAULT_WALK_PAGE, MAX_INDIVIDUALS, MAX_NOTE_LENGTH, MAX_SPECIES_LENGTH, MAX_WALK_NAME_LENGTH,
    MAX_WALK_PAGE, SCIENTIFIC_NAME_PATTERN, CardLanguage, DetectionStatus,
)
from ..model_species import require_model_species

OutingKind = Literal["stationary", "walk", "import"]
EventKind = Literal["detection", "observation"]
EntityKind = Literal["outing", "observation", "collection"]
TutorTopic = Literal["confidence", "detection_status", "recording_privacy", "approximate_location", "species_facts"]
PositiveVersion = Annotated[int, Field(strict=True, ge=1)]


def _name(value: object) -> object:
    return " ".join(value.split()) if isinstance(value, str) else value


def _note(value: object) -> object:
    return (value.strip() or None) if isinstance(value, str) else value


ShortName = Annotated[str, BeforeValidator(_name), Field(min_length=1, max_length=MAX_WALK_NAME_LENGTH)]
Note = Annotated[Annotated[str, Field(max_length=MAX_NOTE_LENGTH)] | None, BeforeValidator(_note)]
SpeciesName = Annotated[str, Field(min_length=1, max_length=MAX_SPECIES_LENGTH, pattern=f"^{SCIENTIFIC_NAME_PATTERN}$")]


class PageQuery(StrictModel):
    limit: int = Field(default=DEFAULT_WALK_PAGE, ge=1, le=MAX_WALK_PAGE)


class DescendingQuery(PageQuery):
    before: AwareTimestamp | None = None
    after_id: UUID | None = None

    @model_validator(mode="after")
    def paired_cursor(self) -> "DescendingQuery":
        if (self.before is None) != (self.after_id is None):
            raise ValueError("before and after_id must be supplied together")
        return self


class EventQuery(PageQuery):
    after: AwareTimestamp | None = None
    after_id: UUID | None = None
    after_kind: EventKind | None = None

    @model_validator(mode="after")
    def complete_cursor(self) -> "EventQuery":
        present = (self.after is not None, self.after_id is not None, self.after_kind is not None)
        if any(present) and not all(present):
            raise ValueError("after, after_id and after_kind must be supplied together")
        return self


class DeletionQuery(PageQuery):
    after: AwareTimestamp | None = None
    after_id: UUID | None = None
    after_kind: EntityKind | None = None

    @model_validator(mode="after")
    def complete_cursor(self) -> "DeletionQuery":
        present = (self.after is not None, self.after_id is not None, self.after_kind is not None)
        if any(present) and not all(present):
            raise ValueError("after, after_id and after_kind must be supplied together")
        return self


class DeleteQuery(StrictModel):
    expected_version: int = Field(ge=1)


class OutingInput(StrictModel):
    expected_version: PositiveVersion | None = None
    title: ShortName | None
    kind: OutingKind
    started_at: AwareTimestamp
    ended_at: AwareTimestamp | None
    note: Note
    site_id: UUID | None
    walk_id: UUID | None

    @model_validator(mode="after")
    def coherent_context(self) -> "OutingInput":
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("An outing cannot end before it starts")
        if self.walk_id is not None and self.kind != "walk":
            raise ValueError("Only a walking outing can reference a walk")
        return self


class Outing(StrictModel):
    id: UUID
    version: int
    title: str | None
    kind: OutingKind
    started_at: datetime
    ended_at: datetime | None
    note: str | None
    site_id: UUID | None
    walk_id: UUID | None
    created_at: datetime
    updated_at: datetime
    detections: int
    observations: int
    species: int


class OutingPage(StrictModel):
    outings: list[Outing]
    has_more: bool


class ObservationInput(StrictModel):
    expected_version: PositiveVersion | None = None
    species: SpeciesName
    observed_at: AwareTimestamp
    location: Location | None
    individuals: int | None = Field(ge=1, le=MAX_INDIVIDUALS, strict=True)
    note: Note
    outing_id: UUID | None


class Observation(StrictModel):
    id: UUID
    version: int
    source: Literal["manual"] = "manual"
    species: str
    observed_at: datetime
    location: Location | None
    individuals: int | None
    note: str | None
    outing_id: UUID | None
    created_at: datetime
    updated_at: datetime


class ObservationQuery(DescendingQuery):
    outing_id: UUID | None = None
    species: SpeciesName | None = None


class ObservationPage(StrictModel):
    observations: list[Observation]
    has_more: bool


class CollectionInput(StrictModel):
    expected_version: PositiveVersion | None = None
    name: ShortName
    note: Note


class Collection(StrictModel):
    id: UUID
    version: int
    name: str
    note: str | None
    created_at: datetime
    updated_at: datetime
    items: int


class CollectionPage(StrictModel):
    collections: list[Collection]
    has_more: bool


class DetectionEvent(StrictModel):
    kind: Literal["detection"] = "detection"
    id: UUID
    recorded_at: datetime
    species: str
    confidence: float
    status: DetectionStatus
    has_recording: bool


class ObservationEvent(StrictModel):
    kind: Literal["observation"] = "observation"
    id: UUID
    recorded_at: datetime
    species: str
    individuals: int | None


TimelineEvent = Annotated[DetectionEvent | ObservationEvent, Field(discriminator="kind")]


class TimelinePage(StrictModel):
    events: list[TimelineEvent]
    has_more: bool


class CollectionItemPage(StrictModel):
    items: list[TimelineEvent]
    has_more: bool


class Deletion(StrictModel):
    kind: EntityKind
    id: UUID
    version: int
    deleted_at: datetime


class DeletionPage(StrictModel):
    deletions: list[Deletion]
    has_more: bool


class WishlistQuery(PageQuery):
    after_species: SpeciesName | None = None


class WishlistItem(StrictModel):
    species: str
    created_at: datetime


class WishlistPage(StrictModel):
    species: list[WishlistItem]
    has_more: bool


class AssistantBase(PageQuery):
    limit: int = Field(default=DEFAULT_WALK_PAGE, ge=1, le=MAX_WALK_PAGE, strict=True)
    locale: CardLanguage = CardLanguage.ES
    tz: TimeZoneName = Field(default_factory=_default_time_zone)


class PeriodFilters(StrictModel):
    since: AwareTimestamp | None = None
    until: AwareTimestamp | None = None

    @model_validator(mode="after")
    def ordered_period(self) -> "PeriodFilters":
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("since must precede until")
        return self


class DetectionsQuestion(AssistantBase, DescendingQuery, PeriodFilters):
    intent: Literal["detections"]
    species: SpeciesName | None = None
    review: Literal["all", "pending"] = "all"


class SpeciesQuestion(AssistantBase, PeriodFilters):
    intent: Literal["species"]
    species: SpeciesName | None = None
    after_species: SpeciesName | None = None


class OutingsQuestion(AssistantBase, DescendingQuery, PeriodFilters):
    intent: Literal["outings"]


class ObservationsQuestion(AssistantBase, DescendingQuery, PeriodFilters):
    intent: Literal["observations"]
    species: SpeciesName | None = None
    outing_id: UUID | None = None


class CollectionsQuestion(AssistantBase, DescendingQuery):
    intent: Literal["collections"]


class WishlistQuestion(AssistantBase):
    intent: Literal["wishlist"]
    species: SpeciesName | None = None
    after_species: SpeciesName | None = None


class ComparedPeriod(StrictModel):
    """A bounded period of a comparison: both ends are required, since "everything" compares with nothing."""
    since: AwareTimestamp
    until: AwareTimestamp

    @model_validator(mode="after")
    def ordered_period(self) -> "ComparedPeriod":
        if self.since >= self.until:
            raise ValueError("since must precede until")
        return self


class CompareQuestion(AssistantBase):
    """Two periods side by side (ADR-47): counts and the species heard in only one of them, all computed in SQL."""
    intent: Literal["compare"]
    species: SpeciesName | None = None
    first: ComparedPeriod
    second: ComparedPeriod


AssistantQuestion = Annotated[
    DetectionsQuestion | SpeciesQuestion | OutingsQuestion | ObservationsQuestion | CollectionsQuestion | WishlistQuestion | CompareQuestion,
    Field(discriminator="intent"),
]


class QueryDetection(StrictModel):
    id: UUID
    species: str
    confidence: float
    status: DetectionStatus
    recorded_at: datetime
    has_recording: bool


class QuerySpecies(StrictModel):
    species: str
    detections: int
    best_confidence: float
    first_recorded_at: datetime
    last_recorded_at: datetime


class QueryOuting(StrictModel):
    id: UUID
    version: int
    title: str | None
    kind: OutingKind
    started_at: datetime
    ended_at: datetime | None
    detections: int
    observations: int
    species: int


class QueryObservation(StrictModel):
    id: UUID
    version: int
    source: Literal["manual"] = "manual"
    species: str
    observed_at: datetime
    individuals: int | None
    outing_id: UUID | None


class QueryCollection(StrictModel):
    id: UUID
    version: int
    name: str
    created_at: datetime
    updated_at: datetime
    items: int


class SpeciesFilters(PeriodFilters):
    species: str | None


class DetectionFilters(SpeciesFilters):
    review: Literal["all", "pending"]


class ObservationFilters(SpeciesFilters):
    outing_id: UUID | None


class EmptyFilters(StrictModel):
    pass


class WishlistFilters(StrictModel):
    species: str | None


class DetectionTotals(StrictModel):
    detections: int
    species: int


class ObservationTotals(StrictModel):
    observations: int
    species: int


class OutingTotals(StrictModel):
    outings: int


class CollectionTotals(StrictModel):
    collections: int


class WishlistTotals(StrictModel):
    species: int


class QueryAnswerBase(StrictModel):
    has_more: bool
    usage: None = None


class DetectionsAnswer(QueryAnswerBase):
    intent: Literal["detections"] = "detections"
    filters: DetectionFilters
    totals: DetectionTotals
    detections: list[QueryDetection]


class SpeciesAnswer(QueryAnswerBase):
    intent: Literal["species"] = "species"
    filters: SpeciesFilters
    totals: DetectionTotals
    species: list[QuerySpecies]


class OutingsAnswer(QueryAnswerBase):
    intent: Literal["outings"] = "outings"
    filters: PeriodFilters
    totals: OutingTotals
    outings: list[QueryOuting]


class ObservationsAnswer(QueryAnswerBase):
    intent: Literal["observations"] = "observations"
    filters: ObservationFilters
    totals: ObservationTotals
    observations: list[QueryObservation]


class CollectionsAnswer(QueryAnswerBase):
    intent: Literal["collections"] = "collections"
    filters: EmptyFilters
    totals: CollectionTotals
    collections: list[QueryCollection]


class WishlistAnswer(QueryAnswerBase):
    intent: Literal["wishlist"] = "wishlist"
    filters: WishlistFilters
    totals: WishlistTotals
    wishlist: list[WishlistItem]


class PeriodSummary(StrictModel):
    """One period of a comparison. Songs leave out discarded ones; visual observations are counted apart."""
    since: datetime
    until: datetime
    detections: int
    species: int
    observations: int
    observed_species: int


class CompareFilters(StrictModel):
    species: str | None


class CompareAnswer(QueryAnswerBase):
    intent: Literal["compare"] = "compare"
    filters: CompareFilters
    first: PeriodSummary
    second: PeriodSummary
    # Species heard in only one of the two periods, alphabetically; at most `limit` of each.
    only_first: list[str]
    only_second: list[str]


AssistantAnswer = Annotated[
    DetectionsAnswer | SpeciesAnswer | OutingsAnswer | ObservationsAnswer | CollectionsAnswer | WishlistAnswer | CompareAnswer,
    Field(discriminator="intent"),
]


class TutorInput(StrictModel):
    topic: TutorTopic
    species: SpeciesName | None = None
    locale: CardLanguage = CardLanguage.ES

    @model_validator(mode="after")
    def species_context(self) -> "TutorInput":
        if (self.topic == "species_facts") != (self.species is not None):
            raise ValueError("Only species_facts requires a species")
        if self.species is not None:
            require_model_species(self.species)
        return self


class TutorSource(StrictModel):
    name: str
    url: str | None
    license: str | None


class TokenUsage(StrictModel):
    input_tokens: int | None = Field(ge=0, strict=True)
    output_tokens: int | None = Field(ge=0, strict=True)
    total_tokens: int | None = Field(ge=0, strict=True)


class TutorAnswer(StrictModel):
    topic: TutorTopic
    species: str | None
    locale: CardLanguage
    paragraphs: list[str]
    sources: list[TutorSource]
    model: str
    generated_at: datetime
    usage: TokenUsage
