"""MCP server construction (official MCP Python SDK, high-level ``MCPServer``).

Tools here are thin adapters; shared behavior and enforcement belong in ``revel.services``.
Authentication for MCP operations is supplied by F2 through ``revel.auth.mcp_auth`` and must be
enforced explicitly: FastAPI route dependencies do not protect the MCP endpoint.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from revel import guidance
from revel.mcp import connection_tools, identity_tools, profile_tools
from revel.settings import Settings


def create_mcp_server(settings: Settings, **server_kwargs: Any) -> MCPServer:
    """Build the MCP server. ``server_kwargs`` (e.g. ``token_verifier``, ``auth``) go to
    ``MCPServer``."""
    server = MCPServer(name="revel", instructions=guidance.server_instructions(), **server_kwargs)

    @server.tool(description="Connectivity check. Returns 'pong'.")
    def ping() -> str:
        return "pong"

    identity_tools.register(server, settings)
    profile_tools.register(server, settings)
    connection_tools.register(server, settings)
    guidance.register_mcp(server)
    return server


def build_mcp_asgi_app(server: MCPServer, settings: Settings) -> Starlette:
    """Return the Streamable HTTP ASGI app serving exactly ``settings.normalized_mcp_path``.

    Stateless mode: each request gets a fresh transport, so any Cloud Run replica can serve any
    request without instance affinity. The returned app's own lifespan is NOT run when mounted;
    the parent must run ``server.session_manager.run()`` (available only after this call).
    """
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.mcp_allowed_hosts(),
        allowed_origins=settings.mcp_allowed_origins(),
    )
    return server.streamable_http_app(
        streamable_http_path=settings.normalized_mcp_path,
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
