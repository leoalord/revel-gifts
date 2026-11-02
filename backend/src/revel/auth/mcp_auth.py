"""MCP authentication seam. Owned by F2 (authentication).

``mcp_auth_kwargs`` returns keyword arguments passed to the ``MCPServer`` constructor
(``revel.mcp.server.create_mcp_server``). For the pinned SDK (mcp==2.3.0) the relevant
constructor parameters are ``token_verifier`` (a ``mcp.server.auth.provider.TokenVerifier``)
and ``auth`` (``mcp.server.auth.settings.AuthSettings`` with ``issuer_url``,
``resource_server_url``, ``required_scopes``, ``validate_token_resource``, ...).
Returning ``{}`` leaves the MCP endpoint unauthenticated (scaffold placeholder only).
"""

from typing import Any

from revel.settings import Settings


def mcp_auth_kwargs(settings: Settings) -> dict[str, Any]:
    return {}
