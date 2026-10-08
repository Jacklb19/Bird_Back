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
    AUDIO_ONLY_FOR_PROVISIONAL = (HTTPStatus.UNPROCESSABLE_ENTITY, "Only provisional detections can include audio")
    DETECTION_NOT_SYNCHRONIZED = (HTTPStatus.NOT_FOUND, "Detection must be synchronized before uploading audio")
    DETECTION_NOT_FOUND = (HTTPStatus.NOT_FOUND, "Detection not found")
    UPLOAD_ONLY_FOR_PROVISIONAL = (HTTPStatus.UNPROCESSABLE_ENTITY, "Only provisional audio may be uploaded")
    INVALID_MAP_BOUNDS = (HTTPStatus.UNPROCESSABLE_ENTITY, "Invalid map bounds")
    INVALID_PERIOD = (HTTPStatus.UNPROCESSABLE_ENTITY, "Invalid period")
    UNKNOWN_SITE = (HTTPStatus.UNPROCESSABLE_ENTITY, "Unknown site")
    SITE_NOT_FOUND = (HTTPStatus.NOT_FOUND, "Site not found")
    DETECTION_ID_CONFLICT = (HTTPStatus.CONFLICT, "Detection identifier conflict")
    AUDIO_ID_CONFLICT = (HTTPStatus.CONFLICT, "Audio identifier conflict")
    MODEL_MANIFEST_UNAVAILABLE = (HTTPStatus.SERVICE_UNAVAILABLE, "Model manifest unavailable")
    STORAGE_NOT_CONFIGURED = (HTTPStatus.SERVICE_UNAVAILABLE, "Audio storage is not configured")
    UPLOAD_NOT_PREPARED = (HTTPStatus.BAD_GATEWAY, "Audio upload could not be prepared")
    INVALID_AUDIO_PATH = (HTTPStatus.UNPROCESSABLE_ENTITY, "Invalid audio path")
    AUDIO_NOT_VERIFIED = (HTTPStatus.UNPROCESSABLE_ENTITY, "Uploaded audio could not be verified")

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
