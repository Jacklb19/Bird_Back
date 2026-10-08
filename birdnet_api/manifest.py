"""Serve the active model manifest from Storage or the bundled copy, with resource paths made absolute."""
import json
from typing import Any, Final
from urllib.parse import urlparse

import httpx

from .errors import ErrorCode, raise_error
from .settings import Settings

# The remote manifest decides which model every client downloads, so it is fetched only over TLS.
MANIFEST_URL_SCHEME: Final = "https"
# Manifest fields that name model resources; relative values are resolved against MODEL_RESOURCE_BASE_URL.
RESOURCE_FIELDS: Final = ("model_file", "labels_file")
ABSOLUTE_RESOURCE_PREFIXES: Final = ("https://", "http://", "/")
# Tolerates the byte order mark some Windows editors add to JSON files.
MANIFEST_FILE_ENCODING: Final = "utf-8-sig"


def load_manifest(settings: Settings) -> dict[str, Any]:
    try:
        manifest = _fetch(settings, settings.model_manifest_url) if settings.model_manifest_url else _read(settings)
        if not isinstance(manifest, dict):
            raise ValueError("Manifest is not an object")
        for field in RESOURCE_FIELDS:
            resource = manifest[field]
            if not isinstance(resource, str):
                raise ValueError(f"Manifest field {field} is not a string")
            if not resource.startswith(ABSOLUTE_RESOURCE_PREFIXES):
                manifest[field] = f"{settings.model_resource_base_url}/{resource}"
        return manifest
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        raise_error(ErrorCode.MODEL_MANIFEST_UNAVAILABLE)


def _read(settings: Settings) -> Any:
    return json.loads(settings.model_manifest_path.read_text(encoding=MANIFEST_FILE_ENCODING))


def _fetch(settings: Settings, url: str) -> Any:
    origin = urlparse(url)
    if origin.scheme != MANIFEST_URL_SCHEME or origin.hostname not in settings.model_manifest_allowed_hosts:
        raise ValueError("Manifest origin is not allowed")
    with httpx.Client(timeout=settings.model_manifest_timeout_seconds) as client, client.stream("GET", url) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > settings.max_manifest_bytes:
                raise ValueError("Manifest exceeds the size limit")
    return json.loads(content)
