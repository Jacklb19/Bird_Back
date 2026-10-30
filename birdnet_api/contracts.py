"""Validate every client-controlled value before it reaches PostgreSQL."""
from datetime import datetime
from math import isclose
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from .domain import (
    AVATAR_CONTENT_TYPE,
    COMMON_NAME_PATTERN,
    CONFIDENCE_DISCARD_BELOW,
    DEFAULT_STATS_PERIOD,
    DEFAULT_WALK_PAGE,
    DEFAULT_OWN_DETECTION_PAGE,
    DEFAULT_WALK_PATH_PAGE,
    HOURS_PER_DAY,
    LOCATION_GRID_DECIMALS,
    LOCATION_GRID_TOLERANCE,
    MAX_ALIAS_LENGTH,
    MAX_AVATAR_BYTES,
    MAX_COMMON_NAME_LENGTH,
    MAX_CONFIDENCE,
    MAX_INDIVIDUALS,
    MAX_OWN_DETECTION_PAGE,
    MAX_LATITUDE,
    MAX_LONGITUDE,
    MAX_MODEL_VERSION_LENGTH,
    MAX_NOTE_LENGTH,
    MAX_RECORDING_BYTES,
    MAX_RECORDING_PATH_LENGTH,
    MAX_REPORT_NAMES,
    MAX_SITE_NAME_LENGTH,
    MAX_SPECIES_LENGTH,
    MAX_SYNC_BATCH_SIZE,
    MAX_TIME_ZONE_LENGTH,
    MAX_WALK_NAME_LENGTH,
    MAX_WALK_PAGE,
    MAX_WALK_PATH_PAGE,
    MAX_WALK_POINTS,
    RECORDING_CONTENT_TYPE,
    WALK_TIME_TOLERANCE_SECONDS,
    CardLanguage,
    DetectionStatus,
    MapDetectionStatus,
    ReviewAction,
    Reviewer,
    StatsPeriod,
    SubmittedDetectionStatus,
    is_time_zone,
)
from .settings import get_settings


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamp must contain a timezone")
    return value


# Naive timestamps are ambiguous across devices and time zones, so every instant must carry its offset.
AwareTimestamp = Annotated[datetime, AfterValidator(_require_timezone)]


def _require_known_zone(value: str) -> str:
    if not is_time_zone(value):
        raise ValueError("Unknown time zone")
    return value


def _default_time_zone() -> str:
    # Read per request so the configured default applies without re-importing the contracts.
    return get_settings().default_time_zone


TimeZoneName = Annotated[str, Field(max_length=MAX_TIME_ZONE_LENGTH), AfterValidator(_require_known_zone)]


def _collapse_spaces(value: object) -> object:
    return " ".join(value.split()) if isinstance(value, str) else value


def _require_grid(value: float) -> float:
    if not isclose(value, round(value, LOCATION_GRID_DECIMALS), abs_tol=LOCATION_GRID_TOLERANCE, rel_tol=0):
        raise ValueError("Location must already be approximate")
    return value


class Location(StrictModel):
    latitude: float = Field(ge=-MAX_LATITUDE, le=MAX_LATITUDE, allow_inf_nan=False)
    longitude: float = Field(ge=-MAX_LONGITUDE, le=MAX_LONGITUDE, allow_inf_nan=False)

    @field_validator("latitude", "longitude")
    @classmethod
    def require_approximation(cls, value: float) -> float:
        return _require_grid(value)


class DetectionInput(StrictModel):
    id: UUID
    species: str = Field(min_length=1, max_length=MAX_SPECIES_LENGTH)
    confidence: float = Field(ge=CONFIDENCE_DISCARD_BELOW, le=MAX_CONFIDENCE, allow_inf_nan=False)
    status: SubmittedDetectionStatus
    recorded_at: AwareTimestamp
    location: Location
    model_version: str = Field(min_length=1, max_length=MAX_MODEL_VERSION_LENGTH)
    site_id: UUID | None = None
    # Whether other people see it on the collective map. Clients that predate the choice shared everything.
    shared: bool = Field(default=True, strict=True)


