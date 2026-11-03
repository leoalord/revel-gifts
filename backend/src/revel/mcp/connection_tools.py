"""MCP connection tools (F6). Registered by ``revel.mcp.server.create_mcp_server``.

Thin adapters over ``revel.services.connections``: each tool resolves the caller with
``principal_from_mcp_context(runtime_for(settings))`` first, then calls the service. Errors are
mapped to fixed, generic ``ToolError`` messages (never SDK, database or OpenFGA text).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from revel.auth.deps import principal_from_mcp_context
from revel.auth.runtime import runtime_for
from revel.services import connections as svc

logger = logging.getLogger(__name__)


def _confirmation_text(changes: list[str]) -> str:
    """``owner_confirmation_required: ...`` with every grant, one per line (as F5's tools)."""
    listed = "\n".join(f"- {change}" for change in changes)
    return (
        "owner_confirmation_required: this widens access. Ask the owner, quoting exactly these "
        "changes, and call again with owner_confirmed=true only if they clearly agree:\n" + listed
    )


def _tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, svc.OwnerConfirmationRequired):
        return ToolError(_confirmation_text(exc.changes))
    if isinstance(exc, svc.ConnectionsError):
        return ToolError(exc.message)
    logger.error("connection tool failed (%s)", type(exc).__name__)
    return ToolError("Connections are temporarily unavailable; please retry")


