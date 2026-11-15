"""Structured private search and a public-context tutor; arbitrary user prose is not accepted."""
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends

from ..auth import authenticate
from ..llm_usage import UsageRepository
from .contracts import AssistantAnswer, AssistantQuestion, TutorAnswer, TutorInput
from .query import QueryRepository
from .tutor import GeminiTutorWriter, TutorWriter, tutor

router = APIRouter(prefix="/v1/assistant")


def queries() -> QueryRepository:
    return QueryRepository()


def tutor_writer() -> TutorWriter:
    return GeminiTutorWriter()


def tutor_usage() -> UsageRepository:
    return UsageRepository()


@router.post("/query", response_model=AssistantAnswer)
def query_journal(question: AssistantQuestion, owner: UUID = Depends(authenticate), repo: QueryRepository = Depends(queries)) -> AssistantAnswer:
    return repo.query(owner, question)


@router.post("/tutor", response_model=TutorAnswer)
def ask_tutor(question: TutorInput, owner: UUID = Depends(authenticate), usage: UsageRepository = Depends(tutor_usage),
              writer: TutorWriter = Depends(tutor_writer)) -> TutorAnswer:
    return tutor(owner, question, datetime.now(UTC), usage, writer)
