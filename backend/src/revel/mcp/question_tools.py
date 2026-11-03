"""MCP question tools (F7): ``submit_question``, ``get_updates``, the lease tools
(``claim_assignment``, ``renew_assignment``, ``release_assignment``, ``report_user_contact``,
``decline_assignment``), ``submit_answer`` and the requester's ``get_question_status`` /
``get_answer``.

Thin adapters over ``revel.services.questions``. Each resolves the caller first with
``principal_from_mcp_context(runtime_for(settings))`` (rejects disconnected delegations) and maps
service errors to a ``ToolError`` of the form ``"<code>: <message>"`` without object IDs, SDK
error text or private purpose values.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from revel.auth.deps import principal_from_mcp_context
from revel.auth.runtime import runtime_for
from revel.services import answers, question_leases, questions
from revel.services.answers import (
    EvidenceIn,
    GetAnswerResponse,
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


CLAIM_DESCRIPTION = (
    "Claim a question assignment from get_updates before working on it. Returns your lease "
    "(lease_version, lease_expires_at, about 15 minutes; renew it while you work). If you "
    "can't answer from what you already know, claim with human_contact=true and ask your user "
    "instead of declining. Only one assistant per question may contact the user, and Revel "
    "refuses if the user was contacted about it recently (by any assistant, you included). "
    "prior_user_contacts says how often the user was already asked about this question."
)
RENEW_DESCRIPTION = (
    "Extend your lease on a claimed assignment. Quote the current lease_version; a stale or "
    "expired lease is refused (claim again if the assignment is available). Renewals per "
    "claim are limited."
)
RELEASE_DESCRIPTION = (
    "Give up your lease so the assignment can be claimed again (by you later, or another "
    "assistant can take the human-contact lease). Quote the current lease_version."
)
REPORT_CONTACT_DESCRIPTION = (
    "Report that you asked your user this question (after claiming it). Revel marks the "
    "assignment waiting_for_user and holds your human-contact lease for the response window "
    "(up to 24 hours, never past the question deadline). delivery_confirmed=true only if you "
    "know the user received it. Then submit_answer once the user replies. Reporting again "
    "while waiting only updates delivery_confirmed: it does not extend the window and counts "
    "against the renewal limit. Do not message the user again within the cooldown."
)
DECLINE_DESCRIPTION = (
    "Decline an assignment you will not answer (reason: cannot_answer, insufficient_context, "
    "not_appropriate or other). Declining is final for you on this question, and the question "
    "closes once every assistant declines. If you can't answer from what you know, prefer "
    "claim_assignment with human_contact=true and ask your user. Quote lease_version if you "
    "hold a lease."
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
GET_QUESTION_STATUS_DESCRIPTION = (
    "Check the status of a question you asked (open, answered, expired, declined, ...)."
)
GET_ANSWER_DESCRIPTION = (
    "Retrieve the answer to a question you asked. Revel re-checks your current access and the "
    "facts the answer relies on every time: result 'answer' includes it, "
    "'no_shareable_answer' means it cannot be shared with you now, 'not_answered' means there "
    "is no answer yet."
)


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

    async def claim_assignment_tool(
        assignment_id: UUID,
        human_contact: Annotated[
            bool, Field(description="True only if you will ask your user directly.")
        ] = False,
        lease_version: Annotated[
            int | None, Field(description="Optional: the lease_version from get_updates.")
        ] = None,
    ) -> LeaseResponse:
        request = _model(ClaimRequest, human_contact=human_contact, lease_version=lease_version)
        return await _call(question_leases.claim_assignment, assignment_id, request)

    async def renew_assignment_tool(assignment_id: UUID, lease_version: int) -> LeaseResponse:
        request = _model(LeaseVersionRequest, lease_version=lease_version)
        return await _call(question_leases.renew_assignment, assignment_id, request)

    async def release_assignment_tool(assignment_id: UUID, lease_version: int) -> LeaseResponse:
        request = _model(LeaseVersionRequest, lease_version=lease_version)
        return await _call(question_leases.release_assignment, assignment_id, request)

    async def report_user_contact_tool(
        assignment_id: UUID,
        lease_version: int,
        delivery_confirmed: Annotated[
            bool, Field(description="True only if the user is known to have received it.")
        ],
    ) -> LeaseResponse:
        request = _model(
            UserContactRequest, lease_version=lease_version, delivery_confirmed=delivery_confirmed
        )
        return await _call(question_leases.report_user_contact, assignment_id, request)

    async def decline_assignment_tool(
        assignment_id: UUID,
        reason: DeclineReason = "cannot_answer",
        lease_version: int | None = None,
    ) -> LeaseResponse:
        request = _model(DeclineRequest, lease_version=lease_version, reason=reason)
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

    async def get_question_status_tool(question_id: UUID) -> QuestionStatusResponse:
        return await _call(answers.get_question_status, question_id)

    async def get_answer_tool(question_id: UUID) -> GetAnswerResponse:
        return await _call(answers.get_answer, question_id)

    tools = (
        (
            claim_assignment_tool,
            "claim_assignment",
            "Claim a question assignment",
            CLAIM_DESCRIPTION,
            False,
        ),
        (
            renew_assignment_tool,
            "renew_assignment",
            "Renew an assignment lease",
            RENEW_DESCRIPTION,
            False,
        ),
        (
            release_assignment_tool,
            "release_assignment",
            "Release an assignment lease",
            RELEASE_DESCRIPTION,
            False,
        ),
        (
            report_user_contact_tool,
            "report_user_contact",
            "Report asking your user",
            REPORT_CONTACT_DESCRIPTION,
            False,
        ),
        (
            decline_assignment_tool,
            "decline_assignment",
            "Decline a question assignment",
            DECLINE_DESCRIPTION,
            False,
        ),
        (
            submit_answer_tool,
            "submit_answer",
            "Answer a question",
            SUBMIT_ANSWER_DESCRIPTION,
            False,
        ),
        # May record lazy expiry of an overdue question.
        (
            get_question_status_tool,
            "get_question_status",
            "Check a question's status",
            GET_QUESTION_STATUS_DESCRIPTION,
            True,
        ),
        # Records an answer_retrieved event; never changes question or lease state.
        (
            get_answer_tool,
            "get_answer",
            "Get the answer to your question",
            GET_ANSWER_DESCRIPTION,
            True,
        ),
    )
    for fn, name, title, description, idempotent in tools:
        server.add_tool(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=_annotations(title, idempotent=idempotent),
        )
