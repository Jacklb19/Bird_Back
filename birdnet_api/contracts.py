"""Validate every client-controlled value before it reaches PostgreSQL."""
from datetime import datetime
from math import isclose
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import (
    AVATAR_CONTENT_TYPE,
    CONFIDENCE_DISCARD_BELOW,
    DEFAULT_STATS_PERIOD,
    HOURS_PER_DAY,
    LOCATION_GRID_DECIMALS,
    LOCATION_GRID_TOLERANCE,
    MAX_ALIAS_LENGTH,
    MAX_AVATAR_BYTES,
    MAX_CONFIDENCE,
    MAX_LATITUDE,
    MAX_LONGITUDE,
    MAX_MODEL_VERSION_LENGTH,
    MAX_RECORDING_BYTES,
    MAX_RECORDING_PATH_LENGTH,
    MAX_SITE_NAME_LENGTH,
    MAX_SPECIES_LENGTH,
    MAX_SYNC_BATCH_SIZE,
    MAX_TIME_ZONE_LENGTH,
    RECORDING_CONTENT_TYPE,
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


class Location(StrictModel):
    latitude: float = Field(ge=-MAX_LATITUDE, le=MAX_LATITUDE, allow_inf_nan=False)
    longitude: float = Field(ge=-MAX_LONGITUDE, le=MAX_LONGITUDE, allow_inf_nan=False)

    @field_validator("latitude", "longitude")
    @classmethod
    def require_approximation(cls, value: float) -> float:
        if not isclose(value, round(value, LOCATION_GRID_DECIMALS), abs_tol=LOCATION_GRID_TOLERANCE, rel_tol=0):
            raise ValueError("Location must already be approximate")
        return value


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
    site_id: UUID
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
    # No detection carries a fragment any more. The field stays, always false, while versions of the app
    # installed before the fragments were retired are in use: they drop any record that lacks it.
    has_audio: bool = False
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
