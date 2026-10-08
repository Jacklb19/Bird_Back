"""Sign private uploads and downloads, and verify uploaded objects by their exact path and content before linking."""
import logging
import struct
from typing import Final
from uuid import UUID

import httpx

from .contracts import AudioUpload, AvatarUpload
from .domain import (
    AVATAR_URL_TTL_SECONDS,
    EXPECTED_WAV_BYTES,
    EXPECTED_WAV_HEADER,
    MAX_AVATAR_BYTES,
    WAV_HEADER_LAYOUT,
    audio_object_path,
    avatar_object_path,
    is_webp,
)
from .errors import ErrorCode, raise_error
from .settings import get_settings

# Supabase Storage REST API, relative to SUPABASE_URL.
STORAGE_API_PATH: Final = "/storage/v1"
# Signing endpoints; the relative URLs they return start with the same prefix.
SIGNED_UPLOAD_PATH: Final = "/object/upload/sign/"
SIGNED_UPLOAD_URL_FIELD: Final = "url"
SIGNED_DOWNLOAD_PATH: Final = "/object/sign/"
SIGNED_DOWNLOAD_URL_FIELD: Final = "signedURL"
SIGNED_DOWNLOAD_EXPIRY_FIELD: Final = "expiresIn"
OBJECT_PATH: Final = "/object/"
API_KEY_HEADER: Final = "apikey"
# Object paths are deterministic, so a retry after a lost response (or a new photo) must be allowed to overwrite.
UPSERT_HEADER: Final = "x-upsert"

logger = logging.getLogger(__name__)


class BucketStorage:
    """Service-role access to one private bucket; the key never reaches the client, only signed URLs do."""

    def __init__(self, bucket: str | None) -> None:
        settings = get_settings()
        self.base = settings.supabase_url or ""
        self.secret = settings.supabase_service_role_key or ""
        self.bucket = bucket or ""
        self.timeout = settings.storage_timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.base and self.secret and self.bucket)

    def client(self) -> httpx.Client:
        if not self.configured:
            raise_error(ErrorCode.STORAGE_NOT_CONFIGURED)
        return httpx.Client(base_url=f"{self.base}{STORAGE_API_PATH}", headers={API_KEY_HEADER: self.secret, "Authorization": f"Bearer {self.secret}"}, timeout=self.timeout)

    def sign_upload(self, path: str) -> str:
        try:
            with self.client() as client:
                response = client.post(f"{SIGNED_UPLOAD_PATH}{self.bucket}/{path}", headers={UPSERT_HEADER: "true"}, json={})
                response.raise_for_status()
                return self._absolute(response.json()[SIGNED_UPLOAD_URL_FIELD], SIGNED_UPLOAD_PATH)
        except (httpx.HTTPError, KeyError, ValueError):
            raise_error(ErrorCode.UPLOAD_NOT_PREPARED)

    def sign_download(self, path: str, expires_seconds: int) -> str:
        """Raises httpx.HTTPError, KeyError or ValueError, also for a missing object; callers decide how to report it."""
        with self.client() as client:
            response = client.post(f"{SIGNED_DOWNLOAD_PATH}{self.bucket}/{path}", json={SIGNED_DOWNLOAD_EXPIRY_FIELD: expires_seconds})
            response.raise_for_status()
            return self._absolute(response.json()[SIGNED_DOWNLOAD_URL_FIELD], SIGNED_DOWNLOAD_PATH)

    def read(self, path: str, limit: int) -> bytes:
        """Object content, refused with ValueError past `limit` bytes; transport and HTTP errors raise httpx.HTTPError."""
        with self.client() as client, client.stream("GET", f"{OBJECT_PATH}{self.bucket}/{path}") as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > limit:
                    raise ValueError("Object exceeds expected length")
        return bytes(content)

    def _absolute(self, url: object, prefix: str) -> str:
        # Only Storage-relative URLs are accepted, so a tampered response cannot send the client elsewhere.
        if not isinstance(url, str) or not url.startswith(prefix):
            raise ValueError("Invalid storage response")
        return f"{self.base}{STORAGE_API_PATH}{url}"


class AudioStorage(BucketStorage):
    def __init__(self) -> None:
        super().__init__(get_settings().supabase_audio_bucket)

    @staticmethod
    def path(owner: UUID, detection: UUID) -> str:
        return audio_object_path(owner, detection)

    def sign(self, owner: UUID, detection: UUID) -> AudioUpload:
        path = self.path(owner, detection)
        return AudioUpload(upload_url=self.sign_upload(path), audio_path=path)

    def verify(self, owner: UUID, detection: UUID, path: str) -> None:
        if path != self.path(owner, detection):
            raise_error(ErrorCode.INVALID_AUDIO_PATH)
        try:
            content = self.read(path, EXPECTED_WAV_BYTES)
            # The client records exactly one model window, so anything else is not one of its fragments.
            if len(content) != EXPECTED_WAV_BYTES or struct.unpack_from(WAV_HEADER_LAYOUT, content) != EXPECTED_WAV_HEADER:
                raise ValueError("Unsupported WAV")
        except (httpx.HTTPError, ValueError):
            raise_error(ErrorCode.AUDIO_NOT_VERIFIED)


class AvatarStorage(BucketStorage):
    def __init__(self) -> None:
        super().__init__(get_settings().supabase_avatar_bucket)

    def sign(self, owner: UUID) -> AvatarUpload:
        path = avatar_object_path(owner)
        return AvatarUpload(upload_url=self.sign_upload(path), avatar_path=path)

    def verify(self, owner: UUID, path: str) -> None:
        if path != avatar_object_path(owner):
            raise_error(ErrorCode.INVALID_AVATAR_PATH)
        try:
            # The bucket checks the declared type and size; the bytes themselves are checked here before linking.
            if not is_webp(self.read(path, MAX_AVATAR_BYTES)):
                raise ValueError("Unsupported image")
        except (httpx.HTTPError, ValueError):
            raise_error(ErrorCode.AVATAR_NOT_VERIFIED)

    def url(self, path: str | None) -> str | None:
        """Short-lived link to the photo; None without photo or Storage, so a photo problem never hides the profile."""
        if path is None or not self.configured:
            return None
        try:
            return self.sign_download(path, AVATAR_URL_TTL_SECONDS)
        except (httpx.HTTPError, KeyError, ValueError) as error:
            logger.warning("Profile photo could not be signed: %s", type(error).__name__)
            return None
