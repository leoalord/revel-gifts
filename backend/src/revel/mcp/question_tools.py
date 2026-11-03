"""MCP question tools (F7): ``submit_question`` and ``get_updates``.

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
from revel.services import questions
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


def _tool_error(exc: QuestionError) -> ToolError:
    return ToolError(f"{exc.code}: {exc.message}")


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
            # Field locations and messages only; never the submitted values.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'request'}: {err['msg']}"
                for err in exc.errors(include_input=False, include_url=False)
            )
            raise ToolError(f"invalid_request: {problems}") from None
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
