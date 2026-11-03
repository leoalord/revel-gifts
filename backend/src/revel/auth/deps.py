"""Transport adapters that turn a bearer token into a :class:`Principal`.

* :func:`current_principal` — FastAPI dependency for HTTP API routes.
* :func:`principal_from_mcp_context` — for MCP tools; the SDK's auth middleware has
  already verified the token with the same verifier.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError

from revel.auth.identity import DelegationDisconnected, IdentityError, Principal
from revel.auth.runtime import AuthNotConfigured, AuthRuntime, get_auth_runtime


def _www_authenticate(
    runtime: AuthRuntime, error: str | None = None, description: str | None = None
) -> str:
    parts: list[str] = []
    if error:
        parts.append(f'error="{error}"')
    if description:
        parts.append(f'error_description="{description}"')
    parts.append(f'resource_metadata="{runtime.config.resource_metadata_url}"')
    return "Bearer " + ", ".join(parts)


def _invalid_token(runtime: AuthRuntime) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired access token",
        headers={
            "WWW-Authenticate": _www_authenticate(
                runtime, "invalid_token", "Invalid or expired token"
            )
        },
    )


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or ""


async def current_principal(
    request: Request,
    runtime: Annotated[AuthRuntime, Depends(get_auth_runtime)],
) -> Principal:
    """Authenticate the request's bearer token and resolve the principal (once per request)."""
    token = _bearer_token(request)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": _www_authenticate(runtime)},
        )
    access_token = await runtime.verifier.verify_token(token) if token else None
    if access_token is None:
        raise _invalid_token(runtime)
    try:
        return await runtime.require_resolver().resolve(access_token, via="api")
    except DelegationDisconnected:
        # Same 401 as any other invalid token (does not reveal that a delegation exists);
        # it makes OAuth clients re-authorize, and a fresh authorization reconnects.
        raise _invalid_token(runtime) from None
    except AuthNotConfigured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Identity store unavailable"
        ) from None
    except IdentityError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token",
            headers={
                "WWW-Authenticate": _www_authenticate(
                    runtime, "invalid_token", "Invalid token claims"
                )
            },
        ) from None


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


async def principal_from_mcp_context(runtime: AuthRuntime) -> Principal:
    """Resolve the principal for the current MCP request.

    **Every MCP tool that reads or writes user data must call this first** and act
    only as the returned principal. The SDK middleware (``DelegationAwareVerifier``)
    already answers 401 for a disconnected authorization's tokens, but it does not create
    or select the delegation; this call does, and re-checks the disconnect rule (only
    data-free tools such as ``ping`` may skip it).

    Raises :class:`ToolError` (returned to the client as a tool error) when the
    request is unauthenticated or the delegation is disconnected.
    """
    access_token = get_access_token()
    if access_token is None:
        # The MCP route requires auth, so this indicates a wiring error; fail closed.
        raise ToolError("Authentication required")
    try:
        return await runtime.require_resolver().resolve(access_token, via="mcp")
    except DelegationDisconnected:
        # Normally rejected earlier with HTTP 401 by ``DelegationAwareVerifier``; this covers
        # a disconnect that lands between authentication and the tool call. Same wording as
        # any other invalid token.
        raise ToolError("Invalid access token") from None
    except AuthNotConfigured:
        raise ToolError("Revel identity service is unavailable") from None
    except IdentityError:
        raise ToolError("Invalid access token") from None