class BatchInput(StrictModel):
    detections: list[DetectionInput] = Field(min_length=1, max_length=MAX_SYNC_BATCH_SIZE)


class BatchResponse(StrictModel):
    accepted_ids: list[UUID]
    existing_ids: list[UUID]


class MapQuery(StrictModel):
    """Visible map area and optional filters; antimeridian-crossing boxes are not supported."""
    west: float = Field(ge=-MAX_LONGITUDE, le=MAX_LONGITUDE, allow_inf_nan=False)
    south: float = Field(ge=-MAX_LATITUDE, le=MAX_LATITUDE, allow_inf_nan=False)
    east: float = Field(ge=-MAX_LONGITUDE, le=MAX_LONGITUDE, allow_inf_nan=False)
    north: float = Field(ge=-MAX_LATITUDE, le=MAX_LATITUDE, allow_inf_nan=False)
    species: str | None = Field(default=None, min_length=1, max_length=MAX_SPECIES_LENGTH)
    since: AwareTimestamp | None = None
    until: AwareTimestamp | None = None


class MapDetection(StrictModel):
    """Collective map row: never exposes the author or the path of a recording.

    Other people's rows appear only while their author shares them. `own` tells viewers which rows are theirs;
    `site_name` is filled only for those, since sites are private.
    """
    id: UUID
    species: str
    confidence: float
    status: MapDetectionStatus
    recorded_at: datetime
    latitude: float
    longitude: float
    own: bool
    site_name: str | None
    # Set on someone else's detection of a threatened species: the point is the centre of a cell this many metres
    # wide, not where it was heard (ADR-41).
    generalized_m: int | None = None


class MapResponse(StrictModel):
    detections: list[MapDetection]
    truncated: bool


class SharingInput(StrictModel):
    """The caller's choice, applied to every detection they have already uploaded."""
    shared: bool = Field(strict=True)


class SharingUpdate(StrictModel):
    """Number of detections whose sharing changed; repeating the same choice changes none."""
    updated: int


class SiteInput(StrictModel):
    name: str = Field(min_length=1, max_length=MAX_SITE_NAME_LENGTH)
    location: Location

    @field_validator("name")
    @classmethod
    def require_visible_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Site name is required")
        return value


class Site(StrictModel):
    id: UUID
    name: str
    latitude: float
    longitude: float
    created_at: datetime


class SiteList(StrictModel):
    sites: list[Site]


class StatsQuery(StrictModel):
    period: StatsPeriod = DEFAULT_STATS_PERIOD
    tz: TimeZoneName = Field(default_factory=_default_time_zone)


class SpeciesStat(StrictModel):
    species: str
    detections: int
    days: int
    first_seen: datetime
    is_new: bool


class SiteStats(StrictModel):
    """Deterministic figures; any text built from them must not add claims they do not support."""
    period: StatsPeriod
    since: datetime | None
    until: datetime
    species_count: int
    previous_species_count: int | None
    detections: int
    active_days: int
    hourly: list[int] = Field(min_length=HOURS_PER_DAY, max_length=HOURS_PER_DAY)
    species: list[SpeciesStat]
    missing: list[str]


class ExportQuery(StrictModel):
    """What to export: one site, one walk, or (without either) all the caller's records."""
    site_id: UUID | None = None
    walk_id: UUID | None = None
    since: AwareTimestamp | None = None
    until: AwareTimestamp | None = None


class Profile(StrictModel):
    """The caller's profile; every field is null until set (or while the photo cannot be signed)."""
    alias: str | None
    avatar_url: str | None
    created_at: datetime | None


class ProfileInput(StrictModel):
    """Partial update: omitted fields keep their value and null clears them."""
    alias: Annotated[str | None, Field(min_length=1, max_length=MAX_ALIAS_LENGTH)] = None
    # Checked against the caller's own photo path (and the uploaded bytes) by the route.
    avatar_path: str | None = None

    @field_validator("alias", mode="before")
    @classmethod
    def normalize_alias(cls, value: object) -> object:
        return _collapse_spaces(value)


