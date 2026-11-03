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
LIST_REQUESTS = (
    "List connection requests. direction='incoming' (default): pending requests from people "
    "asking to see the user's profile, with who asked and through which assistant. "
    "direction='outgoing': the user's own pending or approved requests to others."
)
_CONFIRM = (
    "Approving needs owner_confirmed=true: first ask the owner, quote exactly what will change "
    "(the person and the tier; a call without the flag returns owner_confirmation_required with "
    "the exact change), and set owner_confirmed only after the owner clearly agrees in this "
    "conversation. Never infer consent."
)
RESPOND = (
    "Respond to a connection request addressed to the user. action='approve' with "
    "tier='connections' or 'trusted_circle' (Trusted Circle sees more sensitive facts), "
    "'decline', or 'block' (also works on an approved connection; the person can no longer "
    "find or request the user). " + _CONFIRM + " Decline and block need no owner_confirmed, "
    "but confirm the decision with the user first."
)
LIST_CONNECTIONS = (
    "List the people the user has approved to see their profile, with each person's tier "
    "('connections' or 'trusted_circle') and the request_id used to change or revoke it."
)
CHANGE_TIER = (
    "Change an approved connection's tier ('connections' or 'trusted_circle') by request_id. "
    "Takes effect on the person's next request. An upgrade to 'trusted_circle' needs "
    "owner_confirmed=true: first ask the owner, quote exactly what will change, and set "
    "owner_confirmed only after the owner clearly agrees in this conversation. A downgrade to "
    "'connections' needs no flag; confirm it with the user first."
)
REVOKE = (
    "Revoke an approved connection by request_id: the person loses access to the user's "
    "profile from their next request (they may ask again later). Confirm with the user first."
)
CANCEL = "Withdraw one of the user's own pending outgoing connection requests by request_id."


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

    async def list_connection_requests(
        direction: Literal["incoming", "outgoing"] = "incoming",
    ) -> dict[str, Any]:
        if direction == "outgoing":
            rows = await _call(settings, svc.list_outgoing)
        else:
            rows = await _call(settings, svc.list_incoming)
        return {"direction": direction, "requests": [r.model_dump(mode="json") for r in rows]}

    async def respond_to_connection_request(
        request_id: UUID,
        action: Literal["approve", "decline", "block"],
        tier: Literal["connections", "trusted_circle"] | None = None,
        owner_confirmed: OwnerConfirmed = False,
    ) -> svc.ResponseResult:
        if action == "approve":
            if tier is None:
                raise ToolError(svc.InvalidTier.message)
            return await _call(
                settings, svc.approve, request_id, tier=tier, owner_confirmed=owner_confirmed
            )
        if action == "decline":
            return await _call(settings, svc.decline, request_id)
        if action == "block":
            return await _call(settings, svc.block, request_id)
        raise ToolError("action must be 'approve', 'decline' or 'block'")

    async def list_connections() -> dict[str, Any]:
        rows = await _call(settings, svc.list_connections)
        return {"connections": [r.model_dump(mode="json") for r in rows]}

    async def change_connection_tier(
        request_id: UUID,
        tier: Literal["connections", "trusted_circle"],
        owner_confirmed: OwnerConfirmed = False,
    ) -> svc.ResponseResult:
        return await _call(
            settings, svc.change_tier, request_id, tier=tier, owner_confirmed=owner_confirmed
        )

    async def revoke_connection(request_id: UUID) -> svc.ResponseResult:
        return await _call(settings, svc.revoke, request_id)

    async def cancel_connection_request(request_id: UUID) -> svc.ResponseResult:
        return await _call(settings, svc.cancel_request, request_id)

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
        (
            list_connection_requests,
            "list_connection_requests",
            "List connection requests",
            LIST_REQUESTS,
            True,
            False,
        ),
        (
            respond_to_connection_request,
            "respond_to_connection_request",
            "Respond to a connection request",
            RESPOND,
            False,
            True,
        ),
        (list_connections, "list_connections", "List connections", LIST_CONNECTIONS, True, False),
        (
            change_connection_tier,
            "change_connection_tier",
            "Change a connection's tier",
            CHANGE_TIER,
            False,
            True,
        ),
        (revoke_connection, "revoke_connection", "Revoke a connection", REVOKE, False, True),
        (
            cancel_connection_request,
            "cancel_connection_request",
            "Cancel my connection request",
            CANCEL,
            False,
            True,
        ),
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
