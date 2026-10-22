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
    SECOND_FACTOR_REQUIRED = (HTTPStatus.FORBIDDEN, "This account asks for the code of its authenticator app")
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
    LLM_NOT_CONFIGURED = (HTTPStatus.SERVICE_UNAVAILABLE, "Language model is not configured")
    LLM_UNAVAILABLE = (HTTPStatus.SERVICE_UNAVAILABLE, "Language model did not write the text; retry later")
    LLM_QUOTA_EXCEEDED = (HTTPStatus.TOO_MANY_REQUESTS, "Daily limit of new texts reached")
    SPECIES_UNKNOWN = (HTTPStatus.UNPROCESSABLE_ENTITY, "Not a bird species known to GBIF")
    PUBLIC_DATA_UNAVAILABLE = (HTTPStatus.BAD_GATEWAY, "Public sources did not answer; retry later")
    CARD_NOT_FOUND = (HTTPStatus.NOT_FOUND, "No card written for this species yet")
    REPORT_NOT_FOUND = (HTTPStatus.NOT_FOUND, "No report written for this site and period yet")
    NOTHING_TO_REPORT = (HTTPStatus.UNPROCESSABLE_ENTITY, "No detections in this period to report on")
    ACCOUNT_NOT_DELETED = (HTTPStatus.BAD_GATEWAY, "Account could not be deleted; nothing was lost, retry later")
    SOUNDS_NOT_CONFIGURED = (HTTPStatus.SERVICE_UNAVAILABLE, "Reference sounds are not configured")
    SOUNDS_UNAVAILABLE = (HTTPStatus.BAD_GATEWAY, "xeno-canto did not answer; retry later")

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
