"""Structured private search and a public-context tutor; arbitrary user prose is not accepted."""
from uuid import UUID

from fastapi import APIRouter, Depends

from ..auth import authenticate
from .contracts import AssistantAnswer, AssistantQuestion
from .query import QueryRepository

router = APIRouter(prefix="/v1/assistant")


def queries() -> QueryRepository:
    return QueryRepository()


@router.post("/query", response_model=AssistantAnswer)
def query_journal(question: AssistantQuestion, owner: UUID = Depends(authenticate), repo: QueryRepository = Depends(queries)) -> AssistantAnswer:
    return repo.query(owner, question)
