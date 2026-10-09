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
    # Private Storage buckets for audio and profile photos; `supabase_url` has no trailing slash.
    supabase_url: str | None
    supabase_service_role_key: str | None
    supabase_audio_bucket: str | None
    supabase_avatar_bucket: str
    storage_timeout_seconds: float
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
            supabase_audio_bucket=_text(environ, "SUPABASE_AUDIO_BUCKET"),
            supabase_avatar_bucket=_text(environ, "SUPABASE_AVATAR_BUCKET") or DEFAULT_AVATAR_BUCKET,
            storage_timeout_seconds=_positive_seconds(environ, "STORAGE_TIMEOUT_SECONDS", DEFAULT_STORAGE_TIMEOUT_SECONDS),
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
