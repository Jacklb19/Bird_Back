"""Validate every client-controlled value before it reaches PostgreSQL."""
from datetime import datetime
from math import isclose
from typing import Literal
from uuid import UUID

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
