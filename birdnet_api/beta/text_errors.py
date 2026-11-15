"""Safe errors for new free-text routes, without changing established validation responses."""
from collections.abc import Callable, Coroutine
from enum import Enum
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from .text_contracts import CONSENT_VERSION, TextConsent


class TextErrorCode(Enum):
    CONSENT_REQUIRED = (422, "consent_required", "Explicit current consent is required")
    CONSENT_VERSION_CHANGED = (409, "consent_version_changed", "Consent notice changed; review it before sending text")
    INVALID_REQUEST = (422, "invalid_assistant_request", "Invalid assistant request")
    MODEL_OUTPUT_INVALID = (503, "model_output_invalid", "Language model response was not usable; retry later")

    def __init__(self, status: int, code: str, detail: str) -> None:
        self.status, self.code, self.detail = status, code, detail


class TextError(Exception):
    def __init__(self, kind: TextErrorCode):
        self.kind = kind
        super().__init__(kind.code)


def require_consent(consent: TextConsent) -> None:
    if not consent.accepted:
        raise TextError(TextErrorCode.CONSENT_REQUIRED)
    if consent.version != CONSENT_VERSION:
        raise TextError(TextErrorCode.CONSENT_VERSION_CHANGED)


def error_response(kind: TextErrorCode) -> JSONResponse:
    return JSONResponse(status_code=kind.status, content={"detail": kind.detail, "code": kind.code})


class PrivateTextRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def safe(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as error:
                consent_error = any(len(item["loc"]) >= 2 and item["loc"][1] == "consent" for item in error.errors())
                return error_response(TextErrorCode.CONSENT_REQUIRED if consent_error else TextErrorCode.INVALID_REQUEST)
            except TextError as error:
                return error_response(error.kind)

        return safe
