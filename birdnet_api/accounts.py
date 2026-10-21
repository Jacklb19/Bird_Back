"""Deleting an account and everything that hangs from it (ADR-38).

Storage objects go first (the profile photo and every saved recording, also uploads that never got linked):
Supabase refuses to delete a user who still owns objects. Then the Supabase Auth user, whose removal cascades in
the database to the profile and from it to detections, sites, walks, reports and usage counts (`on delete cascade`
in the migrations). If Storage fails nothing else is touched; if Auth fails after Storage, repeating the request
finishes the job, since an empty folder lists nothing.
"""
import logging
from typing import Final
from uuid import UUID

import httpx

from .errors import ErrorCode, raise_error
from .settings import get_settings
from .storage import API_KEY_HEADER, BucketStorage

AUTH_ADMIN_USERS_PATH: Final = "/auth/v1/admin/users/{owner}"
# Storage removes at most this many objects per request.
REMOVE_BATCH: Final = 1000
NOT_FOUND: Final = 404

logger = logging.getLogger(__name__)


class AccountDeletion:
    def __init__(self) -> None:
        settings = get_settings()
        self.base = settings.supabase_url or ""
        self.secret = settings.supabase_service_role_key or ""
        self.timeout = settings.storage_timeout_seconds
        self.buckets = [BucketStorage(settings.supabase_avatar_bucket), BucketStorage(settings.supabase_recording_bucket)]

    def delete(self, owner: UUID) -> None:
        if not (self.base and self.secret):
            raise_error(ErrorCode.STORAGE_NOT_CONFIGURED)
        try:
            for bucket in self.buckets:
                paths = bucket.folder(str(owner))
                for start in range(0, len(paths), REMOVE_BATCH):
                    bucket.remove(paths[start:start + REMOVE_BATCH])
        except (httpx.HTTPError, KeyError, ValueError) as error:
            logger.warning("Account objects could not be removed: %s", type(error).__name__)
            raise_error(ErrorCode.ACCOUNT_NOT_DELETED)
        self._delete_user(owner)

    def _delete_user(self, owner: UUID) -> None:
        headers = {API_KEY_HEADER: self.secret, "Authorization": f"Bearer {self.secret}"}
        try:
            with httpx.Client(base_url=self.base, headers=headers, timeout=self.timeout) as client:
                response = client.delete(AUTH_ADMIN_USERS_PATH.format(owner=owner))
        except httpx.HTTPError as error:
            logger.warning("Account user could not be removed: %s", type(error).__name__)
            raise_error(ErrorCode.ACCOUNT_NOT_DELETED)
        # A user already gone means an earlier request got this far and lost its answer.
        if response.is_error and response.status_code != NOT_FOUND:
            logger.warning("Auth answered %s when removing a user", response.status_code)
            raise_error(ErrorCode.ACCOUNT_NOT_DELETED)
