"""The language model that puts gathered facts into words (RF-14, RF-15; ADR-37).

It never hears audio nor identifies a species: it receives facts the API already gathered (public sources, or
deterministic statistics) and returns a few paragraphs. Gemini's free tier may use what it receives to improve
Google's products, so callers send only public data and aggregated figures: never a person's name, e-mail, site
name, coordinates or identifiers.
"""
import json
import logging
from typing import Any, Final, Protocol

import httpx

from .errors import ErrorCode, raise_error
from .settings import get_settings

API_KEY_HEADER: Final = "x-goog-api-key"
GENERATE_PATH_TEMPLATE: Final = "/models/{model}:generateContent"
# Low temperature: the texts restate facts, they are not meant to be creative.
TEMPERATURE: Final = 0.3
MAX_OUTPUT_TOKENS: Final = 1200
MIN_PARAGRAPHS: Final = 1
MAX_PARAGRAPHS: Final = 4
MAX_PARAGRAPH_LENGTH: Final = 900
# The answer is constrained to this shape, and checked again here: the schema does not bound lengths.
ANSWER_SCHEMA: Final = {
    "type": "object",
    "properties": {"paragraphs": {"type": "array", "items": {"type": "string"}}},
    "required": ["paragraphs"],
}
QUOTA_STATUS: Final = 429

logger = logging.getLogger(__name__)


class TextWriter(Protocol):
    """Anything that turns instructions and facts into paragraphs; tests replace the real model with a stub."""

    @property
    def model(self) -> str: ...

    def write(self, instructions: str, facts: dict[str, Any]) -> list[str]: ...


class GeminiWriter:
    """Gemini's REST API (`models.generateContent`) with a JSON answer of paragraphs."""

    def __init__(self) -> None:
        settings = get_settings()
        self.base = settings.llm_api_url
        self.key = settings.llm_api_key
        self.model = settings.llm_model
        self.timeout = settings.llm_timeout_seconds

    def write(self, instructions: str, facts: dict[str, Any]) -> list[str]:
        if not self.key:
            raise_error(ErrorCode.LLM_NOT_CONFIGURED)
        body = {
            "systemInstruction": {"parts": [{"text": instructions}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(facts, ensure_ascii=False, default=str)}]}],
            "generationConfig": {
                "temperature": TEMPERATURE, "maxOutputTokens": MAX_OUTPUT_TOKENS,
                "responseMimeType": "application/json", "responseSchema": ANSWER_SCHEMA,
            },
        }
        try:
            with httpx.Client(base_url=self.base, headers={API_KEY_HEADER: self.key}, timeout=self.timeout) as client:
                response = client.post(GENERATE_PATH_TEMPLATE.format(model=self.model), json=body)
        except httpx.HTTPError as error:
            logger.warning("Language model did not answer: %s", type(error).__name__)
            raise_error(ErrorCode.LLM_UNAVAILABLE)
        if response.status_code == QUOTA_STATUS:
            # The free tier's per-minute or per-day limit of the whole project: nothing to retry right now.
            logger.warning("Language model quota reached")
            raise_error(ErrorCode.LLM_UNAVAILABLE)
        if response.is_error:
            logger.warning("Language model answered %s", response.status_code)
            raise_error(ErrorCode.LLM_UNAVAILABLE)
        paragraphs = parse_answer(response.content)
        if paragraphs is None:
            logger.warning("Language model answer was not usable")
            raise_error(ErrorCode.LLM_UNAVAILABLE)
        return paragraphs


def parse_answer(content: bytes) -> list[str] | None:
    """Paragraphs of a generateContent response, or None when it is blocked, cut short or malformed."""
    try:
        body = json.loads(content)
        candidate = body["candidates"][0]
        # A text cut by the token limit or stopped by a safety filter is not a finished text.
        if candidate.get("finishReason") not in (None, "STOP"):
            return None
        # Parts flagged as thoughts are the model's reasoning, not its answer.
        text = "".join(part.get("text", "") for part in candidate["content"]["parts"] if not part.get("thought"))
        paragraphs = json.loads(text)["paragraphs"]
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
    if not isinstance(paragraphs, list) or not all(isinstance(p, str) for p in paragraphs):
        return None
    cleaned = [" ".join(p.split()) for p in paragraphs if p.strip()]
    if not MIN_PARAGRAPHS <= len(cleaned) <= MAX_PARAGRAPHS or any(len(p) > MAX_PARAGRAPH_LENGTH for p in cleaned):
        return None
    return cleaned
