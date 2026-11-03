"""MCP question tools (F7): ``submit_question``, ``get_updates``, ``update_assignment`` (the
lease actions claim, renew, release, decline and report_user_contact), ``submit_answer`` and the
requester's ``get_question`` (status plus the answer when available).

Thin adapters over ``revel.services.questions``. Each resolves the caller first with
``principal_from_mcp_context(runtime_for(settings))`` (rejects disconnected delegations) and maps
service errors to a ``ToolError`` of the form ``"<code>: <message>"`` without object IDs, SDK
error text or private purpose values.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from revel.auth.deps import principal_from_mcp_context
from revel.auth.runtime import runtime_for
from revel.services import answers, question_leases, questions
from revel.services.answers import (
    AnswerOut,
    EvidenceIn,
    QuestionStatusResponse,
    SubmitAnswerRequest,
    SubmitAnswerResponse,
)
from revel.services.question_leases import (
    ClaimRequest,
    DeclineReason,
    DeclineRequest,
    LeaseResponse,
    LeaseVersionRequest,
    UserContactRequest,
)
from revel.services.question_routing import PrivatePurpose
from revel.services.questions import (
    EscalationContext,
    QuestionError,
    SubmitQuestionRequest,
    SubmitQuestionResponse,
    UpdatesResponse,
)

SUBMIT_QUESTION_DESCRIPTION = (
    "Ask a question about a person you are connected with on Revel (by their exact handle, or "
    "profile_id). Put ONLY what you want to know in requested_information (e.g. 'What flowers "
    "does she like?'). Put the occasion, budget, candidate gift ideas and any other planning "
    "context in private_purpose: Revel uses it to route and minimize the question and never "
    "shows it to the person or their assistants. Revel returns the profile facts you may "
    "already see, then routes a purpose-minimized question to the person's own assistants; "
    "their assistants see who is asking. Optional deadline: expires_at (timezone-aware) or "
    "expires_in_hours (default 72h, 1h to 14 days; out-of-range values are clamped). "
    "escalation_permitted/escalation_context are stored for later but not used yet."
)
GET_UPDATES_DESCRIPTION = (
    "Return this assistant's Revel inbox: typed items (type 'question_assignment' for now) "
    "with the purpose-minimized question, who is asking and through which assistant, the "
    "person it is about, the deadline and lease state. Call it at the start of a conversation "
    "or when the user asks about Revel; Revel cannot wake you up."
)


UPDATE_ASSIGNMENT_DESCRIPTION = (
    "Work on a question assignment from get_updates. Always pass assignment_id and action; "
    "each action takes only its own parameters (others are refused with invalid_request) and "
    "returns your lease (assignment_id, status, lease_version, lease_expires_at, "
    "human_contact, renewals_remaining). Actions: "
    "claim [optional human_contact (bool, default false), optional lease_version]: claim "
    "before working on it; the lease lasts about 15 minutes. If you can't answer from what you "
    "know, ask your user instead of declining: claim with human_contact=true first (or "
    "upgrade your current claim by claiming again with human_contact=true and your "
    "lease_version), then ask, then action report_user_contact. Only one assistant per "
    "question may contact the user, and Revel refuses if the user was contacted about it "
    "recently (by any assistant, you included). "
    "renew [required lease_version]: extend your lease; a stale or expired lease is refused "
    "(claim again if the assignment is available) and renewals per claim are limited. "
    "release [required lease_version]: give up your lease so the assignment can be claimed "
    "again (by you later, or another assistant can take the human-contact lease). "
    "decline [optional reason: cannot_answer (default), insufficient_context, "
    "not_appropriate or other; optional lease_version, required if you hold a lease]: you "
    "will not answer; this is final for you on this question, and the question closes once "
    "every assistant declines. Prefer claim with human_contact=true and asking your user. "
    "report_user_contact [required lease_version, required delivery_confirmed (bool, true "
    "only if you know the user received it)]: report that you asked your user; requires a "
    "human-contact claim (else human_contact_required). Revel marks the assignment "
    "waiting_for_user and holds your human-contact lease for the response window (up to 24 "
    "hours, never past the question deadline). Then submit_answer once the user replies. "
    "Reporting again while waiting only updates delivery_confirmed: it does not extend the "
    "window and counts against the renewal limit. Do not message the user again within the "
    "cooldown."
)
SUBMIT_ANSWER_DESCRIPTION = (
    "Answer a claimed assignment. Quote your lease_version. body: the answer only, in a short "
    "sentence or two. evidence (at least one item; cite minimally, only what the answer relies "
    "on): either {claim_id} of a profile fact you cite, or {category, proposed_audience, "
    "evidence_type (explicit/observed/inferred), observed_month (YYYY-MM, optional)} for "
    "something not on the profile. No quotes, sources or extra personal details. "
    "proposed_audience may not be more open than the owner's policy for the category. To keep "
    "a new durable fact on the profile, add it with add_facts first and cite its claim_id. "
    "Late answers (past the question deadline) are refused."
)
GET_QUESTION_DESCRIPTION = (
    "Check a question you asked and retrieve its answer when there is one. Returns the status "
    "(open, answered, expired, declined, ...) with expires_at, created_at, closed_at and "
    "answer_available, plus result: 'answer' (the answer is included), 'no_shareable_answer' "
    "(it cannot be shared with you now) or 'not_answered' (no answer yet). Revel re-checks "
    "your current access and the facts the answer relies on every time."
)


class GetQuestionResponse(QuestionStatusResponse):
    """``get_question_status`` fields plus the ``get_answer`` outcome, both unchanged."""

    result: Literal["answer", "no_shareable_answer", "not_answered"]
    answer: AnswerOut | None = None


# Shared helpers for per-action validation (same wording in the F5/F6 action tools). Codes use
# the existing ``invalid_request``; parameters are checked in signature order.
def _unknown_action(tool: str, action: str, actions: Any) -> ToolError:
    return ToolError(
        f"invalid_request: Unknown action '{action}' for {tool}; "
        f"expected one of: {', '.join(actions)}."
    )


def _check_action_params(
    action: str,
    given: dict[str, Any],
    required: frozenset[str],
    allowed: frozenset[str],
    signature_order: tuple[str, ...],
) -> None:
    """Raise for the first parameter (signature order) that is missing or not accepted."""
    for name in signature_order:
        if name in required and given.get(name) is None:
            raise ToolError(f"invalid_request: Action '{action}' requires {name}.")
        if given.get(name) is not None and name not in allowed:
            raise ToolError(f"invalid_request: Action '{action}' does not accept {name}.")


# action -> (required, allowed) parameters beyond assignment_id and action.
_ACTION_PARAMS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "claim": (frozenset(), frozenset({"human_contact", "lease_version"})),
    "renew": (frozenset({"lease_version"}), frozenset({"lease_version"})),
    "release": (frozenset({"lease_version"}), frozenset({"lease_version"})),
    "decline": (frozenset(), frozenset({"lease_version", "reason"})),
    "report_user_contact": (
        frozenset({"lease_version", "delivery_confirmed"}),
        frozenset({"lease_version", "delivery_confirmed"}),
    ),
}
_ACTION_SIGNATURE = ("lease_version", "human_contact", "delivery_confirmed", "reason")


def _tool_error(exc: QuestionError) -> ToolError:
    return ToolError(f"{exc.code}: {exc.message}")


def _invalid(exc: ValidationError) -> ToolError:
    # Field locations and messages only; never the submitted values.
    problems = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'request'}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    )
    return ToolError(f"invalid_request: {problems}")


def _annotations(title: str, *, idempotent: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


def register(server: MCPServer, settings: Any) -> None:
    async def submit_question_tool(
        requested_information: Annotated[
            str, Field(description="Only what you want to know; no purpose details.")
        ],
        handle: Annotated[str | None, Field(description="Target's exact handle.")] = None,
        profile_id: Annotated[UUID | None, Field(description="Target profile id.")] = None,
        private_purpose: Annotated[
            PrivatePurpose | None,
            Field(
                description="Restricted: occasion, occasion_date, budget, candidate_ideas, notes"
            ),
        ] = None,
        expires_at: datetime | None = None,
        expires_in_hours: float | None = None,
        escalation_permitted: bool = False,
        escalation_context: EscalationContext | None = None,
    ) -> SubmitQuestionResponse:
        runtime = runtime_for(settings)
        principal = await principal_from_mcp_context(runtime)
        try:
            request = SubmitQuestionRequest(
                handle=handle,
                profile_id=profile_id,
                requested_information=requested_information,
                private_purpose=private_purpose or PrivatePurpose(),
                expires_at=expires_at,
                expires_in_hours=expires_in_hours,
                escalation_permitted=escalation_permitted,
                escalation_context=escalation_context,
            )
        except ValidationError as exc:
            raise _invalid(exc) from None
        try:
            return await questions.submit_question(
                principal, request, deps=questions.deps_for(settings)
            )
        except QuestionError as exc:
            raise _tool_error(exc) from None

    async def get_updates_tool() -> UpdatesResponse:
        runtime = runtime_for(settings)
        principal = await principal_from_mcp_context(runtime)
        try:
            return await questions.get_updates(principal, deps=questions.deps_for(settings))
        except QuestionError as exc:
            raise _tool_error(exc) from None

    server.add_tool(
        submit_question_tool,
        name="submit_question",
        title="Ask a question about a connection",
        description=SUBMIT_QUESTION_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Ask a question about a connection",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    server.add_tool(
        get_updates_tool,
        name="get_updates",
        title="Get Revel updates",
        description=GET_UPDATES_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Get Revel updates",
            # Records a "retrieved" event and may mark expired questions; not read-only.
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )

    async def _call(op: Any, *args: Any) -> Any:
        principal = await principal_from_mcp_context(runtime_for(settings))
        try:
            return await op(principal, *args, deps=questions.deps_for(settings))
        except QuestionError as exc:
            raise _tool_error(exc) from None

    def _model(cls: Any, **values: Any) -> Any:
        try:
            return cls(**values)
        except ValidationError as exc:
            raise _invalid(exc) from None

    async def update_assignment_tool(
        assignment_id: UUID,
        action: Annotated[
            str,
            Field(
                description="claim, renew, release, decline or report_user_contact.",
                json_schema_extra={"enum": list(_ACTION_PARAMS)},
            ),
        ],
        lease_version: Annotated[
            int | None,
            Field(description="Required for renew, release and report_user_contact."),
        ] = None,
        human_contact: Annotated[
            bool | None, Field(description="claim only: true if you will ask your user.")
        ] = None,
        delivery_confirmed: Annotated[
            bool | None,
            Field(description="report_user_contact only (required): true if user received it."),
        ] = None,
        reason: Annotated[
            DeclineReason | None, Field(description="decline only; default cannot_answer.")
        ] = None,
    ) -> LeaseResponse:
        if action not in _ACTION_PARAMS:
            raise _unknown_action("update_assignment", action, _ACTION_PARAMS)
        required, allowed = _ACTION_PARAMS[action]
        _check_action_params(
            action,
            {
                "lease_version": lease_version,
                "human_contact": human_contact,
                "delivery_confirmed": delivery_confirmed,
                "reason": reason,
            },
            required,
            allowed,
            _ACTION_SIGNATURE,
        )
        if action == "claim":
            request = _model(
                ClaimRequest, human_contact=bool(human_contact), lease_version=lease_version
            )
            return await _call(question_leases.claim_assignment, assignment_id, request)
        if action in ("renew", "release"):
            request = _model(LeaseVersionRequest, lease_version=lease_version)
            op = (
                question_leases.renew_assignment
                if action == "renew"
                else question_leases.release_assignment
            )
            return await _call(op, assignment_id, request)
        if action == "report_user_contact":
            request = _model(
                UserContactRequest,
                lease_version=lease_version,
                delivery_confirmed=delivery_confirmed,
            )
            return await _call(question_leases.report_user_contact, assignment_id, request)
        request = _model(
            DeclineRequest, lease_version=lease_version, reason=reason or "cannot_answer"
        )
        return await _call(question_leases.decline_assignment, assignment_id, request)

    async def submit_answer_tool(
        assignment_id: UUID,
        lease_version: int,
        body: Annotated[str, Field(description="The answer only; no quotes or sources.")],
        evidence: Annotated[
            list[EvidenceIn],
            Field(description="At least one: {claim_id} or inline category/audience labels."),
        ],
    ) -> SubmitAnswerResponse:
        request = _model(
            SubmitAnswerRequest, lease_version=lease_version, body=body, evidence=evidence
        )
        return await _call(answers.submit_answer, assignment_id, request)

    async def get_question_tool(question_id: UUID) -> GetQuestionResponse:
        status = await _call(answers.get_question_status, question_id)
        # get_answer keeps its re-check and no_shareable_answer semantics; it records an
        # answer_retrieved event only when it releases an answer.
        outcome = await _call(answers.get_answer, question_id)
        return GetQuestionResponse(
            **status.model_dump(), result=outcome.result, answer=outcome.answer
        )

    # Not read-only (it changes lease state). Not destructive in MCP's sense: decline and
    # release only end the caller's OWN work item on one assignment; they delete no data, the
    # assignment stays recorded, and release lets it be claimed again. Nothing owned by
    # another party is changed.
    server.add_tool(
        update_assignment_tool,
        name="update_assignment",
        title="Update a question assignment",
        description=UPDATE_ASSIGNMENT_DESCRIPTION,
        annotations=_annotations("Update a question assignment"),
    )
    server.add_tool(
        submit_answer_tool,
        name="submit_answer",
        title="Answer a question",
        description=SUBMIT_ANSWER_DESCRIPTION,
        annotations=_annotations("Answer a question"),
    )
    # Not read-only: may record lazy expiry of an overdue question and an answer_retrieved
    # event. Never changes lease state.
    server.add_tool(
        get_question_tool,
        name="get_question",
        title="Check a question and get its answer",
        description=GET_QUESTION_DESCRIPTION,
        annotations=_annotations("Check a question and get its answer", idempotent=True),
    )
