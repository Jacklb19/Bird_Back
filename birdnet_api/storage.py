"""Sign private uploads and verify their exact path and WAV format before linking."""
import struct
from typing import Final
from uuid import UUID

import httpx

from .contracts import AudioUpload
from .domain import EXPECTED_WAV_BYTES, EXPECTED_WAV_HEADER, WAV_HEADER_LAYOUT, audio_object_path
from .errors import ErrorCode, raise_error
from .settings import get_settings

# Supabase Storage REST API, relative to SUPABASE_URL.
STORAGE_API_PATH: Final = "/storage/v1"
# Signing endpoint; the relative URL it returns, in SIGNED_URL_FIELD, starts with the same prefix.
SIGNED_UPLOAD_PATH: Final = "/object/upload/sign/"
SIGNED_URL_FIELD: Final = "url"
OBJECT_PATH: Final = "/object/"
API_KEY_HEADER: Final = "apikey"
# A retry after a lost response signs the same deterministic path again, so the upload must be allowed to overwrite it.
UPSERT_HEADER: Final = "x-upsert"


class AudioStorage:
    def __init__(self) -> None:
        settings = get_settings()
        self.base = settings.supabase_url or ""
        self.secret = settings.supabase_service_role_key or ""
        self.bucket = settings.supabase_audio_bucket or ""
        self.timeout = settings.storage_timeout_seconds

    @staticmethod
    def path(owner: UUID, detection: UUID) -> str:
        return audio_object_path(owner, detection)

    def client(self) -> httpx.Client:
        if not self.base or not self.secret or not self.bucket:
            raise_error(ErrorCode.STORAGE_NOT_CONFIGURED)
        return httpx.Client(base_url=f"{self.base}{STORAGE_API_PATH}", headers={API_KEY_HEADER: self.secret, "Authorization": f"Bearer {self.secret}"}, timeout=self.timeout)

    def sign(self, owner: UUID, detection: UUID) -> AudioUpload:
        path = self.path(owner, detection)
        try:
            with self.client() as client:
                response = client.post(f"{SIGNED_UPLOAD_PATH}{self.bucket}/{path}", headers={UPSERT_HEADER: "true"}, json={})
                response.raise_for_status()
                url = response.json()[SIGNED_URL_FIELD]
                if not isinstance(url, str) or not url.startswith(SIGNED_UPLOAD_PATH):
                    raise ValueError("Invalid storage response")
                return AudioUpload(upload_url=f"{self.base}{STORAGE_API_PATH}{url}", audio_path=path)
        except (httpx.HTTPError, KeyError, ValueError):
            raise_error(ErrorCode.UPLOAD_NOT_PREPARED)

    def verify(self, owner: UUID, detection: UUID, path: str) -> None:
        if path != self.path(owner, detection):
            raise_error(ErrorCode.INVALID_AUDIO_PATH)
        try:
            with self.client() as client, client.stream("GET", f"{OBJECT_PATH}{self.bucket}/{path}") as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > EXPECTED_WAV_BYTES:
                        raise ValueError("Audio exceeds expected length")
            # The client records exactly one model window, so anything else is not one of its fragments.
            if len(content) != EXPECTED_WAV_BYTES or struct.unpack_from(WAV_HEADER_LAYOUT, content) != EXPECTED_WAV_HEADER:
                raise ValueError("Unsupported WAV")
        except (httpx.HTTPError, ValueError):
            raise_error(ErrorCode.AUDIO_NOT_VERIFIED)
