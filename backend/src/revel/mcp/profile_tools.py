"""MCP tools for F5 profiles and contributions (RG-18; consolidated in RG-59, #31). Registered
by ``revel.mcp.server.create_mcp_server``.

Thin adapters over ``revel.services.profiles`` (same behavior as ``/api/v1/profile*``). Each
tool resolves the caller with ``principal_from_mcp_context`` first (rejects disconnected
delegations) and acts only on that user's own profile, except ``get_profile(handle)`` (RG-56,
#28), which reads another person's profile as the caller may see it: principal first, then
F6's ``resolve_handle``, then ``read_shared_profile`` with the resolved profile id (no tool
takes a profile id). ``get_profile()`` without a handle is the owner read (``get_my_profile``
plus ``get_policy``); owner and shared reads stay separate service and authorization paths.
Errors are ``ToolError`` with generic text (no SDK or internal detail), except
``owner_confirmation_required`` (#29), whose text lists the exact widening changes so the
assistant can quote them to the owner, and ``invalid_request`` per-action validation errors
(RG-59), worded like the other consolidated adapters.
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
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel
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
    SharedProfileOut,
    SummaryOut,
)

logger = logging.getLogger(__name__)

POLICY_GUIDANCE = (
    "The policy lists the fact categories: for each its effective audience (the policy's "
    "audience limit for routine contributions), allowed audiences, whether review is "
    "required and the details schema. An audience alone does not imply approval or sharing. "
    "Read the policy before add_facts. Never collected: street "
    "addresses and birth years. Sensitive topics (health, religion, politics, orientation, "
    "finances) may be saved only when the user volunteers them, in the 'sensitive' category "
    "(Only Me, explicit evidence, held for the user's approval); never infer them or ask about "
    "them."
)
GET_PROFILE_DESCRIPTION = (
    "Read a Revel profile. Read-only. Without handle: the user's own profile, i.e. their facts "
    "(live and pending review) with audience and status, a summary of recent contributions, "
    "and 'policy', their contribution policy (policy.policy is null before the first approval; "
    "policy.policy.version is the expected_version for set_contribution_policy). "
    + POLICY_GUIDANCE
    + " With handle: another person's profile by their EXACT handle (no partial search): "
    "display name, handle and avatar plus only the facts they share with the user (by "
    "connection tier, or Public facts if they opted in). Facts marked provisional are not yet "
    "confirmed by that person; mention the caveat. If nothing is available under that handle "
    "the result is the same whether the handle does not exist, is hidden or shares nothing "
    "with the user; do not guess other handles. Handle lookups are rate limited."
)
ADD_FACTS_DESCRIPTION = (
    "Save 1-20 short, actionable gifting facts about the user to their Revel profile "
    "(e.g. 'Owns a Moka pot', 'Prefers loose-leaf green tea'). Never raw conversation or "
    "quotes. Sensitive topics go only in the 'sensitive' category and only when the user "
    "volunteered them (evidence_type 'explicit'; Only Me; held for the user's approval); "
    "never infer them or ask about them. Set involves_others when a fact is about other "
    "people too. Each fact gets its own outcome: 'accepted' (status 'active', or "
    "'provisional' when inferred or low confidence, shareable with a caveat only while the "
    "owner's current policy allows it), 'pending_review' "
    "(held for the user's approval, e.g. an audience wider than their policy allows) or "
    "'rejected' with a reason_code. To correct a fact, send the replacement with supersedes_id; "
    "the old fact is retired only when the replacement is accepted."
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
    "Call get_profile (no handle) first and pass policy.policy.version as expected_version "
    "(null before the first approval); omitted fields keep their current values and "
    "category_settings, when sent, replaces the whole map. Widening (the first approval, "
    "public_opt_in on, a looser category audience, turning off require_review_all or a "
    "category's require_review, enrichment_enabled or share_provisional back on) needs the "
    "owner's confirmation; narrowing (tighter audiences, requiring review, withdrawing "
    "Public, turning provisional sharing or contributions off) does not. "
    + CONFIRMATION_RULE
    + " Returns the policy in the same shape as get_profile's 'policy'."
)
UPDATE_FACT_DESCRIPTION = (
    "Change one of the user's facts. action='set_audience' (requires audience): change who "
    "can see it (public > connections > trusted_circle > only_me); narrowing needs no "
    "confirmation. Sensitive facts are always only_me and Public needs the user's Public opt-in. "
    "action='approve' (takes no audience): approve a fact pending the "
    "user's review, when they decide; it becomes active and shared with its audience (a "
    "replacement retires the fact it corrects) and always needs the owner's confirmation. To "
    "reject a pending fact use remove_fact with action='reject'. For approvals and widening, "
    "first call without confirmation for a preview. Retry with owner_confirmed=true and the "
    "preview's expected_updated_at only after the owner agrees. If Revel returns a new "
    "preview, ask again. " + CONFIRMATION_RULE
)
REMOVE_DESCRIPTION = (
    "Remove one of the user's live facts (action='remove'), or reject a fact pending their "
    "review (action='reject'). State mismatch is a conflict: reread and confirm the intended "
    "operation before retrying. Neither action requires confirmation. For legacy calls "
    "omitting action, owner_confirmed=true selects reject; otherwise the default is remove."
)
REJECT_CONFIRMED_DESCRIPTION = (
    "Reject only: set true only when the owner explicitly told you to reject this pending "
    "fact. If action is omitted, true selects reject."
)

_NOT_FOUND = "Fact not found"
_PROFILE_NOT_FOUND = "No Revel profile is available to you under that handle"
_RATE_LIMITED = "Too many lookups; try again later"
_NOT_ALLOWED = "Not allowed"
_UNAVAILABLE = "Authorization service unavailable"
_CONFLICT = "The fact changed; read the profile again and retry"
_POLICY_CONFLICT = "The policy changed; read it again with get_profile and retry with its version"
_INVALID = "Invalid request"
_INVALID_REQUEST = "invalid_request"  # error code shared with the F6/F7 adapters (RG-59)

FactAction = Literal["set_audience", "approve"]
FACT_ACTIONS: tuple[FactAction, ...] = ("set_audience", "approve")
AUDIENCES: tuple[str, ...] = tuple(a.value for a in Audience)  # widest first


async def _run[T](settings: Any, operation: Callable[[Session, Principal], Awaitable[T]]) -> T:
    principal = await principal_from_mcp_context(runtime_for(settings))
    try:
        async with profiles.session_scope() as session:
            return await operation(session, principal)
    except profiles.FactNotFound:
        raise ToolError(_NOT_FOUND) from None
    except profiles.OwnerConfirmationRequired as exc:
        raise ToolError(_confirmation_text(exc)) from None
    except (profiles.NotAllowed, AuthorizationDenied):
        raise ToolError(_NOT_ALLOWED) from None
    except profiles.AuthorizationUnavailable:
        raise ToolError(_UNAVAILABLE) from None
    except profiles.PolicyVersionConflict:
        raise ToolError(_POLICY_CONFLICT) from None
    except profiles.FactConflict as exc:
        raise ToolError(f"{exc.reason}: {_CONFLICT}") from None
    except profiles.PolicyInvalid as exc:
        raise ToolError(_invalid_text(exc.errors)) from None
    except profiles.InvalidRequest:
        raise ToolError(_INVALID) from None


def _confirmation_text(error: profiles.OwnerConfirmationRequired) -> str:
    """Show the proposed changes separately from any retry parameter."""
    listed = "\n".join(f"- {change}" for change in error.changes)
    expected = error.expected_updated_at
    retry = (
        f"\nFor this preview, pass expected_updated_at={expected.isoformat()}." if expected else ""
    )
    return (
        "owner_confirmation_required: ask the owner, quoting exactly these changes, and call "
        "again with owner_confirmed=true only if they clearly agree:\n" + listed + retry
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


def _invalid_request(message: str) -> ToolError:
    """A per-action validation error (``invalid_request: <message>``), raised before any
    service call."""
    return ToolError(f"{_INVALID_REQUEST}: {message}")


def _shown(value: str) -> str:
    """An agent-supplied action name echoed in an error, bounded."""
    return value if len(value) <= 40 else value[:40] + "..."


def _fact_action(action: str, audience: str | None) -> tuple[FactAction, Audience | None]:
    """Validate ``update_fact``'s action and its per-action parameters."""
    if action not in FACT_ACTIONS:
        raise _invalid_request(
            f"Unknown action '{_shown(action)}' for update_fact; expected one of: "
            + ", ".join(FACT_ACTIONS)
            + "."
        )
    if action == "approve":
        if audience is not None:
            raise _invalid_request("Action 'approve' does not accept audience.")
        return "approve", None
    if audience is None:
        raise _invalid_request("Action 'set_audience' requires audience.")
    if audience not in AUDIENCES:
        raise _invalid_request(
            "Action 'set_audience' requires audience to be one of: " + ", ".join(AUDIENCES) + "."
        )
    return "set_audience", Audience(audience)


