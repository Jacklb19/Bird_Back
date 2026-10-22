"""Business rules shared by validation, persistence and export.

The web client mirrors these values in `src/config/contract.ts`; changing one side without the other makes
the server reject records the client considers valid, so keep both in step.
"""
from collections.abc import Mapping, Sequence
from enum import StrEnum
from math import asin, cos, radians, sin, sqrt
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

# Privacy: the raw device reading never leaves the device; coordinates are first rounded to a ~10 m cell
# (10^-4 degrees, about 11 m of latitude). The database function `public.round_to_location_grid` (migration
# 20261010000000) rounds to the same 4 decimals. Points on the earlier 3-decimal grid lie on this one too,
# so older rows and clients stay valid.
LOCATION_GRID_DECIMALS: Final = 4
# Float tolerance when checking that a coordinate already lies on that grid.
LOCATION_GRID_TOLERANCE: Final = 1e-9
MAX_LATITUDE: Final = 90.0
MAX_LONGITUDE: Final = 180.0
# Spatial reference of every stored point; the geography columns are declared with it (initial migration).
WGS84_SRID: Final = 4326

# Field length limits. Species, model version and site name mirror FIELD_LIMITS in the client.
MAX_SPECIES_LENGTH: Final = 200
MAX_MODEL_VERSION_LENGTH: Final = 200
MAX_RECORDING_PATH_LENGTH: Final = 300
MAX_SITE_NAME_LENGTH: Final = 80
# Profile name; the CHECK `profiles_alias_length` (migration 20261008000000) enforces the same bound.
MAX_ALIAS_LENGTH: Final = 40
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
# Bounds of the personal record: species listed, map cells and latest detections of one species.
MAX_OWN_SPECIES: Final = 2000
MAX_SPECIES_CELLS: Final = 500
MAX_RECENT_DETECTIONS: Final = 20


def is_time_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    # A zone directory such as "America" is not a zone: opening it raises an OSError
    # (IsADirectoryError on Linux, PermissionError on Windows) instead of ZoneInfoNotFoundError.
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


# Capture window of the client: three seconds at the sample rate the bundled model takes. The API handles no audio
# samples itself; the shipped model_manifest.json (sample_rate, window_samples) is checked against these values.
AUDIO_SAMPLE_RATE_HZ: Final = 48_000
AUDIO_WINDOW_SECONDS: Final = 3
AUDIO_WINDOW_SAMPLES: Final = AUDIO_SAMPLE_RATE_HZ * AUDIO_WINDOW_SECONDS


# Profile photo: one WebP per user, overwritten on change. Size and type match the `avatars` bucket limits and
# the path matches the CHECK `profiles_avatar_path_owned` (migration 20261008000000).
AVATAR_CONTENT_TYPE: Final = "image/webp"
MAX_AVATAR_BYTES: Final = 200_000
AVATAR_OBJECT_PATH_TEMPLATE: Final = "{owner}/avatar.webp"
# RIFF container header: "RIFF", 4-byte size, then the "WEBP" form type.
WEBP_RIFF_ID: Final = b"RIFF"
WEBP_FORM_TYPE: Final = b"WEBP"
WEBP_FORM_TYPE_OFFSET: Final = 8
# Lifetime of the signed photo URL in a profile response; the client asks for the profile again after it.
AVATAR_URL_TTL_SECONDS: Final = 3600


def avatar_object_path(owner: UUID) -> str:
    return AVATAR_OBJECT_PATH_TEMPLATE.format(owner=owner)


def is_webp(content: bytes) -> bool:
    return content.startswith(WEBP_RIFF_ID) and content[WEBP_FORM_TYPE_OFFSET:WEBP_FORM_TYPE_OFFSET + len(WEBP_FORM_TYPE)] == WEBP_FORM_TYPE


# Saved recording: the clip of one detection, compressed on the device (Opus in WebM), that its author chose to
# keep in their account. Type, size and path match the `recordings` bucket limits and the CHECK
# `detections_recording_owned` (migration 20261011000000).
RECORDING_CONTENT_TYPE: Final = "audio/webm"
# A three-second clip is some 12 kB at 32 kbit/s; the bound leaves room for a higher bitrate, not for other files.
MAX_RECORDING_BYTES: Final = 100_000
RECORDING_OBJECT_PATH_TEMPLATE: Final = "{owner}/{detection}.webm"
# EBML signature that opens every WebM file.
WEBM_SIGNATURE: Final = bytes.fromhex("1a45dfa3")
# Lifetime of the signed URL a recording is played from; the client asks again after it.
RECORDING_URL_TTL_SECONDS: Final = 600


def recording_object_path(owner: UUID, detection: UUID) -> str:
    return RECORDING_OBJECT_PATH_TEMPLATE.format(owner=owner, detection=detection)


def is_webm(content: bytes) -> bool:
    return content.startswith(WEBM_SIGNATURE)


class ReviewAction(StrEnum):
    """What a person can say about one of their detections after listening to it (RF-11)."""

    CONFIRM = "confirm"
    CORRECT = "correct"
    DISCARD = "discard"


class Reviewer(StrEnum):
    """Who reviewed a detection, as the API reports it."""

    PERSON = "person"


