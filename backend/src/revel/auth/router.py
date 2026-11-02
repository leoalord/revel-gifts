"""OAuth discovery endpoints and ``GET /me`` (included at the application root)."""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from revel.auth.deps import CurrentPrincipal
from revel.auth.runtime import AuthNotConfigured, AuthRuntime, get_auth_runtime
from revel.services.identity import IdentityNotFound, WhoAmI, whoami

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

Runtime = Annotated[AuthRuntime, Depends(get_auth_runtime)]


def _protected_resource_metadata(runtime: AuthRuntime) -> dict[str, Any]:
    if runtime.config.issuer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Authorization not configured"
        )
    return {
        "resource": runtime.config.resource_url,
        "authorization_servers": [runtime.config.issuer],
        "bearer_methods_supported": ["header"],
        "resource_name": "Revel Gifts",
    }


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
async def protected_resource_metadata(runtime: Runtime) -> dict[str, Any]:
    return _protected_resource_metadata(runtime)


@router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def protected_resource_metadata_mcp(runtime: Runtime) -> dict[str, Any]:
    return _protected_resource_metadata(runtime)


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def authorization_server_metadata(runtime: Runtime) -> dict[str, Any]:
    """AuthKit's metadata, for clients that skip protected-resource discovery."""
    try:
        return await runtime.authorization_server_metadata()
    except AuthNotConfigured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Authorization not configured"
        ) from None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("AuthKit metadata fetch failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Authorization server unavailable"
        ) from None


@router.get("/me", response_model=WhoAmI)
async def me(principal: CurrentPrincipal, runtime: Runtime) -> WhoAmI:
    """Return the authenticated Revel user and, if any, the acting assistant."""
    try:
        return await whoami(principal, runtime.repository)
    except IdentityNotFound:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown principal"
        ) from None
