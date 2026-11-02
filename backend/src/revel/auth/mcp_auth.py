"""MCP authentication seam. Owned by F2 (authentication).

``mcp_auth_kwargs(settings)`` is splatted into ``MCPServer(...)`` by
``revel.mcp.server.create_mcp_server``. It enables the SDK's bearer-auth middleware
with our WorkOS AuthKit verifier, so every request to the MCP endpoint needs a valid
AuthKit access token whose audience is ``settings.mcp_resource_url``; otherwise the SDK
answers 401 with ``WWW-Authenticate: Bearer ..., resource_metadata="..."``.
"""

from __future__ import annotations

from typing import Any

from mcp.server.auth.settings import AuthSettings

from revel.auth.runtime import runtime_for

# Issuer advertised when AuthKit is unconfigured. The verifier rejects every token
# in that state, so the MCP endpoint still requires (and never grants) auth.
UNCONFIGURED_ISSUER = "https://authkit.invalid"


def mcp_auth_kwargs(settings: Any) -> dict[str, Any]:
    """Return ``token_verifier`` and ``auth`` kwargs for ``MCPServer``.

    Uses the auth runtime registered for ``settings`` (configuring it if needed), so
    the MCP endpoint and ``GET /me`` share one verifier.
    """
    runtime = runtime_for(settings)
    config = runtime.config
    return {
        "token_verifier": runtime.verifier,
        "auth": AuthSettings(
            issuer_url=config.issuer or UNCONFIGURED_ISSUER,
            resource_server_url=config.resource_url,
            # The verifier enforces the audience itself (aud == resource URL).
            validate_token_resource=False,
        ),
    }
