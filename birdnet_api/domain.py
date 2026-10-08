"""Business rules shared by validation, persistence and export.

The web client mirrors these values in `src/config/contract.ts`; changing one side without the other makes
the server reject records the client considers valid, so keep both in step.
"""
import struct
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Confidence thresholds (Table 7 of the specification): below the first nothing is kept, from the second a
# detection is confirmed locally. They mirror the `model_versions.umbrales` default ("intermedia", "alta") in
# supabase/migrations/20260928000000_initial_schema.sql and CONFIDENCE_THRESHOLDS in the client.
CONFIDENCE_DISCARD_BELOW: Final = 0.45
CONFIDENCE_CONFIRMED_FROM: Final = 0.80
# Confidences are probabilities; the CHECK on `detections.confianza` enforces the same upper bound.
MAX_CONFIDENCE: Final = 1.0

# Privacy: coordinates are rounded to a ~100 m cell (10^-3 degrees) before they leave the device. The
# database trigger `public.round_to_100m_grid` (initial migration) rounds to the same 3 decimals.
LOCATION_GRID_DECIMALS: Final = 3
# Float tolerance when checking that a coordinate already lies on that grid.
LOCATION_GRID_TOLERANCE: Final = 1e-9
MAX_LATITUDE: Final = 90.0
MAX_LONGITUDE: Final = 180.0
# Spatial reference of every stored point; the geography columns are declared with it (initial migration).
WGS84_SRID: Final = 4326

# Field length limits. Species, model version and site name mirror FIELD_LIMITS in the client.
MAX_SPECIES_LENGTH: Final = 200
MAX_MODEL_VERSION_LENGTH: Final = 200
MAX_AUDIO_PATH_LENGTH: Final = 300
MAX_SITE_NAME_LENGTH: Final = 80
# IANA zone names are far shorter; the limit only bounds untrusted input.
MAX_TIME_ZONE_LENGTH: Final = 64

# Detections per synchronization request; mirrors SYNC_BATCH_SIZE in the client.
MAX_SYNC_BATCH_SIZE: Final = 50


class DetectionStatus(StrEnum):
    """Verification state as the API reports it."""

    CONFIRMED = "confirmed"
    PROVISIONAL = "provisional"
    VERIFIED = "verified"
    CORRECTED = "corrected"
    DISCARDED = "discarded"


class DbDetectionStatus(StrEnum):
    """Values stored in `detections.estado`; the CHECK constraint in the initial migration lists the same five."""

    CONFIRMED = "confirmada"
    PROVISIONAL = "provisional"
    VERIFIED = "verificada"
    CORRECTED = "corregida"
    DISCARDED = "descartada"


# Both vocabularies pair by member name, so a state missing on either side fails at import time.
DB_TO_API_STATUS: Final[Mapping[DbDetectionStatus, DetectionStatus]] = MappingProxyType(
    {stored: DetectionStatus[stored.name] for stored in DbDetectionStatus}
)
API_TO_DB_STATUS: Final[Mapping[DetectionStatus, DbDetectionStatus]] = MappingProxyType(
    {status: DbDetectionStatus[status.name] for status in DetectionStatus}
)

# States the client assigns on the device; verification states are set only on the server.
SubmittedDetectionStatus = Literal[DetectionStatus.CONFIRMED, DetectionStatus.PROVISIONAL]
# States visible on the collective map: discarded detections stay private to their author.
MapDetectionStatus = Literal[DetectionStatus.CONFIRMED, DetectionStatus.PROVISIONAL, DetectionStatus.VERIFIED, DetectionStatus.CORRECTED]


def local_status(confidence: float) -> SubmittedDetectionStatus:
    """State the device must assign to a kept detection with this confidence."""
    return DetectionStatus.CONFIRMED if confidence >= CONFIDENCE_CONFIRMED_FROM else DetectionStatus.PROVISIONAL


class StatsPeriod(StrEnum):
    """Statistics periods with their length in days (None is the whole record); mirrors PERIODS in the client."""

    days: int | None

    WEEK = ("week", 7)
    MONTH = ("month", 30)
    YEAR = ("year", 365)
    ALL = ("all", None)

    def __new__(cls, value: str, days: int | None) -> "StatsPeriod":
        member = str.__new__(cls, value)
        member._value_ = value
        member.days = days
        return member