class AvatarInput(StrictModel):
    """Declared photo upload: WebP only, within the bucket's size limit."""
    content_type: str = Field(strict=True, json_schema_extra={"const": AVATAR_CONTENT_TYPE})
    size_bytes: int = Field(strict=True, ge=1, le=MAX_AVATAR_BYTES)

    @field_validator("content_type")
    @classmethod
    def require_webp(cls, value: str) -> str:
        if value != AVATAR_CONTENT_TYPE:
            raise ValueError(f"Only {AVATAR_CONTENT_TYPE} is accepted")
        return value


class AvatarUpload(StrictModel):
    """Signed Storage upload target and the object path the client then saves with PATCH /me."""
    upload_url: str
    avatar_path: str


class RecordingInput(StrictModel):
    """Declared recording upload: one compressed clip, within the bucket's size limit."""
    content_type: str = Field(strict=True, json_schema_extra={"const": RECORDING_CONTENT_TYPE})
    size_bytes: int = Field(strict=True, ge=1, le=MAX_RECORDING_BYTES)

    @field_validator("content_type")
    @classmethod
    def require_webm(cls, value: str) -> str:
        if value != RECORDING_CONTENT_TYPE:
            raise ValueError(f"Only {RECORDING_CONTENT_TYPE} is accepted")
        return value


class RecordingUpload(StrictModel):
    """Signed Storage upload target and the object path the client then links with PUT /detections/{id}/recording."""
    upload_url: str
    recording_path: str


class RecordingLink(StrictModel):
    recording_path: str = Field(min_length=1, max_length=MAX_RECORDING_PATH_LENGTH)


class RecordingUrl(StrictModel):
    """Short-lived link the owner plays the recording from."""
    url: str
    expires_in: int


class RecordingUsage(StrictModel):
    """What the caller's recordings take of the account's quota."""
    recordings: int
    bytes: int
    limit_bytes: int


class RecordingsDeleted(StrictModel):
    deleted: int


