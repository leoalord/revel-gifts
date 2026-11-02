"""HTTP question routes (F7): ``POST /api/questions`` and ``GET /api/updates``.

Thin adapters over ``revel.services.questions`` with the same behavior as the MCP tools.
Errors are ``{"detail": {"code": ..., "message": ...}}`` with the service's status code
(422 invalid_request / unsafe_requested_information, 403 not_permitted, 429 rate_limited with
``Retry-After``, 503 service_unavailable); request validation errors use FastAPI's 422.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from revel.auth.deps import CurrentPrincipal
from revel.services import questions
from revel.services.questions import (
    QuestionError,
    QuestionRateLimited,
    SubmitQuestionRequest,
    SubmitQuestionResponse,
    UpdatesResponse,
)

router = APIRouter(prefix="/api", tags=["questions"])


def _http_error(exc: QuestionError) -> HTTPException:
    headers = None
    if isinstance(exc, QuestionRateLimited):
        headers = {"Retry-After": str(exc.retry_after_seconds)}
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
        headers=headers,
    )


def _deps(request: Request) -> questions.QuestionDeps:
    return questions.deps_for(request.app.state.settings)


@router.post(
    "/questions",
    status_code=status.HTTP_201_CREATED,
    response_model=SubmitQuestionResponse,
)
async def submit_question(
    body: SubmitQuestionRequest, principal: CurrentPrincipal, request: Request
) -> SubmitQuestionResponse:
    try:
        return await questions.submit_question(principal, body, deps=_deps(request))
    except QuestionError as exc:
        raise _http_error(exc) from None


@router.get("/updates", response_model=UpdatesResponse)
async def get_updates(principal: CurrentPrincipal, request: Request) -> UpdatesResponse:
    try:
        return await questions.get_updates(principal, deps=_deps(request))
    except QuestionError as exc:
        raise _http_error(exc) from None
