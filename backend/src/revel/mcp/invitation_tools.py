"""Authenticated assistant invitation operations; redemption is explicitly browser-only."""

from typing import Any, Literal
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from revel.auth.deps import principal_from_mcp_context
from revel.auth.runtime import runtime_for
from revel.services import invitations as svc


def register(server: MCPServer, settings: Any) -> None:
    async def manage_invitations(
        action: Literal["create", "list", "send", "cancel"],
        email: str | None = None,
        invitation_id: UUID | None = None,
        channel: Literal["copy", "email"] = "copy",
        owner_confirmed: bool = False,
    ) -> Any:
        principal = await principal_from_mcp_context(runtime_for(settings))
        try:
            if action == "list":
                if (
                    email is not None
                    or invitation_id is not None
                    or owner_confirmed
                    or channel != "copy"
                ):
                    raise ToolError("invalid_request: list takes no invitation arguments")
                return await svc.list_sent(principal, settings=settings)
            if action == "create":
                if email is None or invitation_id is not None:
                    raise ToolError("invalid_request: create requires email and no invitation_id")
                return await svc.create(
                    principal,
                    email=email,
                    channel=channel,
                    owner_confirmed=owner_confirmed,
                    settings=settings,
                )
            if invitation_id is None or email is not None or channel != "copy":
                raise ToolError("invalid_request: send/cancel require invitation_id only")
            if action == "send":
                return await svc.send(
                    principal, invitation_id, owner_confirmed=owner_confirmed, settings=settings
                )
            if action == "cancel" and not owner_confirmed:
                return await svc.cancel(principal, invitation_id, settings=settings)
            raise ToolError("invalid_request: cancel does not take owner_confirmed")
        except svc.InvitationError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from None

    server.add_tool(
        manage_invitations,
        name="manage_invitations",
        title="Invite a friend",
        description=(
            "Create, list, send or cancel email-bound invitations. Ask the owner for the "
            "destination address. Prefer channel='copy' to return a link for them to share. "
            "Before channel='email' or action='send', show the exact destination and ask whether "
            "to send, then pass owner_confirmed=true after explicit owner consent. "
            "Never send unsolicited invitations. If email is unavailable, offer a copy link. "
            "Do not copy invitation tokens into prompts or logs. Recipient signs in through WorkOS "
            "with the verified invited email and accepts in their browser. Acceptance creates a "
            "pending directional connection request and grants no profile access. "
            "Duplicate creates return the existing pending invite. Sending is never automatically "
            "retried after an unknown delivery outcome. Confirm cancellation with the owner."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
        ),
    )
