"""HTTP question routes (F7), thin adapters with the same behavior as the MCP tools.

- ``POST /api/questions`` (submit), ``GET /api/updates`` (respondent inbox);
- ``POST /api/assignments/{id}/claim|renew|release|user-contact|decline`` (leases,
  ``revel.services.question_leases``) and ``POST /api/assignments/{id}/answer`` (201);
- ``GET /api/questions/{id}`` (requester status) and ``GET /api/questions/{id}/answer``
  (requester answer retrieval, ``revel.services.answers``).

Errors are ``{"detail": {"code": ..., "message": ...}}`` with the service's status code
(422 invalid_request / unsafe_requested_information / invalid_evidence, 403 not_permitted,
404 not_found, 409 stale_lease / lease_expired / lease_not_held / already_claimed /
renewal_limit / human_contact_taken / human_contact_required / outreach_cooldown (with
``Retry-After``) / question_expired / question_closed, 429 rate_limited with ``Retry-After``, 503
service_unavailable); request validation errors use FastAPI's 422.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from revel.auth.deps import CurrentPrincipal
from revel.services import answers, question_leases, questions
from revel.services.answers import (
    GetAnswerResponse,
    QuestionStatusResponse,
    SubmitAnswerRequest,
    SubmitAnswerResponse,
)
from revel.services.question_leases import (
    ClaimRequest,
    DeclineRequest,
    LeaseResponse,
    LeaseVersionRequest,
    OutreachCooldown,
    UserContactRequest,
)
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
    if isinstance(exc, QuestionRateLimited | OutreachCooldown):
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


# --- Leases -------------------------------------------------------------------------------------


@router.post("/assignments/{assignment_id}/claim", response_model=LeaseResponse)
async def claim_assignment(
    assignment_id: UUID,
    principal: CurrentPrincipal,
    request: Request,
    body: ClaimRequest | None = None,
) -> LeaseResponse:
    """Claim an assignment (the lease), or upgrade one's own claimed lease in place with
    human_contact=true and the current lease_version. To ask the user: claim with
    human_contact=true first (or upgrade), then ask, then report user contact."""
    try:
        return await question_leases.claim_assignment(
            principal, assignment_id, body or ClaimRequest(), deps=_deps(request)
        )
    except QuestionError as exc:
        raise _http_error(exc) from None


@router.post("/assignments/{assignment_id}/renew", response_model=LeaseResponse)
async def renew_assignment(
    assignment_id: UUID, body: LeaseVersionRequest, principal: CurrentPrincipal, request: Request
) -> LeaseResponse:
    try:
        return await question_leases.renew_assignment(
            principal, assignment_id, body, deps=_deps(request)
        )
    except QuestionError as exc:
        raise _http_error(exc) from None


@router.post("/assignments/{assignment_id}/release", response_model=LeaseResponse)
async def release_assignment(
    assignment_id: UUID, body: LeaseVersionRequest, principal: CurrentPrincipal, request: Request
) -> LeaseResponse:
    try:
        return await question_leases.release_assignment(
            principal, assignment_id, body, deps=_deps(request)
        )
    except QuestionError as exc:
        raise _http_error(exc) from None


@router.post("/assignments/{assignment_id}/user-contact", response_model=LeaseResponse)
async def report_user_contact(
    assignment_id: UUID, body: UserContactRequest, principal: CurrentPrincipal, request: Request
) -> LeaseResponse:
    """Record outreach to the user (waiting_for_user). Requires a human-contact claim
    (409 human_contact_required otherwise). A repeat report while waiting only
    updates delivery_confirmed: no new window, counts against the renewal limit."""
    try:
        return await question_leases.report_user_contact(
            principal, assignment_id, body, deps=_deps(request)
        )
    except QuestionError as exc:
        raise _http_error(exc) from None


@router.post("/assignments/{assignment_id}/decline", response_model=LeaseResponse)
async def decline_assignment(
    assignment_id: UUID,
    principal: CurrentPrincipal,
    request: Request,
    body: DeclineRequest | None = None,
) -> LeaseResponse:
    """Decline: final for this assistant; the question closes once every assistant declines."""
    try:
        return await question_leases.decline_assignment(
            principal, assignment_id, body or DeclineRequest(), deps=_deps(request)
        )
    except QuestionError as exc:
        raise _http_error(exc) from None


# --- Answers ------------------------------------------------------------------------------------


@router.post(
    "/assignments/{assignment_id}/answer",
    status_code=status.HTTP_201_CREATED,
    response_model=SubmitAnswerResponse,
)
async def submit_answer(
    assignment_id: UUID, body: SubmitAnswerRequest, principal: CurrentPrincipal, request: Request
) -> SubmitAnswerResponse:
    """Answer with minimal evidence (only what the answer relies on); completes the lease."""
    try:
        return await answers.submit_answer(principal, assignment_id, body, deps=_deps(request))
    except QuestionError as exc:
        raise _http_error(exc) from None


@router.get("/questions/{question_id}", response_model=QuestionStatusResponse)
async def get_question_status(
    question_id: UUID, principal: CurrentPrincipal, request: Request
) -> QuestionStatusResponse:
    try:
        return await answers.get_question_status(principal, question_id, deps=_deps(request))
    except QuestionError as exc:
        raise _http_error(exc) from None


@router.get("/questions/{question_id}/answer", response_model=GetAnswerResponse)
async def get_answer(
    question_id: UUID, principal: CurrentPrincipal, request: Request
) -> GetAnswerResponse:
    try:
        return await answers.get_answer(principal, question_id, deps=_deps(request))
    except QuestionError as exc:
        raise _http_error(exc) from None