class DbReviewer(StrEnum):
    """Values stored in `detections.revisada_por`.

    The CHECK `detections_review_source` also admits "nube", for a cloud verifier that was never built: no code
    path writes it.
    """

    PERSON = "persona"


# Both vocabularies pair by member name, as the detection states do.
DB_TO_API_REVIEWER: Final[Mapping[DbReviewer, Reviewer]] = MappingProxyType({stored: Reviewer[stored.name] for stored in DbReviewer})


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
    # Appended after the original columns so files exported before the review existed keep their layout.
    MODEL_SPECIES = "model_species"
    REVIEWED_BY = "reviewed_by"
    # Appended after the review columns, for the same reason (ADR-40).
    INDIVIDUALS = "individuals"
    NOTE = "note"


# Coordinates are exported with LOCATION_GRID_DECIMALS, the precision they are stored with.
CONFIDENCE_EXPORT_DECIMALS: Final = 3
# Spreadsheets run a cell that starts with one of these as a formula; text written by a person (a note, a site
# name) is exported with a leading apostrophe instead, so opening the file never runs anything.
SPREADSHEET_FORMULA_PREFIXES: Final = ("=", "+", "-", "@", "\t", "\r")


def spreadsheet_text(value: str) -> str:
    return f"'{value}" if value.startswith(SPREADSHEET_FORMULA_PREFIXES) else value


# Notes and count of individuals per record (ADR-40); the CHECKs of migration 20261022000100 enforce the same bounds,
# and the client mirrors them in NOTE_LIMITS.
MAX_NOTE_LENGTH: Final = 500
MAX_INDIVIDUALS: Final = 999


# Threatened species on the collective map (ADR-41): to anyone but their author, detections of the IUCN categories
# below are shown at the centre of a cell of this many degrees (about 11 km) instead of the ~10 m one. The list of
# species comes from GBIF through scripts/build_threatened_species.py, never per request.
THREATENED_CATEGORIES: Final = frozenset({"VU", "EN", "CR", "EW"})
THREATENED_CELL_DEGREES: Final = 0.1
# Side of that cell along a meridian, in metres, as the client shows it.
THREATENED_CELL_METERS: Final = 11_000


# Walks saved in the account (ADR-35). Name and path bounds match the CHECKs of migration 20261015000000; the path
# bound mirrors WALK_TRACK_MAX_POINTS in the client, which stops a path from growing beyond it.
MAX_WALK_NAME_LENGTH: Final = 80
MAX_WALK_POINTS: Final = 2000
# Walks per page of the list, and the bounds of one walk's detail.
DEFAULT_WALK_PAGE: Final = 20
MAX_WALK_PAGE: Final = 50
# Walks per page of their paths for the map; each carries up to MAX_WALK_POINTS points.
DEFAULT_WALK_PATH_PAGE: Final = 10
MAX_WALK_PATH_PAGE: Final = 20
MAX_WALK_SPECIES: Final = 500
MAX_WALK_PLACES: Final = 2000
# Seconds a point's time may run past the end of its walk: the phone stamps both from the same clock, but rounds.
WALK_TIME_TOLERANCE_SECONDS: Final = 1
# Mean Earth radius (IUGG) in metres; the client's walkGeometry.ts measures paths with the same value and formula,
# so a walk shows the same distance on the phone and in the account.
EARTH_RADIUS_M: Final = 6_371_008.8


def distance_meters(start: tuple[float, float], end: tuple[float, float]) -> float:
    """Great-circle distance between two cells given as (latitude, longitude), in metres (haversine)."""
    latitude_delta = radians(end[0] - start[0])
    longitude_delta = radians(end[1] - start[1])
    a = sin(latitude_delta / 2) ** 2 + cos(radians(start[0])) * cos(radians(end[0])) * sin(longitude_delta / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(min(1.0, sqrt(a)))


def track_meters(track: Sequence[tuple[float, float]]) -> float:
    """Length of a path, in metres."""
    return sum(distance_meters(track[index - 1], track[index]) for index in range(1, len(track)))


# Texts written by the language model (ADR-37): species cards and site reports, in the interface languages.
class CardLanguage(StrEnum):
    """Languages of the texts; the CHECKs of migration 20261021000000 list the same two."""

    ES = "es"
    EN = "en"


# Scientific names as the acoustic models label them: genus and epithet, sometimes with a hyphen. Anything else is
# refused before any public source or the model is asked.
SCIENTIFIC_NAME_PATTERN: Final = r"[A-Z][a-z]+ [a-z]+(?:-[a-z]+)?"
# Common names the client sends for a report: letters, spaces and the punctuation bird names use.
COMMON_NAME_PATTERN: Final = r"[^\W\d_](?:[^\W\d_]|[ '\-.,()])*"
MAX_COMMON_NAME_LENGTH: Final = 80
# Species of a report named in the prompt; the statistics list them most heard first. The client may name them and
# the species not heard recently.
MAX_REPORT_SPECIES: Final = 40
MAX_REPORT_NAMES: Final = MAX_REPORT_SPECIES + MAX_MISSING_SPECIES
# Versions of the instructions given to the model: raising one makes the API write those texts again.
SPECIES_CARD_INSTRUCTIONS_VERSION: Final = 1
SITE_REPORT_INSTRUCTIONS_VERSION: Final = 1
