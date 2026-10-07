"""Validate every client-controlled value before it reaches PostgreSQL."""
from datetime import datetime
from math import isclose
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from .domain import (
    AUDIO_CONTENT_TYPE,
    CONFIDENCE_DISCARD_BELOW,
    DEFAULT_STATS_PERIOD,
    EXPECTED_WAV_BYTES,
    HOURS_PER_DAY,
    LOCATION_GRID_DECIMALS,
    LOCATION_GRID_TOLERANCE,
    MAX_AUDIO_PATH_LENGTH,
    MAX_CONFIDENCE,
    MAX_LATITUDE,
    MAX_LONGITUDE,
    MAX_MODEL_VERSION_LENGTH,
    MAX_SITE_NAME_LENGTH,
    MAX_SPECIES_LENGTH,
    MAX_SYNC_BATCH_SIZE,
    MAX_TIME_ZONE_LENGTH,
    MapDetectionStatus,
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
    audio_path: str | None = Field(default=None, max_length=MAX_AUDIO_PATH_LENGTH)
    site_id: UUID | None = None


class BatchInput(StrictModel):
    detections: list[DetectionInput] = Field(min_length=1, max_length=MAX_SYNC_BATCH_SIZE)


class BatchResponse(StrictModel):
    accepted_ids: list[UUID]
    existing_ids: list[UUID]


class AudioInput(StrictModel):
    """Declared upload: only one model window as WAV, the format the server verifies after upload."""
    # Validators instead of Literal (which only takes literal expressions); the schema still publishes the single accepted value.
    content_type: str = Field(strict=True, json_schema_extra={"const": AUDIO_CONTENT_TYPE})
    size_bytes: int = Field(strict=True, json_schema_extra={"const": EXPECTED_WAV_BYTES})

    @field_validator("content_type")
    @classmethod
    def require_wav(cls, value: str) -> str:
        if value != AUDIO_CONTENT_TYPE:
            raise ValueError(f"Only {AUDIO_CONTENT_TYPE} is accepted")
        return value

    @field_validator("size_bytes")
    @classmethod
    def require_window_size(cls, value: int) -> int:
        if value != EXPECTED_WAV_BYTES:
            raise ValueError(f"Audio must be exactly {EXPECTED_WAV_BYTES} bytes")
        return value


class AudioUpload(StrictModel):
    """Signed Storage upload target and the object path the client reports back as `audio_path`."""
    upload_url: str
    audio_path: str


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
    """Collective map row: never exposes the author or the audio path."""
    id: UUID
    species: str
    confidence: float
    status: MapDetectionStatus
    recorded_at: datetime
    latitude: float
    longitude: float


class MapResponse(StrictModel):
    detections: list[MapDetection]
    truncated: bool


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
    # Read per request so the configured default applies without re-importing the contracts.
    tz: str = Field(default_factory=lambda: get_settings().default_time_zone, max_length=MAX_TIME_ZONE_LENGTH)

    @field_validator("tz")
    @classmethod
    def require_known_zone(cls, value: str) -> str:
        if not is_time_zone(value):
            raise ValueError("Unknown time zone")
        return value


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
