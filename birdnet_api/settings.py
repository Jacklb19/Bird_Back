"""Runtime configuration, read once from environment variables and validated with messages naming the variable.

Credentials and endpoints of optional features (database, authentication, Storage, remote manifest)
may be absent: each feature reports its own "not configured" error when a request needs it, so the API still
starts and serves everything else. Tunables have the defaults below and fail fast when malformed.
"""
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

from .domain import MAX_TIME_ZONE_LENGTH, is_time_zone

# Manifest shipped with the function, used unless MODEL_MANIFEST_PATH or MODEL_MANIFEST_URL says otherwise.
BUNDLED_MODEL_MANIFEST: Final = Path(__file__).with_name("model_manifest.json")
# The web app also serves the model files statically under this path.
DEFAULT_MODEL_RESOURCE_BASE_URL: Final = "/models/"
DEFAULT_DATABASE_CONNECT_TIMEOUT_SECONDS: Final = 10
DEFAULT_JWKS_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_STORAGE_TIMEOUT_SECONDS: Final = 20.0
DEFAULT_MODEL_MANIFEST_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_MAX_METADATA_BODY_BYTES: Final = 128 * 1024
DEFAULT_MAX_MANIFEST_BYTES: Final = 128 * 1024
DEFAULT_MAP_RESULT_LIMIT: Final = 2000
DEFAULT_EXPORT_ROW_LIMIT: Final = 20_000
# Created by migration 20261008000000_profiles_and_avatars.sql.
DEFAULT_AVATAR_BUCKET: Final = "avatars"
# Created by migration 20261011000000_recordings_and_review.sql.
DEFAULT_RECORDING_BUCKET: Final = "recordings"
# Recordings one account may keep. Supabase's free plan gives the whole project 1 GB of Storage, shared with the
# models, so this bound is what keeps a handful of accounts from filling it.
DEFAULT_RECORDING_QUOTA_BYTES: Final = 50 * 1024 * 1024
# Walks one account may keep (ADR-35). A full path takes under 100 kB in the database, so a full account stays
# under 20 MB of the free plan's 500 MB.
DEFAULT_WALK_QUOTA: Final = 200
# Language model that writes species cards and site reports (ADR-37). Gemini's free tier needs no billing; its
# limits are per project and only shown in AI Studio, so the daily caps below must stay under what it shows there.
DEFAULT_LLM_API_URL: Final = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_LLM_MODEL: Final = "gemini-3.5-flash-lite"
# Bounded so a card (public sources, then the model) fits the function's maxDuration of 60 s in vercel.json.
DEFAULT_LLM_TIMEOUT_SECONDS: Final = 20.0
DEFAULT_PUBLIC_DATA_TIMEOUT_SECONDS: Final = 6.0
# New texts per person per day, and in total per day; reading a text already written does not count.
DEFAULT_LLM_USER_DAILY_QUOTA: Final = 10
DEFAULT_LLM_DAILY_QUOTA: Final = 200
# A species card is written again after this many days, so it follows changes in its sources.
DEFAULT_SPECIES_CARD_MAX_AGE_DAYS: Final = 180
# Reference sounds of each species (ADR-42): xeno-canto's API v3 asks every caller for a key of a registered account.
DEFAULT_SOUNDS_API_URL: Final = "https://xeno-canto.org/api/3"
# Days Vercel's CDN keeps the answer for one species, so xeno-canto is asked about it at most once a week.
DEFAULT_SOUNDS_CACHE_DAYS: Final = 7
# Deployment region; used for statistics when the client sends no time zone.
DEFAULT_TIME_ZONE: Final = "America/Bogota"
# Vercel rejects function request bodies above 4.5 MB, so a larger metadata limit could never apply.
PLATFORM_MAX_REQUEST_BODY_BYTES: Final = 4_500_000
# Bare DNS names or IPv4 addresses: no scheme, port, path or credentials.
HOST_NAME: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")
HOST_SEPARATOR: Final = ","


class SettingsError(RuntimeError):
    """A configuration value is malformed. Not a ValueError, so request validation never mistakes it for bad input."""


