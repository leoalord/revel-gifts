"""MCP identity tools (F2). Registered by ``revel.mcp.server.create_mcp_server``.

Pattern for every data-bearing MCP tool: resolve the caller with
``revel.auth.deps.principal_from_mcp_context(runtime_for(settings))`` before doing
anything else; the SDK's bearer middleware alone does not reject disconnected
delegations.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from revel.auth.deps import principal_from_mcp_context
from revel.auth.runtime import runtime_for
from revel.services.identity import IdentityNotFound, WhoAmI, whoami

WHOAMI_DESCRIPTION = (
    "Return the authenticated Revel user (id, email, display name) and the assistant acting "
    "for them (agent and delegation ids, OAuth client id). Read-only; takes no arguments. "
    "Use it to confirm which Revel account this assistant is connected to."
)


def register(server: MCPServer, settings: Any) -> None:
    async def whoami_tool() -> WhoAmI:
        runtime = runtime_for(settings)
        principal = await principal_from_mcp_context(runtime)
        try:
            return await whoami(principal, runtime.repository)
        except IdentityNotFound:
            raise ToolError("Unknown principal") from None

    server.add_tool(
        whoami_tool,
        name="whoami",
        title="Who am I",
        description=WHOAMI_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Who am I", readOnlyHint=True, idempotentHint=True, openWorldHint=False
        ),
    )
