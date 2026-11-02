"""MCP tools for F5 profiles and contributions (RG-18). Registered by
``revel.mcp.server.create_mcp_server``.

Thin adapters over ``revel.services.profiles`` (same behavior as ``/api/v1/profile*``). Each
tool resolves the caller with ``principal_from_mcp_context`` first (rejects disconnected
delegations) and acts only on that user's own profile. Errors are ``ToolError`` with generic
text (no SDK or internal detail). Contract: ``docs/contracts/f5-profiles.md``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy.orm import Session

from revel.auth.authz import AuthorizationDenied
from revel.auth.deps import principal_from_mcp_context
from revel.auth.identity import Principal
from revel.auth.runtime import runtime_for
from revel.db.enums import Audience
from revel.services import profiles
from revel.services.profiles import AddFactsResult, FactInput, FactOut, PolicyResponse, ProfileOut

POLICY_DESCRIPTION = (
    "Read the user's contribution policy and the fact categories: for each category its "
    "effective audience (the widest audience accepted without the user's review), allowed "
    "audiences, whether review is required and the details schema. Read-only. Call it before "
    "add_facts. Never collected: street addresses, birth years, health, religion, politics, "
    "orientation and finances."
)
ADD_FACTS_DESCRIPTION = (
    "Save 1-20 short, actionable gifting facts about the user to their Revel profile "
    "(e.g. 'Owns a Moka pot', 'Prefers loose-leaf green tea'). Never raw conversation, quotes "
    "or sensitive information. Each fact gets its own outcome: 'accepted' (status 'active', or "
    "'provisional' when inferred or low confidence, shared with a caveat), 'pending_review' "
    "(held for the user's approval, e.g. an audience wider than their policy allows) or "
    "'rejected' with a reason_code. To correct a fact, send the replacement with supersedes_id; "
    "the old fact is retired only when the replacement is accepted."
)
PROFILE_DESCRIPTION = (
    "Read the user's own profile facts (live and pending review) with their audience and "
    "status, the current contribution policy and a summary of recent contributions. Read-only."
)
AUDIENCE_DESCRIPTION = (
    "Narrow who can see one of the user's facts (public > connections > trusted_circle > "
    "only_me). Assistants can only narrow; widening is done by the user in Revel."
)
REMOVE_DESCRIPTION = (
    "Remove one of the user's facts (e.g. undo a wrong contribution or a fact the user asked "
    "to forget). It stops being shared immediately."
)

_NOT_FOUND = "Fact not found"
_NOT_ALLOWED = "Not allowed"
_UNAVAILABLE = "Authorization service unavailable"
_CONFLICT = "The fact changed; read the profile again and retry"
_INVALID = "Invalid request"


async def _run[T](settings: Any, operation: Callable[[Session, Principal], Awaitable[T]]) -> T:
    principal = await principal_from_mcp_context(runtime_for(settings))
    try:
        async with profiles.session_scope() as session:
            return await operation(session, principal)
    except profiles.FactNotFound:
        raise ToolError(_NOT_FOUND) from None
    except (profiles.NotAllowed, AuthorizationDenied):
        raise ToolError(_NOT_ALLOWED) from None
    except profiles.AuthorizationUnavailable:
        raise ToolError(_UNAVAILABLE) from None
    except (profiles.FactConflict, profiles.PolicyVersionConflict):
        raise ToolError(_CONFLICT) from None
    except (profiles.InvalidRequest, profiles.PolicyInvalid):
        raise ToolError(_INVALID) from None


def register(server: MCPServer, settings: Any) -> None:
    async def get_contribution_policy() -> PolicyResponse:
        return await _run(settings, profiles.get_policy)

    async def add_facts(
        facts: Annotated[
            list[FactInput],
            Field(min_length=1, max_length=profiles.MAX_FACTS_PER_BATCH),
        ],
    ) -> AddFactsResult:
        return await _run(settings, lambda s, p: profiles.add_facts(s, p, facts))

    async def get_my_profile() -> ProfileOut:
        return await _run(settings, profiles.get_my_profile)

    async def set_fact_audience(fact_id: UUID, audience: Audience) -> FactOut:
        return await _run(
            settings, lambda s, p: profiles.set_fact_audience(s, p, fact_id, audience)
        )

    async def remove_fact(fact_id: UUID) -> FactOut:
        return await _run(settings, lambda s, p: profiles.remove_fact(s, p, fact_id))

    read_only = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    tools: list[tuple[Callable[..., Any], str, str, str, ToolAnnotations]] = [
        (
            get_contribution_policy,
            "get_contribution_policy",
            "Contribution policy",
            POLICY_DESCRIPTION,
            read_only,
        ),
        (
            add_facts,
            "add_facts",
            "Add profile facts",
            ADD_FACTS_DESCRIPTION,
            ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=False,
            ),
        ),
        (get_my_profile, "get_my_profile", "My profile", PROFILE_DESCRIPTION, read_only),
        (
            set_fact_audience,
            "set_fact_audience",
            "Narrow a fact's audience",
            AUDIENCE_DESCRIPTION,
            ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
        ),
        (
            remove_fact,
            "remove_fact",
            "Remove a fact",
            REMOVE_DESCRIPTION,
            ToolAnnotations(
                readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
            ),
        ),
    ]
    for fn, name, title, description, hints in tools:
        annotations = hints.model_copy(update={"title": title})
        server.add_tool(
            fn, name=name, title=title, description=description, annotations=annotations
        )
