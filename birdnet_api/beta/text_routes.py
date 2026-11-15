"""Additive text endpoints with route-local private validation responses."""
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends

from ..auth import authenticate
from ..llm_usage import UsageRepository
from .free_tutor import answer_question
from .interpretation import interpret
from .text_contracts import InterpretationResponse, InterpretInput, TextPolicy, TutorQuestionInput, TutorQuestionResponse
from .text_errors import PrivateTextRoute
from .text_model import GeminiStructuredWriter, StructuredWriter

router = APIRouter(prefix="/v1/assistant", route_class=PrivateTextRoute)


def text_writer() -> StructuredWriter:
    return GeminiStructuredWriter()


def text_usage() -> UsageRepository:
    return UsageRepository()


def current_time() -> datetime:
    return datetime.now(UTC)


@router.get("/text-policy", response_model=TextPolicy)
def text_policy(owner: UUID = Depends(authenticate)) -> TextPolicy:
    return TextPolicy()


@router.post("/interpret", response_model=InterpretationResponse)
def interpret_text(request: InterpretInput, owner: UUID = Depends(authenticate), usage: UsageRepository = Depends(text_usage),
                   writer: StructuredWriter = Depends(text_writer), current: datetime = Depends(current_time)) -> InterpretationResponse:
    return interpret(owner, request, current, usage, writer)


@router.post("/tutor-question", response_model=TutorQuestionResponse)
def tutor_question(request: TutorQuestionInput, owner: UUID = Depends(authenticate), usage: UsageRepository = Depends(text_usage),
                   writer: StructuredWriter = Depends(text_writer), current: datetime = Depends(current_time)) -> TutorQuestionResponse:
    return answer_question(owner, request, current, usage, writer)
