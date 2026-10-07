"""Validate every client-controlled value before it reaches PostgreSQL."""
from datetime import datetime
from math import isclose
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Location(StrictModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)

    @field_validator("latitude", "longitude")
    @classmethod
    def require_approximation(cls, value: float) -> float:
        if not isclose(value, round(value, 3), abs_tol=1e-9, rel_tol=0):
            raise ValueError("Location must already be approximate")
        return value


class DetectionInput(StrictModel):
    id: UUID
    species: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0.45, le=1, allow_inf_nan=False)
    status: Literal["confirmed", "provisional"]
    recorded_at: datetime
    location: Location
    model_version: str = Field(min_length=1, max_length=200)
    audio_path: str | None = Field(default=None, max_length=300)
    site_id: UUID | None = None

    @field_validator("recorded_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Timestamp must contain a timezone")
        return value


class BatchInput(StrictModel):
    detections: list[DetectionInput] = Field(min_length=1, max_length=50)


class BatchResponse(StrictModel):
    accepted_ids: list[UUID]
    existing_ids: list[UUID]


class AudioInput(StrictModel):
    content_type: Literal["audio/wav"]
    size_bytes: Literal[288044]


class MapQuery(StrictModel):
    """Visible map area and optional filters; antimeridian-crossing boxes are not supported."""
    west: float = Field(ge=-180, le=180, allow_inf_nan=False)
    south: float = Field(ge=-90, le=90, allow_inf_nan=False)
    east: float = Field(ge=-180, le=180, allow_inf_nan=False)
    north: float = Field(ge=-90, le=90, allow_inf_nan=False)
    species: str | None = Field(default=None, min_length=1, max_length=200)
    since: datetime | None = None
    until: datetime | None = None

    @field_validator("since", "until")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Timestamp must contain a timezone")
        return value


class MapDetection(StrictModel):
    """Collective map row: never exposes the author or the audio path."""
    id: UUID
    species: str
    confidence: float
    status: Literal["confirmed", "provisional", "verified", "corrected"]
    recorded_at: datetime
    latitude: float
    longitude: float


class MapResponse(StrictModel):
    detections: list[MapDetection]
    truncated: bool


class SiteInput(StrictModel):
    name: str = Field(min_length=1, max_length=80)
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


Period = Literal["week", "month", "year", "all"]
PERIOD_DAYS = {"week": 7, "month": 30, "year": 365, "all": None}


class StatsQuery(StrictModel):
    period: Period = "month"
    tz: str = Field(default="America/Bogota", max_length=64)

    @field_validator("tz")
    @classmethod
    def require_known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown time zone") from None
        return value


class SpeciesStat(StrictModel):
    species: str
    detections: int
    days: int
    first_seen: datetime
    is_new: bool


class SiteStats(StrictModel):
    """Deterministic figures; any text built from them must not add claims they do not support."""
    period: Period
    since: datetime | None
    until: datetime
    species_count: int
    previous_species_count: int | None
    detections: int
    active_days: int
    hourly: list[int] = Field(min_length=24, max_length=24)
    species: list[SpeciesStat]
    missing: list[str]


class ExportQuery(StrictModel):
    site_id: UUID
    since: datetime | None = None
    until: datetime | None = None

    @field_validator("since", "until")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Timestamp must contain a timezone")
        return value