class ReviewInput(StrictModel):
    """A person's verdict on one of their detections; a correction names the species actually heard."""
    action: ReviewAction
    species: str | None = Field(default=None, min_length=1, max_length=MAX_SPECIES_LENGTH)

    @field_validator("species")
    @classmethod
    def trim_species(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("Species must not have surrounding spaces")
        return value

    @model_validator(mode="after")
    def species_only_for_correction(self) -> "ReviewInput":
        if (self.action == ReviewAction.CORRECT) != (self.species is not None):
            raise ValueError("A correction, and only a correction, names a species")
        return self


class ReviewedDetection(StrictModel):
    """A detection after review: `model_species` is what the model said when a person corrected it."""
    id: UUID
    species: str
    model_species: str | None
    status: DetectionStatus
    reviewed_by: Reviewer | None


def _note_text(value: object) -> object:
    # Surrounding blank lines are noise; an all-blank note is no note.
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class NoteInput(StrictModel):
    """Everything the person wrote about one of their detections; both null deletes the note (ADR-40).

    The request carries the whole note, so repeating it after a lost answer changes nothing.
    """
    note: Annotated[Annotated[str, Field(max_length=MAX_NOTE_LENGTH)] | None, BeforeValidator(_note_text)] = None
    individuals: int | None = Field(default=None, strict=True, ge=1, le=MAX_INDIVIDUALS)


class DetectionNote(StrictModel):
    id: UUID
    note: str | None
    individuals: int | None


class TimeZoneQuery(StrictModel):
    """Same `tz` parameter and validation as the site statistics."""
    tz: TimeZoneName = Field(default_factory=_default_time_zone)


class RecordSummary(StrictModel):
    """The caller's own record without discarded detections."""
    detections: int
    species: int
    sites: int
    first_recorded_at: datetime | None
    last_recorded_at: datetime | None
    active_days: int


class OwnSpecies(StrictModel):
    species: str
    detections: int
    best_confidence: float
    first_recorded_at: datetime
    last_recorded_at: datetime
    sites: int


class OwnSpeciesList(StrictModel):
    species: list[OwnSpecies]


class OwnDetectionQuery(StrictModel):
    """Newest first; `before` is the time of the last song already received."""
    before: AwareTimestamp | None = None
    limit: int = Field(default=DEFAULT_OWN_DETECTION_PAGE, ge=1, le=MAX_OWN_DETECTION_PAGE)


class OwnDetection(StrictModel):
    """One of the caller's located songs, with the species a person's review gave it."""
    id: UUID
    species: str
    confidence: float
    status: MapDetectionStatus
    recorded_at: datetime
    latitude: float
    longitude: float


class OwnDetectionPage(StrictModel):
    detections: list[OwnDetection]
    has_more: bool


class SpeciesSite(StrictModel):
    id: UUID
    name: str
    detections: int


class SpeciesCell(StrictModel):
    """One cell of the location grid, the precision locations are stored with."""
    latitude: float
    longitude: float
    detections: int


class SpeciesDetection(StrictModel):
    id: UUID
    recorded_at: datetime
    confidence: float
    status: MapDetectionStatus
    site_id: UUID | None
    has_recording: bool
    reviewed_by: Reviewer | None


class SpeciesRecord(StrictModel):
    """One species in the caller's record; a species never recorded has zero detections and empty lists."""
    species: str
    detections: int
    best_confidence: float | None
    first_recorded_at: datetime | None
    last_recorded_at: datetime | None
    hours: list[int] = Field(min_length=HOURS_PER_DAY, max_length=HOURS_PER_DAY)
    sites: list[SpeciesSite]
    cells: list[SpeciesCell]
    recent: list[SpeciesDetection]


GridLatitude = Annotated[float, Field(ge=-MAX_LATITUDE, le=MAX_LATITUDE, allow_inf_nan=False), AfterValidator(_require_grid)]
GridLongitude = Annotated[float, Field(ge=-MAX_LONGITUDE, le=MAX_LONGITUDE, allow_inf_nan=False), AfterValidator(_require_grid)]
# A cell of a walk's path: [latitude, longitude], plus the seconds since the walk started on walks recorded since
# they carry it. Only cells of the location grid are accepted, never a raw reading.
TrackPoint = tuple[GridLatitude, GridLongitude] | tuple[GridLatitude, GridLongitude, Annotated[int, Field(ge=0)]]
WalkName = Annotated[str, BeforeValidator(_collapse_spaces), Field(min_length=1, max_length=MAX_WALK_NAME_LENGTH)]


class WalkInput(StrictModel):
    """A finished walk as the phone uploads it; repeating the same upload stores nothing new.

    Without a name the app shows one built from the walk's date, in the language of whoever reads it.
    """
    name: WalkName | None = None
    started_at: AwareTimestamp
    ended_at: AwareTimestamp
    track: list[TrackPoint] = Field(max_length=MAX_WALK_POINTS)

    @model_validator(mode="after")
    def require_consistent_period(self) -> "WalkInput":
        if self.ended_at < self.started_at:
            raise ValueError("A walk cannot end before it starts")
        timed = [len(point) == 3 for point in self.track]
        if any(timed) and not all(timed):
            raise ValueError("Either every point of a path has its time or none has")
        times = [point[2] for point in self.track if len(point) == 3]
        if any(later < earlier for earlier, later in zip(times, times[1:], strict=False)):
            raise ValueError("Point times must not go back")
        if times and times[-1] > (self.ended_at - self.started_at).total_seconds() + WALK_TIME_TOLERANCE_SECONDS:
            raise ValueError("A point cannot come after the end of its walk")
        return self


class WalkRename(StrictModel):
    """A new name; null goes back to the one built from the date."""
    name: WalkName | None


class WalkPageQuery(StrictModel):
    """Newest first; `before` is the start of the last walk already received."""
    before: AwareTimestamp | None = None
    limit: int = Field(default=DEFAULT_WALK_PAGE, ge=1, le=MAX_WALK_PAGE)


class WalkSpecies(StrictModel):
    species: str
    detections: int
    best_confidence: float


class Walk(StrictModel):
    """A saved walk with figures computed from its path and from the caller's detections within its period.

    Discarded detections never count, and a corrected one counts as the species the person heard.
    """
    id: UUID
    name: str | None
    started_at: datetime
    ended_at: datetime
    distance_m: float
    points: int
    detections: int
    species: list[WalkSpecies]


class WalkPage(StrictModel):
    walks: list[Walk]
    has_more: bool


class WalkPlace(StrictModel):
    """Where one species was heard during a walk: one cell of the location grid."""
    species: str
    latitude: float
    longitude: float
    detections: int
    best_confidence: float
    first_recorded_at: datetime


class WalkDetail(Walk):
    track: list[TrackPoint]
    places: list[WalkPlace]


class WalkPathQuery(StrictModel):
    """Newest first, in smaller pages than the list: each walk carries its whole path."""
    before: AwareTimestamp | None = None
    limit: int = Field(default=DEFAULT_WALK_PATH_PAGE, ge=1, le=MAX_WALK_PATH_PAGE)


class WalkPath(StrictModel):
    """What a map draws of a walk: its path and where its species were heard."""
    id: UUID
    started_at: datetime
    ended_at: datetime
    track: list[TrackPoint]
    places: list[WalkPlace]


class WalkPathPage(StrictModel):
    walks: list[WalkPath]
    has_more: bool


class WalkUsage(StrictModel):
    walks: int
    limit: int


class WalksDeleted(StrictModel):
    deleted: int


class TextQuery(StrictModel):
    locale: CardLanguage = CardLanguage.ES


class CardSource(StrictModel):
    name: str
    url: str
    license: str


class SpeciesCard(StrictModel):
    """A plain-language card written by the language model from public sources only (RF-14).

    It is never the person's data and never says whether the bird lives anywhere in particular. `stale` asks for a
    new one: its sources may have changed, or the model or its instructions did.
    """
    species: str
    locale: CardLanguage
    paragraphs: list[str]
    sources: list[CardSource]
    model: str
    generated_at: datetime
    stale: bool


class SoundLicense(StrictModel):
    code: str
    url: str


class ReferenceSound(StrictModel):
    """One recording of xeno-canto, with what its conditions ask to show: recordist, licence and catalogue number."""
    id: str
    type: str
    length_seconds: int
    country: str | None
    recordist: str
    license: SoundLicense
    quality: str
    page_url: str
    audio_url: str


class ReferenceSounds(StrictModel):
    """Reference recordings of a species, made by other people; never the caller's records (ADR-42).

    `restricted` says xeno-canto withholds this species' recordings because it is vulnerable to capture.
    """
    species: str
    source: str
    restricted: bool
    sounds: list[ReferenceSound]


CommonName = Annotated[str, BeforeValidator(_collapse_spaces), Field(min_length=1, max_length=MAX_COMMON_NAME_LENGTH, pattern=f"^{COMMON_NAME_PATTERN}$")]


class ReportQuery(StrictModel):
    period: StatsPeriod = DEFAULT_STATS_PERIOD
    tz: TimeZoneName = Field(default_factory=_default_time_zone)
    locale: CardLanguage = CardLanguage.ES


class ReportInput(ReportQuery):
    """What the person asked for, plus the common names their app shows for the species of the site.

    The API has no names index; the names only replace scientific ones in the text and are checked to be names.
    """
    names: dict[Annotated[str, Field(min_length=1, max_length=MAX_SPECIES_LENGTH)], CommonName] = Field(default_factory=dict, max_length=MAX_REPORT_NAMES)


class SiteReport(StrictModel):
    """Plain-language report of a site, written only when asked, from the deterministic statistics it carries (RF-15).

    Every number in the paragraphs is one of `stats`. `stale` says the figures changed since it was written.
    """
    period: StatsPeriod
    locale: CardLanguage
    paragraphs: list[str]
    stats: SiteStats
    model: str
    generated_at: datetime
    stale: bool


class AccountDeletion(StrictModel):
    """Deleting an account cannot be undone; the fixed word guards against a request sent by mistake."""
    confirmation: Literal["DELETE"]