async def _call(settings: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
    principal = await principal_from_mcp_context(runtime_for(settings))
    try:
        return await fn(principal, *args, **kwargs)
    except Exception as exc:
        raise _tool_error(exc) from None


# Tools that are not safe to repeat with the same arguments (a second approve fails).
NOT_IDEMPOTENT = frozenset({"respond_to_connection_request"})


def _annotations(
    title: str, *, read_only: bool, destructive: bool = False, idempotent: bool = True
) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive if not read_only else None,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


SET_HANDLE = (
    "Set the user's own Revel handle (3-30 lowercase letters, digits or underscores), whether "
    "people can find them by exact handle (discoverable, on by default) and an optional https "
    "avatar URL. Only for the connected user's own account; ask the user before changing "
    "their handle or discoverability. A taken handle returns 'unavailable'."
)
REQUEST_CONNECTION = (
    "Ask to connect with another Revel user by their EXACT handle (no partial search). Returns "
    "their minimal identity (display name, handle, avatar) and the pending request, or the "
    "existing request if one is already open. Approval is up to them and grants the user "
    "access to THEIR profile only; they need their own request to see the user's profile. "
    "If nobody is available under that handle the result is the same whether the handle does "
    "not exist or is hidden; do not guess other handles. Rate limited."
)
LIST_CONNECTIONS = (
    "List the user's connections and connection requests (read-only). include='all' (default) "
    "returns 'connections', 'incoming' and 'outgoing'; include='connections' returns only "
    "'connections': the people the user has approved to see their profile, with each person's "
    "tier ('connections' or 'trusted_circle') and the request_id used to change or end it. "
    "include='requests' returns only 'incoming' (pending requests from people asking to see the "
    "user's profile, with who asked and through which assistant) and 'outgoing' (the user's "
    "own pending or approved requests to others)."
)
_CONFIRM = (
    "approve, and change_tier to 'trusted_circle', need owner_confirmed=true: first ask the "
    "owner, quote exactly what will change (the person and the tier; a call without the flag "
    "returns owner_confirmation_required with the exact change), and set owner_confirmed only "
    "after the owner clearly agrees in this conversation. Never infer consent."
)
RESPOND = (
    "Respond to a connection request addressed to the user, or change an approved "
    "connection's tier, by request_id. action='approve' requires tier='connections' or "
    "'trusted_circle' (Trusted Circle sees more sensitive facts). action='change_tier' "
    "requires tier and changes an approved connection; it takes effect on the person's next "
    "request. action='decline'. action='block' also works on an approved connection; the "
    "person can no longer find or request the user. decline and block do not accept tier or "
    "owner_confirmed. " + _CONFIRM + " A downgrade to 'connections', decline and block need "
    "no owner_confirmed, but confirm the decision with the user first."
)
END_CONNECTION = (
    "End a connection by request_id. action='revoke': revoke an approved connection the user "
    "granted; the person loses access to the user's profile from their next request (they may "
    "ask again later). action='cancel': withdraw one of the user's own pending outgoing "
    "connection requests. Confirm with the user first."
)

# Per-action validation, with the same wording as the F5/F7 action tools (#31).
_RESPOND_ACTIONS = ("approve", "decline", "block", "change_tier")
_TIER_ACTIONS = frozenset({"approve", "change_tier"})
_END_ACTIONS = ("revoke", "cancel")
_INCLUDE = ("connections", "requests", "all")


def _unknown_action(tool: str, action: str, actions: tuple[str, ...]) -> ToolError:
    return ToolError(
        f"invalid_request: Unknown action '{action}' for {tool}; "
        f"expected one of: {', '.join(actions)}."
    )


def _requires(action: str, param: str) -> ToolError:
    return ToolError(f"invalid_request: Action '{action}' requires {param}.")


def _does_not_accept(action: str, param: str) -> ToolError:
    return ToolError(f"invalid_request: Action '{action}' does not accept {param}.")


OwnerConfirmed = Annotated[bool, Field(description=svc.OWNER_CONFIRMED_DESCRIPTION)]


def register(server: MCPServer, settings: Any) -> None:
    async def set_handle(
        handle: str | None = None,
        discoverable: bool | None = None,
        avatar_url: str | None = None,
    ) -> svc.HandleInfo:
        kwargs: dict[str, Any] = {"handle": handle, "discoverable": discoverable}
        if avatar_url is not None:
            kwargs["avatar_url"] = avatar_url
        return await _call(settings, svc.set_handle, **kwargs)

    async def request_connection(handle: str) -> svc.RequestResult:
        return await _call(settings, svc.lookup_and_request, handle=handle)

    async def list_connections(
        include: Annotated[
            str,
            Field(
                description="connections, requests (incoming and outgoing) or all.",
                json_schema_extra={"enum": list(_INCLUDE)},
            ),
        ] = "all",
    ) -> dict[str, Any]:
        if include not in _INCLUDE:
            raise ToolError(
                f"invalid_request: Unknown include '{include}' for list_connections; "
                f"expected one of: {', '.join(_INCLUDE)}."
            )
        result: dict[str, Any] = {}
        if include in ("connections", "all"):
            rows = await _call(settings, svc.list_connections)
            result["connections"] = [r.model_dump(mode="json") for r in rows]
        if include in ("requests", "all"):
            incoming = await _call(settings, svc.list_incoming)
            outgoing = await _call(settings, svc.list_outgoing)
            result["incoming"] = [r.model_dump(mode="json") for r in incoming]
            result["outgoing"] = [r.model_dump(mode="json") for r in outgoing]
        return result

    async def respond_to_connection_request(
        request_id: UUID,
        action: Annotated[
            str,
            Field(
                description="approve, decline, block or change_tier.",
                json_schema_extra={"enum": list(_RESPOND_ACTIONS)},
            ),
        ],
        tier: Annotated[
            Literal["connections", "trusted_circle"] | None,
            Field(description="Required for approve and change_tier; not accepted otherwise."),
        ] = None,
        owner_confirmed: OwnerConfirmed = False,
    ) -> svc.ResponseResult:
        if action not in _RESPOND_ACTIONS:
            raise _unknown_action("respond_to_connection_request", action, _RESPOND_ACTIONS)
        if action in _TIER_ACTIONS:
            if tier is None:
                raise _requires(action, "tier")
            fn = svc.approve if action == "approve" else svc.change_tier
            return await _call(settings, fn, request_id, tier=tier, owner_confirmed=owner_confirmed)
        if tier is not None:
            raise _does_not_accept(action, "tier")
        if owner_confirmed:
            raise _does_not_accept(action, "owner_confirmed")
        fn = svc.decline if action == "decline" else svc.block
        return await _call(settings, fn, request_id)

    async def end_connection(
        request_id: UUID,
        action: Annotated[
            str,
            Field(
                description="revoke (an approved connection the user granted) or cancel (the "
                "user's own pending outgoing request).",
                json_schema_extra={"enum": list(_END_ACTIONS)},
            ),
        ],
    ) -> svc.ResponseResult:
        if action not in _END_ACTIONS:
            raise _unknown_action("end_connection", action, _END_ACTIONS)
        fn = svc.revoke if action == "revoke" else svc.cancel_request
        return await _call(settings, fn, request_id)

    # (function, name, title, description, read_only, destructive)
    tools: tuple[tuple[Any, str, str, str, bool, bool], ...] = (
        (set_handle, "set_handle", "Set my handle", SET_HANDLE, False, False),
        (
            request_connection,
            "request_connection",
            "Request a connection",
            REQUEST_CONNECTION,
            False,
            False,
        ),
        (list_connections, "list_connections", "List connections", LIST_CONNECTIONS, True, False),
        (
            respond_to_connection_request,
            "respond_to_connection_request",
            "Respond to a connection request",
            RESPOND,
            False,
            True,
        ),
        (end_connection, "end_connection", "End a connection", END_CONNECTION, False, True),
    )
    for fn, name, title, description, read_only, destructive in tools:
        server.add_tool(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=_annotations(
                title,
                read_only=read_only,
                destructive=destructive,
                idempotent=name not in NOT_IDEMPOTENT,
            ),
        )