DEFAULT_STATS_PERIOD: Final = StatsPeriod.MONTH
# Species "not detected recently" listed in the statistics.
MAX_MISSING_SPECIES: Final = 10
# Length of the per-hour activity series.
HOURS_PER_DAY: Final = 24


def is_time_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    # A zone directory such as "America" is not a zone: opening it raises an OSError
    # (IsADirectoryError on Linux, PermissionError on Windows) instead of ZoneInfoNotFoundError.
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


# Audio fragment format: one analysis window of the model as mono 16-bit PCM WAV. The sample rate and window
# length are the model's input (model_manifest.json: sample_rate, window_samples, window_seconds).
AUDIO_SAMPLE_RATE_HZ: Final = 48_000
AUDIO_WINDOW_SECONDS: Final = 3
AUDIO_CHANNELS: Final = 1
AUDIO_BITS_PER_SAMPLE: Final = 16
AUDIO_WINDOW_SAMPLES: Final = AUDIO_SAMPLE_RATE_HZ * AUDIO_WINDOW_SECONDS
BITS_PER_BYTE: Final = 8
AUDIO_BLOCK_ALIGN: Final = AUDIO_CHANNELS * (AUDIO_BITS_PER_SAMPLE // BITS_PER_BYTE)
AUDIO_BYTE_RATE: Final = AUDIO_SAMPLE_RATE_HZ * AUDIO_BLOCK_ALIGN
AUDIO_DATA_BYTES: Final = AUDIO_WINDOW_SAMPLES * AUDIO_BLOCK_ALIGN

# Canonical 44-byte header: RIFF id, RIFF size, "WAVE", "fmt ", fmt size, format, channels, sample rate,
# byte rate, block align, bits per sample, "data", data size.
WAV_HEADER_LAYOUT: Final = "<4sI4s4sIHHIIHH4sI"
WAV_HEADER_BYTES: Final = struct.calcsize(WAV_HEADER_LAYOUT)
# The RIFF size excludes the RIFF id and the size field itself.
WAV_RIFF_PREAMBLE_BYTES: Final = 8
WAV_PCM_FMT_CHUNK_BYTES: Final = 16
WAV_PCM_FORMAT: Final = 1
EXPECTED_WAV_BYTES: Final = WAV_HEADER_BYTES + AUDIO_DATA_BYTES
EXPECTED_WAV_HEADER: Final = (
    b"RIFF", EXPECTED_WAV_BYTES - WAV_RIFF_PREAMBLE_BYTES, b"WAVE", b"fmt ", WAV_PCM_FMT_CHUNK_BYTES, WAV_PCM_FORMAT,
    AUDIO_CHANNELS, AUDIO_SAMPLE_RATE_HZ, AUDIO_BYTE_RATE, AUDIO_BLOCK_ALIGN, AUDIO_BITS_PER_SAMPLE, b"data", AUDIO_DATA_BYTES,
)
AUDIO_CONTENT_TYPE: Final = "audio/wav"
AUDIO_FILE_EXTENSION: Final = "wav"
# Storage object key of a detection's audio; the client checks the path the API returns against the same rule.
AUDIO_OBJECT_PATH_TEMPLATE: Final = "{owner}/{detection}.{extension}"


def audio_object_path(owner: UUID, detection: UUID) -> str:
    return AUDIO_OBJECT_PATH_TEMPLATE.format(owner=owner, detection=detection, extension=AUDIO_FILE_EXTENSION)


class ExportColumn(StrEnum):
    """CSV export columns, in file order; the names are part of the export contract."""

    ID = "id"
    SPECIES = "species"
    CONFIDENCE = "confidence"
    STATUS = "status"
    RECORDED_AT = "recorded_at"
    LATITUDE = "latitude"
    LONGITUDE = "longitude"
    MODEL_VERSION = "model_version"
    SITE = "site"


# Coordinates are exported with LOCATION_GRID_DECIMALS, the precision they are stored with.
CONFIDENCE_EXPORT_DECIMALS: Final = 3
