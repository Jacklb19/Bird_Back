"""One metered structured REST call per explicit action; raw content is never logged."""
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from ..errors import ErrorCode, raise_error
from ..language_model import API_KEY_HEADER, GENERATE_PATH_TEMPLATE, MAX_OUTPUT_TOKENS, TEMPERATURE
from ..llm_usage import UsageRepository
from ..settings import get_settings
from .contracts import TokenUsage
from .text_errors import TextError, TextErrorCode
from .tutor import token_usage


@dataclass(frozen=True, slots=True)
class StructuredCompletion:
    value: object
    usage: TokenUsage


class StructuredWriter(Protocol):
    @property
    def model(self) -> str: ...

    def ensure_configured(self) -> None: ...

    def write(self, instructions: str, facts: dict[str, Any], schema: dict[str, Any]) -> StructuredCompletion: ...


def parse_decision(content: bytes) -> object:
    try:
        body = json.loads(content)
        candidate = body["candidates"][0]
        if candidate.get("finishReason") != "STOP":
            raise ValueError("Unfinished decision")
        text = "".join(part.get("text", "") for part in candidate["content"]["parts"] if not part.get("thought"))
        return json.loads(text)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise TextError(TextErrorCode.MODEL_OUTPUT_INVALID) from None


class GeminiStructuredWriter:
    def __init__(self) -> None:
        settings = get_settings()
        self.model = settings.llm_model
        self.base = settings.llm_api_url
        self.key = settings.llm_api_key or ""
        self.timeout = settings.llm_timeout_seconds
        self.max_facts_bytes = settings.max_metadata_body_bytes

    def ensure_configured(self) -> None:
        if not self.key:
            raise_error(ErrorCode.LLM_NOT_CONFIGURED)

    def write(self, instructions: str, facts: dict[str, Any], schema: dict[str, Any]) -> StructuredCompletion:
        self.ensure_configured()
        encoded = json.dumps(facts, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > self.max_facts_bytes:
            raise_error(ErrorCode.PUBLIC_DATA_UNAVAILABLE)
        body = {
            "systemInstruction": {"parts": [{"text": instructions}]},
            "contents": [{"role": "user", "parts": [{"text": encoded}]}],
            "generationConfig": {"temperature": TEMPERATURE, "maxOutputTokens": MAX_OUTPUT_TOKENS,
                                 "responseMimeType": "application/json", "responseSchema": schema},
        }
        try:
            with httpx.Client(base_url=self.base, headers={API_KEY_HEADER: self.key}, timeout=self.timeout) as client:
                response = client.post(GENERATE_PATH_TEMPLATE.format(model=self.model), json=body)
        except httpx.HTTPError:
            raise_error(ErrorCode.LLM_UNAVAILABLE)
        if response.is_error:
            raise_error(ErrorCode.LLM_UNAVAILABLE)
        return StructuredCompletion(parse_decision(response.content), token_usage(response.content))


@contextmanager
def counted_generation(owner: UUID, current: datetime, usage: UsageRepository, writer: StructuredWriter) -> Iterator[None]:
    # Configuration is checked before taking a unit or asking any public source.
    writer.ensure_configured()
    settings = get_settings()
    usage.spend(owner, current, settings.llm_user_daily_quota, settings.llm_daily_quota)
    try:
        yield
    except (ValidationError, ValueError, OverflowError):
        usage.give_back(owner, current)
        raise TextError(TextErrorCode.MODEL_OUTPUT_INVALID) from None
    except Exception as error:
        # Keep the existing policy: a GBIF name that is not a bird species is an invalid request, not an outage.
        if not (isinstance(error, HTTPException) and error.detail == ErrorCode.SPECIES_UNKNOWN.message):
            usage.give_back(owner, current)
        raise