class OwnerProfileView(BaseModel):
    """``get_profile()`` without a handle: ``get_my_profile``'s facts and summary plus the full
    ``get_policy`` response as ``policy`` (``policy.policy`` and ``policy.defaults`` are what
    ``get_my_profile`` returned as ``policy`` and ``defaults``)."""

    profile_id: UUID | None
    policy_version: int | None
    facts: list[FactOut]
    summary: SummaryOut
    policy: PolicyResponse


class ProfileView(RootModel[OwnerProfileView | SharedProfileOut]):
    """Both possible top-level object shapes; no additional result wrapper."""

    model_config = ConfigDict(json_schema_extra={"type": "object"})


async def _read_owner(settings: Any) -> OwnerProfileView:
    async def read(session: Session, principal: Principal) -> OwnerProfileView:
        profile = await profiles.get_my_profile(session, principal)
        policy = await profiles.get_policy(session, principal)
        return OwnerProfileView(
            profile_id=profile.profile_id,
            policy_version=profile.policy_version,
            facts=profile.facts,
            summary=profile.summary,
            policy=policy,
        )

    return await _run(settings, read)


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
    async def get_profile(
        handle: Annotated[
            str | None,
            Field(
                description="Another person's exact Revel handle. Omit to read the user's own "
                "profile and contribution policy."
            ),
        ] = None,
    ) -> ProfileView:
        if handle is None:
            return ProfileView(await _read_owner(settings))
        return ProfileView(await _read_shared(settings, handle))

    async def add_facts(
        facts: Annotated[
            list[FactInput],
            Field(min_length=1, max_length=profiles.MAX_FACTS_PER_BATCH),
        ],
    ) -> AddFactsResult:
        return await _run(settings, lambda s, p: profiles.add_facts(s, p, facts))

    async def update_fact(
        fact_id: UUID,
        # Plain str (with an enum in the schema) so an unknown value gets the shared
        # invalid_request wording instead of the SDK's validation error.
        action: Annotated[
            str,
            Field(
                description="'set_audience' (requires audience) or 'approve' (a pending fact; "
                "takes no audience).",
                json_schema_extra={"enum": list(FACT_ACTIONS)},
            ),
        ],
        audience: Annotated[
            str | None,
            Field(
                description="set_audience only: the new audience (public, connections, "
                "trusted_circle or only_me).",
                json_schema_extra={"enum": [*AUDIENCES, None]},
            ),
        ] = None,
        owner_confirmed: OwnerConfirmed = False,
        expected_updated_at: Annotated[
            AwareDatetime | None,
            Field(description="The exact fact revision supplied in the confirmation preview."),
        ] = None,
    ) -> FactOut:
        checked, new_audience = _fact_action(action, audience)
        if checked == "approve":
            return await _run(
                settings,
                lambda s, p: profiles.approve_fact(
                    s,
                    p,
                    fact_id,
                    owner_confirmed=owner_confirmed,
                    expected_updated_at=expected_updated_at,
                ),
            )
        assert new_audience is not None
        return await _run(
            settings,
            lambda s, p: profiles.set_fact_audience(
                s,
                p,
                fact_id,
                new_audience,
                owner_confirmed=owner_confirmed,
                expected_updated_at=expected_updated_at,
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

    async def remove_fact(
        fact_id: UUID,
        owner_confirmed: Annotated[bool, Field(description=REJECT_CONFIRMED_DESCRIPTION)] = False,
        action: Annotated[
            Literal["remove", "reject"] | None,
            Field(description="Remove a live fact or reject a pending fact."),
        ] = None,
    ) -> FactOut:
        if action == "reject" or (action is None and owner_confirmed):
            return await _run(
                settings,
                lambda s, p: profiles.reject_fact(s, p, fact_id, owner_confirmed=owner_confirmed),
            )
        if owner_confirmed:
            raise _invalid_request("Action 'remove' does not accept owner_confirmed=true.")
        return await _run(settings, lambda s, p: profiles.remove_fact(s, p, fact_id))

    read_only = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    tools: list[tuple[Callable[..., Any], str, str, str, ToolAnnotations]] = [
        (get_profile, "get_profile", "Read a profile", GET_PROFILE_DESCRIPTION, read_only),
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
            update_fact,
            "update_fact",
            "Change a fact's audience or approve it",
            UPDATE_FACT_DESCRIPTION,
            # A repeat approve is a conflict (ToolError), so not idempotent.
            ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
            ),
        ),
        (
            remove_fact,
            "remove_fact",
            "Remove or reject a fact",
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
