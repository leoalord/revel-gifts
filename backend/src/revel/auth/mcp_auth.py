"""MCP authentication seam. Owned by F2 (authentication).

``mcp_auth_kwargs(settings)`` is splatted into ``MCPServer(...)`` by
``revel.mcp.server.create_mcp_server``. It enables the SDK's bearer-auth middleware
with our WorkOS AuthKit verifier, so every request to the MCP endpoint needs a valid
AuthKit access token whose audience is ``settings.mcp_resource_url``; otherwise the SDK
answers 401 with ``WWW-Authenticate: Bearer ..., resource_metadata="..."``.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

from revel.auth.runtime import AuthRuntime, runtime_for

logger = logging.getLogger(__name__)

# Issuer advertised when AuthKit is unconfigured. The verifier rejects every token
# in that state, so the MCP endpoint still requires (and never grants) auth.
UNCONFIGURED_ISSUER = "https://authkit.invalid"


class DelegationAwareVerifier(TokenVerifier):
    """The runtime's WorkOS verifier plus F2's read-only disconnect check.

    A token of a disconnected authorization (``IdentityResolver.is_disconnected``) verifies
    as ``None``, so the SDK's ``RequireAuthMiddleware`` answers HTTP 401 with
    ``WWW-Authenticate: Bearer error="invalid_token", ..., resource_metadata="..."`` exactly as
    for any other invalid token (no hint that a delegation exists), and MCP clients restart
    OAuth: reconnecting is the same as connecting.

    This is the client-facing signal, not the enforcement: every data tool still resolves
    through ``principal_from_mcp_context``, which applies the same rule. So when the identity
    store cannot be read here, the token is passed through (logged by class name) rather than
    turned into a 401 (which would push clients into re-authorizing during an outage) or a
    500 (which would break data-free requests such as ``initialize``); data tools then fail
    as they would anyway. Without a resolver the check is skipped.
    """

    def __init__(self, runtime: AuthRuntime):
        self._runtime = runtime

    async def verify_token(self, token: str) -> AccessToken | None:
        access_token = await self._runtime.verifier.verify_token(token)
        if access_token is None or self._runtime.resolver is None:
            return access_token
        try:
            disconnected = await self._runtime.resolver.is_disconnected(access_token)
        except Exception as exc:
            logger.warning("Disconnect check skipped: %s", type(exc).__name__)
            return access_token
        if disconnected:
            logger.info("Bearer token rejected: disconnected")
            return None
        return access_token


def mcp_auth_kwargs(settings: Any) -> dict[str, Any]:
    """Return ``token_verifier`` and ``auth`` kwargs for ``MCPServer``.

    Uses the auth runtime registered for ``settings`` (configuring it if needed), so
    the MCP endpoint and ``GET /me`` share one verifier.
    """
    runtime = runtime_for(settings)
    config = runtime.config
    return {
        "token_verifier": DelegationAwareVerifier(runtime),
        "auth": AuthSettings(
            issuer_url=config.issuer or UNCONFIGURED_ISSUER,
            resource_server_url=config.resource_url,
            # The verifier enforces the audience itself (aud == resource URL).
            validate_token_resource=False,
        ),
    }
