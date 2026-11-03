"""MCP tools for F5 profiles and contributions (RG-18). Registered by
``revel.mcp.server.create_mcp_server``.

Thin adapters over ``revel.services.profiles`` (same behavior as ``/api/v1/profile*``). Each
tool resolves the caller with ``principal_from_mcp_context`` first (rejects disconnected
delegations) and acts only on that user's own profile, except ``get_profile(handle)`` (RG-56,
#28), which reads another person's profile as the caller may see it: principal first, then
F6's ``resolve_handle``, then ``read_shared_profile`` with the resolved profile id (no tool
takes a profile id). Errors are ``ToolError`` with generic text (no SDK or internal detail),
except ``owner_confirmation_required`` (#29), whose text lists the exact widening changes so the
assistant can quote them to the owner.
Contract: ``docs/contracts/f5-profiles.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal
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
from revel.services import connections, profiles
from revel.services.categories import CATEGORY_BY_ID
from revel.services.profiles import (
    AddFactsResult,
    CategorySetting,
    FactInput,
    FactOut,
    PolicyResponse,
    PolicyUpdate,
    ProfileOut,
    SharedProfileOut,
)

logger = logging.getLogger(__name__)

POLICY_DESCRIPTION = (
    "Read the user's contribution policy and the fact categories: for each category its "
    "effective audience (the widest audience accepted without the user's review), allowed "
    "audiences, whether review is required and the details schema. Read-only. Call it before "
    "add_facts. Never collected: street addresses and birth years. Sensitive topics (health, "
    "religion, politics, orientation, finances) may be saved only when the user volunteers "
    "them, in the 'sensitive' category (Only Me, explicit evidence, held for the user's "
    "approval); never infer them or ask about them."
)
ADD_FACTS_DESCRIPTION = (
    "Save 1-20 short, actionable gifting facts about the user to their Revel profile "
    "(e.g. 'Owns a Moka pot', 'Prefers loose-leaf green tea'). Never raw conversation or "
    "quotes. Sensitive topics go only in the 'sensitive' category and only when the user "
    "volunteered them (evidence_type 'explicit'; Only Me; held for the user's approval); "
    "never infer them or ask about them. Set involves_others when a fact is about other "
    "people too. Each fact gets its own outcome: 'accepted' (status 'active', or "
    "'provisional' when inferred or low confidence, shared with a caveat), 'pending_review' "
    "(held for the user's approval, e.g. an audience wider than their policy allows) or "
    "'rejected' with a reason_code. To correct a fact, send the replacement with supersedes_id; "
    "the old fact is retired only when the replacement is accepted."
)
PROFILE_DESCRIPTION = (
    "Read the user's own profile facts (live and pending review) with their audience and "
    "status, the current contribution policy and a summary of recent contributions. Read-only."
)
CONFIRMATION_RULE = (
    "Anything that widens access needs owner_confirmed=true: first ask the owner, quote "
    "exactly what will change, and set owner_confirmed only after the owner clearly agrees in "
    "this conversation. Never infer consent (not from earlier messages, general enthusiasm or "
    "your own judgment). Without it the call fails with owner_confirmation_required and lists "
    "the exact changes to quote."
)
SET_POLICY_DESCRIPTION = (
    "Approve or change the user's contribution policy for the user, when they ask you to. "
    "Call get_contribution_policy first and pass its policy version as expected_version (null "
    "before the first approval); omitted fields keep their current values and "
    "category_settings, when sent, replaces the whole map. Widening (the first approval, "
    "public_opt_in on, a looser category audience, turning off require_review_all or a "
    "category's require_review, enrichment_enabled or share_provisional back on) needs the "
    "owner's confirmation; narrowing (tighter audiences, requiring review, withdrawing "
    "Public, turning provisional sharing or contributions off) does not. "
    + CONFIRMATION_RULE
    + " Returns the same shape as get_contribution_policy."
)
REVIEW_DESCRIPTION = (
    "Approve or reject one of the user's facts that is pending their review, when they decide. "
    "Approving makes it active and shared with its audience (a replacement retires the fact it "
    "corrects) and always needs the owner's confirmation; rejecting needs none. "
    + CONFIRMATION_RULE
)
AUDIENCE_DESCRIPTION = (
    "Change who can see one of the user's facts (public > connections > trusted_circle > "
    "only_me). Narrowing needs no confirmation; widening does. Sensitive facts are always "
    "only_me and Public needs the user's Public opt-in. " + CONFIRMATION_RULE
)
REMOVE_DESCRIPTION = (
    "Remove one of the user's facts (e.g. undo a wrong contribution or a fact the user asked "
    "to forget). It stops being shared immediately. Removing an already removed or replaced "
    "fact is an error (not idempotent)."
)

SHARED_PROFILE_DESCRIPTION = (
    "Read another Revel user's profile by their EXACT handle (no partial search): their "
    "display name, handle and avatar plus only the facts they share with the user (by "
    "connection tier, or Public facts if they opted in). Facts marked provisional are not yet "
    "confirmed by that person; mention the caveat. Read-only. If nothing is available under "
    "that handle the result is the same whether the handle does not exist, is hidden or "
    "shares nothing with the user; do not guess other handles. Rate limited."
)

_NOT_FOUND = "Fact not found"
_PROFILE_NOT_FOUND = "No Revel profile is available to you under that handle"
_RATE_LIMITED = "Too many lookups; try again later"
_NOT_ALLOWED = "Not allowed"
_UNAVAILABLE = "Authorization service unavailable"
_CONFLICT = "The fact changed; read the profile again and retry"
_POLICY_CONFLICT = (
    "The policy changed; read it again with get_contribution_policy and retry with its version"
)
_INVALID = "Invalid request"


async def _run[T](settings: Any, operation: Callable[[Session, Principal], Awaitable[T]]) -> T:
    principal = await principal_from_mcp_context(runtime_for(settings))
    try:
        async with profiles.session_scope() as session:
            return await operation(session, principal)
    except profiles.FactNotFound:
        raise ToolError(_NOT_FOUND) from None
    except profiles.OwnerConfirmationRequired as exc:
        raise ToolError(_confirmation_text(exc.changes)) from None
    except (profiles.NotAllowed, AuthorizationDenied):
        raise ToolError(_NOT_ALLOWED) from None
    except profiles.AuthorizationUnavailable:
        raise ToolError(_UNAVAILABLE) from None
    except profiles.PolicyVersionConflict:
        raise ToolError(_POLICY_CONFLICT) from None
    except profiles.FactConflict:
        raise ToolError(_CONFLICT) from None
    except profiles.PolicyInvalid as exc:
        raise ToolError(_invalid_text(exc.errors)) from None
    except profiles.InvalidRequest:
        raise ToolError(_INVALID) from None


def _confirmation_text(changes: list[str]) -> str:
    """``owner_confirmation_required: ...`` with every widening change, one per line."""
    listed = "\n".join(f"- {change}" for change in changes)
    return (
        "owner_confirmation_required: this widens access. Ask the owner, quoting exactly these "
        "changes, and call again with owner_confirmed=true only if they clearly agree:\n" + listed
    )


# Field-path parts that are schema names, never agent-supplied text.
_SCHEMA_FIELDS = frozenset(PolicyUpdate.model_fields) | frozenset(CategorySetting.model_fields)


def _path_part(part: Any) -> str:
    """A field path part, or ``*`` for anything that is not a schema field or a known
    category id (agent-supplied keys are never echoed)."""
    text = str(part)
    return text if text in _SCHEMA_FIELDS or text in CATEGORY_BY_ID else "*"


def _invalid_text(errors: list[dict[str, Any]]) -> str:
    """Policy field errors: generic field paths plus the service's fixed messages."""
    parts = [
        f"{'.'.join(_path_part(p) for p in e.get('loc', []))}: {e.get('msg', '')}" for e in errors
    ]
    return f"{_INVALID}: " + "; ".join(parts) if parts else _INVALID


