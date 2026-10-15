"""Every error the API reports, with its HTTP status and the message sent as `{"detail": ...}`.

The web client reacts to status codes only; messages are for people reading logs and responses.
"""
from enum import Enum, unique
from http import HTTPStatus
from typing import NoReturn

from fastapi import HTTPException
from fastapi.responses import JSONResponse


@unique
class ErrorCode(Enum):
    BODY_TOO_LARGE = (HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body exceeds metadata limit")
    CONFIGURATION_INVALID = (HTTPStatus.SERVICE_UNAVAILABLE, "Service configuration is invalid")
    AUTH_REQUIRED = (HTTPStatus.UNAUTHORIZED, "Authentication required")
    AUTH_NOT_CONFIGURED = (HTTPStatus.SERVICE_UNAVAILABLE, "Authentication is not configured")
    SESSION_INVALID = (HTTPStatus.UNAUTHORIZED, "Invalid or expired session")
    DATABASE_NOT_CONFIGURED = (HTTPStatus.SERVICE_UNAVAILABLE, "Database is not configured")
    DATABASE_UNAVAILABLE = (HTTPStatus.SERVICE_UNAVAILABLE, "Database operation failed; retry later")
    STATUS_CONFIDENCE_MISMATCH = (HTTPStatus.UNPROCESSABLE_ENTITY, "Confidence and verification state disagree")
    DETECTION_NOT_FOUND = (HTTPStatus.NOT_FOUND, "Detection not found")
    INVALID_MAP_BOUNDS = (HTTPStatus.UNPROCESSABLE_ENTITY, "Invalid map bounds")
    INVALID_PERIOD = (HTTPStatus.UNPROCESSABLE_ENTITY, "Invalid period")
    UNKNOWN_SITE = (HTTPStatus.UNPROCESSABLE_ENTITY, "Unknown site")
    SITE_NOT_FOUND = (HTTPStatus.NOT_FOUND, "Site not found")
    DETECTION_ID_CONFLICT = (HTTPStatus.CONFLICT, "Detection identifier conflict")
    MODEL_MANIFEST_UNAVAILABLE = (HTTPStatus.SERVICE_UNAVAILABLE, "Model manifest unavailable")
    STORAGE_NOT_CONFIGURED = (HTTPStatus.SERVICE_UNAVAILABLE, "Storage is not configured")
    UPLOAD_NOT_PREPARED = (HTTPStatus.BAD_GATEWAY, "Upload could not be prepared")
    INVALID_AVATAR_PATH = (HTTPStatus.UNPROCESSABLE_ENTITY, "Invalid profile photo path")
    AVATAR_NOT_VERIFIED = (HTTPStatus.UNPROCESSABLE_ENTITY, "Uploaded profile photo could not be verified")
    INVALID_RECORDING_PATH = (HTTPStatus.UNPROCESSABLE_ENTITY, "Invalid recording path")
    RECORDING_NOT_VERIFIED = (HTTPStatus.UNPROCESSABLE_ENTITY, "Uploaded recording could not be verified")
    RECORDING_NOT_FOUND = (HTTPStatus.NOT_FOUND, "Detection has no recording")
    RECORDING_QUOTA_EXCEEDED = (HTTPStatus.INSUFFICIENT_STORAGE, "Recording quota of the account is full")
    RECORDING_UNAVAILABLE = (HTTPStatus.BAD_GATEWAY, "Recording storage did not answer; retry later")
    WALK_NOT_FOUND = (HTTPStatus.NOT_FOUND, "Walk not found")
    WALK_ID_CONFLICT = (HTTPStatus.CONFLICT, "Walk identifier conflict")
    WALK_QUOTA_EXCEEDED = (HTTPStatus.INSUFFICIENT_STORAGE, "Walk quota of the account is full")
    EXPORT_SCOPE_INVALID = (HTTPStatus.UNPROCESSABLE_ENTITY, "Export either a site or a walk, not both")

    def __init__(self, status: HTTPStatus, message: str) -> None:
        self.status = status
        self.message = message


def http_error(code: ErrorCode) -> HTTPException:
    return HTTPException(status_code=int(code.status), detail=code.message)


def raise_error(code: ErrorCode) -> NoReturn:
    # Callers raise this while handling internal errors (driver, network, parser); keep them out of the chain.
    raise http_error(code) from None


def error_response(code: ErrorCode) -> JSONResponse:
    """Same body as an HTTPException, for middleware that runs outside FastAPI's exception handlers."""
    return JSONResponse({"detail": code.message}, status_code=int(code.status))