@dataclass(frozen=True, slots=True)
class Settings:
    # PostgreSQL through Supabase's transaction pooler.
    database_url: str | None
    database_connect_timeout_seconds: int
    # Supabase Auth: issuer of access tokens and, for legacy HS256 projects only, the shared secret.
    supabase_auth_issuer: str | None
    supabase_jwt_secret: str | None
    jwks_timeout_seconds: float
    # Private Storage buckets for profile photos and saved recordings; `supabase_url` has no trailing slash.
    supabase_url: str | None
    supabase_service_role_key: str | None
    supabase_avatar_bucket: str
    supabase_recording_bucket: str
    recording_quota_bytes: int
    walk_quota: int
    storage_timeout_seconds: float
    # Language model (ADR-37); without a key the routes that write texts answer "not configured".
    llm_api_url: str
    llm_api_key: str | None
    llm_model: str
    llm_timeout_seconds: float
    llm_user_daily_quota: int
    llm_daily_quota: int
    species_card_max_age_days: int
    public_data_timeout_seconds: float
    # Reference sounds (ADR-42); without a key the route answers "not configured".
    sounds_api_url: str
    sounds_api_key: str | None
    sounds_cache_days: int
    # Error reports (ADR-43); without a DSN nothing is sent anywhere.
    sentry_dsn: str | None
    sentry_environment: str | None
    # Model manifest: the remote URL wins over the local file when set.
    model_manifest_path: Path
    model_manifest_url: str | None
    model_manifest_allowed_hosts: frozenset[str]
    model_manifest_timeout_seconds: float
    max_manifest_bytes: int
    # Base for relative model resources, without trailing slash.
    model_resource_base_url: str
    # Request and response bounds.
    max_metadata_body_bytes: int
    map_result_limit: int
    export_row_limit: int
    default_time_zone: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings":
        supabase_url = _text(environ, "SUPABASE_URL")
        model_manifest_url = _text(environ, "MODEL_MANIFEST_URL")
        manifest_path = _text(environ, "MODEL_MANIFEST_PATH")
        resource_base = _text(environ, "MODEL_RESOURCE_BASE_URL") or DEFAULT_MODEL_RESOURCE_BASE_URL
        allowed_hosts = _hosts(environ, "MODEL_MANIFEST_ALLOWED_HOSTS")
        if allowed_hosts is None:
            # Without an explicit list, trust only the project's own Supabase host (or the configured manifest's).
            default_host = _host_of(supabase_url) or _host_of(model_manifest_url)
            allowed_hosts = frozenset({default_host}) if default_host else frozenset()
        return cls(
            database_url=_text(environ, "DATABASE_URL"),
            database_connect_timeout_seconds=_positive_int(environ, "DATABASE_CONNECT_TIMEOUT_SECONDS", DEFAULT_DATABASE_CONNECT_TIMEOUT_SECONDS),
            supabase_auth_issuer=_text(environ, "SUPABASE_AUTH_ISSUER"),
            supabase_jwt_secret=_text(environ, "SUPABASE_JWT_SECRET"),
            jwks_timeout_seconds=_positive_seconds(environ, "JWKS_TIMEOUT_SECONDS", DEFAULT_JWKS_TIMEOUT_SECONDS),
            supabase_url=supabase_url.rstrip("/") if supabase_url else None,
            supabase_service_role_key=_text(environ, "SUPABASE_SERVICE_ROLE_KEY"),
            supabase_avatar_bucket=_text(environ, "SUPABASE_AVATAR_BUCKET") or DEFAULT_AVATAR_BUCKET,
            supabase_recording_bucket=_text(environ, "SUPABASE_RECORDING_BUCKET") or DEFAULT_RECORDING_BUCKET,
            recording_quota_bytes=_positive_int(environ, "RECORDING_QUOTA_BYTES", DEFAULT_RECORDING_QUOTA_BYTES),
            walk_quota=_positive_int(environ, "WALK_QUOTA", DEFAULT_WALK_QUOTA),
            storage_timeout_seconds=_positive_seconds(environ, "STORAGE_TIMEOUT_SECONDS", DEFAULT_STORAGE_TIMEOUT_SECONDS),
            llm_api_url=(_text(environ, "GEMINI_API_URL") or DEFAULT_LLM_API_URL).rstrip("/"),
            llm_api_key=_text(environ, "GEMINI_API_KEY"),
            llm_model=_text(environ, "GEMINI_MODEL") or DEFAULT_LLM_MODEL,
            llm_timeout_seconds=_positive_seconds(environ, "LLM_TIMEOUT_SECONDS", DEFAULT_LLM_TIMEOUT_SECONDS),
            llm_user_daily_quota=_positive_int(environ, "LLM_USER_DAILY_QUOTA", DEFAULT_LLM_USER_DAILY_QUOTA),
            llm_daily_quota=_positive_int(environ, "LLM_DAILY_QUOTA", DEFAULT_LLM_DAILY_QUOTA),
            species_card_max_age_days=_positive_int(environ, "SPECIES_CARD_MAX_AGE_DAYS", DEFAULT_SPECIES_CARD_MAX_AGE_DAYS),
            public_data_timeout_seconds=_positive_seconds(environ, "PUBLIC_DATA_TIMEOUT_SECONDS", DEFAULT_PUBLIC_DATA_TIMEOUT_SECONDS),
            sounds_api_url=(_text(environ, "XENO_CANTO_API_URL") or DEFAULT_SOUNDS_API_URL).rstrip("/"),
            sounds_api_key=_text(environ, "XENO_CANTO_API_KEY"),
            sounds_cache_days=_positive_int(environ, "SOUNDS_CACHE_DAYS", DEFAULT_SOUNDS_CACHE_DAYS),
            sentry_dsn=_text(environ, "SENTRY_DSN"),
            # Vercel names the environment of each deployment (production, preview), so preview errors stay apart.
            sentry_environment=_text(environ, "VERCEL_ENV"),
            model_manifest_path=Path(manifest_path) if manifest_path else BUNDLED_MODEL_MANIFEST,
            model_manifest_url=model_manifest_url,
            model_manifest_allowed_hosts=allowed_hosts,
            model_manifest_timeout_seconds=_positive_seconds(environ, "MODEL_MANIFEST_TIMEOUT_SECONDS", DEFAULT_MODEL_MANIFEST_TIMEOUT_SECONDS),
            max_manifest_bytes=_positive_int(environ, "MAX_MANIFEST_BYTES", DEFAULT_MAX_MANIFEST_BYTES),
            model_resource_base_url=resource_base.rstrip("/"),
            max_metadata_body_bytes=_positive_int(environ, "MAX_METADATA_BODY_BYTES", DEFAULT_MAX_METADATA_BODY_BYTES, PLATFORM_MAX_REQUEST_BODY_BYTES),
            map_result_limit=_positive_int(environ, "MAP_RESULT_LIMIT", DEFAULT_MAP_RESULT_LIMIT),
            export_row_limit=_positive_int(environ, "EXPORT_ROW_LIMIT", DEFAULT_EXPORT_ROW_LIMIT),
            default_time_zone=_time_zone(environ, "DEFAULT_TIME_ZONE", DEFAULT_TIME_ZONE),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings of this process; tests call `get_settings.cache_clear()` after changing the environment."""
    return Settings.from_env(os.environ)


def _text(environ: Mapping[str, str], name: str) -> str | None:
    # Blank values count as unset: an empty variable in the Vercel dashboard must not become an empty URL.
    value = environ.get(name, "").strip()
    return value or None


def _positive_int(environ: Mapping[str, str], name: str, default: int, maximum: int | None = None) -> int:
    raw = _text(environ, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a positive integer, got {raw!r}") from None
    if value <= 0:
        raise SettingsError(f"{name} must be a positive integer, got {raw!r}")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{name} must be at most {maximum}, got {raw!r}")
    return value


def _positive_seconds(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = _text(environ, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a positive number of seconds, got {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise SettingsError(f"{name} must be a positive number of seconds, got {raw!r}")
    return value


def _time_zone(environ: Mapping[str, str], name: str, default: str) -> str:
    value = _text(environ, name) or default
    if len(value) > MAX_TIME_ZONE_LENGTH or not is_time_zone(value):
        raise SettingsError(f"{name} must be an IANA time zone such as {DEFAULT_TIME_ZONE!r}, got {value!r}")
    return value


def _hosts(environ: Mapping[str, str], name: str) -> frozenset[str] | None:
    raw = _text(environ, name)
    if raw is None:
        return None
    hosts = frozenset(host.strip().lower() for host in raw.split(HOST_SEPARATOR))
    if not all(HOST_NAME.fullmatch(host) for host in hosts):
        raise SettingsError(f"{name} must be a comma-separated list of host names without scheme, port or path, got {raw!r}")
    return hosts


def _host_of(url: str | None) -> str | None:
    # A malformed URL yields no default host; the feature using it reports its own error when called.
    try:
        return urlparse(url).hostname if url else None
    except ValueError:
        return None