async def _read_shared(settings: Any, handle: str) -> SharedProfileOut:
    principal = await principal_from_mcp_context(runtime_for(settings))
    try:
        async with profiles.session_scope() as session:
            target = await connections.resolve_handle(session, principal, handle=handle)
            return await profiles.read_shared_profile(
                session, principal, profile_id=target.profile_id
            )
    except (connections.TargetNotFound, profiles.SharedProfileNotFound):
        raise ToolError(_PROFILE_NOT_FOUND) from None
    except connections.RateLimited:
        raise ToolError(_RATE_LIMITED) from None
    except connections.NotAllowed:
        raise ToolError(_NOT_ALLOWED) from None
    except Exception as exc:
        logger.warning("get_profile failed (%s)", type(exc).__name__)
        raise ToolError(_UNAVAILABLE) from None


OwnerConfirmed = Annotated[bool, Field(description=profiles.OWNER_CONFIRMED_DESCRIPTION)]


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

    async def set_fact_audience(
        fact_id: UUID, audience: Audience, owner_confirmed: OwnerConfirmed = False
    ) -> FactOut:
        return await _run(
            settings,
            lambda s, p: profiles.set_fact_audience(
                s, p, fact_id, audience, owner_confirmed=owner_confirmed
            ),
        )

    async def set_contribution_policy(
        expected_version: Annotated[
            int | None,
            Field(description="The current policy version (null before the first approval)."),
        ],
        public_opt_in: bool | None = None,
        share_provisional: bool | None = None,
        require_review_all: bool | None = None,
        enrichment_enabled: bool | None = None,
        category_settings: Annotated[
            dict[str, CategorySetting] | None,
            Field(
                description="Replaces the whole map when sent: {category: {audience?, "
                "require_review?}}."
            ),
        ] = None,
        owner_confirmed: OwnerConfirmed = False,
    ) -> PolicyResponse:
        update = PolicyUpdate(
            expected_version=expected_version,
            public_opt_in=public_opt_in,
            share_provisional=share_provisional,
            require_review_all=require_review_all,
            enrichment_enabled=enrichment_enabled,
            category_settings=category_settings,
        )
        return await _run(
            settings,
            lambda s, p: profiles.set_policy(s, p, update, owner_confirmed=owner_confirmed),
        )

    async def review_pending_fact(
        fact_id: UUID,
        decision: Literal["approve", "reject"],
        owner_confirmed: OwnerConfirmed = False,
    ) -> FactOut:
        operation = profiles.approve_fact if decision == "approve" else profiles.reject_fact
        return await _run(
            settings,
            lambda s, p: operation(s, p, fact_id, owner_confirmed=owner_confirmed),
        )

    async def remove_fact(fact_id: UUID) -> FactOut:
        return await _run(settings, lambda s, p: profiles.remove_fact(s, p, fact_id))

    async def get_profile(
        handle: Annotated[str, Field(description="The person's exact Revel handle.")],
    ) -> SharedProfileOut:
        return await _read_shared(settings, handle)

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
            get_profile,
            "get_profile",
            "Someone's shared profile",
            SHARED_PROFILE_DESCRIPTION,
            read_only,
        ),
        (
            set_contribution_policy,
            "set_contribution_policy",
            "Change the contribution policy",
            SET_POLICY_DESCRIPTION,
            ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,  # a repeat with the old expected_version conflicts
                openWorldHint=False,
            ),
        ),
        (
            review_pending_fact,
            "review_pending_fact",
            "Approve or reject a pending fact",
            REVIEW_DESCRIPTION,
            # Rejecting removes the fact; a repeat is a conflict (ToolError).
            ToolAnnotations(
                readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
            ),
        ),
        (
            set_fact_audience,
            "set_fact_audience",
            "Change a fact's audience",
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
            # A repeat remove is a conflict (ToolError), so not idempotent.
            ToolAnnotations(
                readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
            ),
        ),
    ]
    for fn, name, title, description, hints in tools:
        annotations = hints.model_copy(update={"title": title})
        server.add_tool(
            fn, name=name, title=title, description=description, annotations=annotations
        )
