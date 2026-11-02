"""OAuth discovery, ``GET /me`` (bearer) and browser sign-in routes (``/auth/*``).

Browser routes (contract for F8; see ``revel.auth.ui``):

* ``GET /auth/login?return_to=/path`` — 302 to WorkOS AuthKit hosted sign-in.
  ``return_to`` must be a same-origin path; anything else becomes ``/``.
* ``GET /auth/callback`` — WorkOS redirect target (``WORKOS_REDIRECT_URI``). Validates
  ``state``, exchanges the code, creates/reuses the user, sets the session cookie and
  302s to ``return_to``. Failures: 400 JSON (bad/missing state or code, cancelled
  sign-in), 401 (WorkOS rejected the code), 502/503 (WorkOS unavailable).
* ``GET /auth/session`` — the shared whoami payload (``via="ui"``, ``agent=null``) for
  the signed-in browser user; 401 when not signed in.
* ``POST /auth/logout`` — same-origin only; clears the session cookie and returns
  ``{"logout_url": ...}``. F8 must then navigate the browser to ``logout_url`` (when not
  ``null``): that is what ends the WorkOS/AuthKit session and its refresh token.

``GET /me`` stays bearer-only; cookies are never accepted there or on ``/mcp``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from revel.auth.browser import SessionInvalid, WorkOSRejected, WorkOSUnavailable
from revel.auth.deps import CurrentPrincipal
from revel.auth.identity import IdentityError, Principal
from revel.auth.runtime import AuthNotConfigured, AuthRuntime, get_auth_runtime
from revel.auth.ui import (
    CurrentUIPrincipal,
    clear_session_cookie,
    clear_state_cookie,
    require_browser,
    require_same_origin,
    set_session_cookie,
    set_state_cookie,
)
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
    # WorkOS recommends serving this for older MCP clients. Caveat: the document's
    # ``issuer`` is the AuthKit domain, not this origin, so strictly RFC 8414-validating
    # clients will reject it and must use protected-resource metadata (RFC 9728) instead.
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


async def _whoami(principal: Principal, runtime: AuthRuntime) -> WhoAmI:
    try:
        return await whoami(principal, runtime.repository)
    except IdentityNotFound:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown principal"
        ) from None


@router.get("/me", response_model=WhoAmI)
async def me(principal: CurrentPrincipal, runtime: Runtime) -> WhoAmI:
    """Return the authenticated Revel user and, if any, the acting assistant (bearer only)."""
    return await _whoami(principal, runtime)


# --- browser sessions ------------------------------------------------------------------


@router.get("/auth/login", include_in_schema=False, response_model=None)
async def login(runtime: Runtime, return_to: str | None = None) -> RedirectResponse:
    """Start browser sign-in through WorkOS AuthKit."""
    browser = require_browser(runtime)
    url, sealed_state = browser.start_login(return_to)
    response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    response.headers["cache-control"] = "no-store"
    set_state_cookie(response, browser, sealed_state)
    return response


def _callback_error(browser: Any, status_code: int, detail: str) -> JSONResponse:
    response = JSONResponse({"detail": detail}, status_code=status_code)
    response.headers["cache-control"] = "no-store"
    clear_state_cookie(response, browser)
    return response


@router.get("/auth/callback", include_in_schema=False, response_model=None)
async def callback(
    request: Request,
    runtime: Runtime,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> JSONResponse | RedirectResponse:
    """WorkOS redirect target: validate state, exchange the code, set the session."""
    browser = require_browser(runtime)
    try:
        return_to = browser.check_state(
            request.cookies.get(browser.config.state_cookie_name), state
        )
    except SessionInvalid:
        return _callback_error(browser, 400, "Sign-in expired or invalid; please try again")
    if error or not code:
        return _callback_error(browser, 400, "Sign-in was cancelled or failed")
    try:
        result = await browser.exchange_code(code)
    except WorkOSRejected:
        return _callback_error(browser, 401, "Sign-in could not be completed")
    except WorkOSUnavailable:
        return _callback_error(browser, 502, "Sign-in service unavailable")
    try:
        await runtime.require_resolver().resolve_user(result.workos_user_id, profile=result.profile)
    except AuthNotConfigured:
        return _callback_error(browser, 503, "Identity store unavailable")
    except IdentityError:
        return _callback_error(browser, 401, "Sign-in could not be completed")
    try:
        sealed = browser.seal_session(result.tokens, result.session_id)
    except WorkOSUnavailable:
        return _callback_error(browser, 502, "Sign-in service unavailable")
    response = RedirectResponse(return_to, status_code=status.HTTP_302_FOUND)
    response.headers["cache-control"] = "no-store"
    clear_state_cookie(response, browser)
    set_session_cookie(response, browser, sealed)
    return response


@router.get("/auth/session", response_model=WhoAmI)
async def browser_session(principal: CurrentUIPrincipal, runtime: Runtime) -> WhoAmI:
    """Shared whoami payload for the signed-in browser user (``via="ui"``)."""
    return await _whoami(principal, runtime)


class LogoutResult(BaseModel):
    logout_url: str | None


@router.post(
    "/auth/logout", response_model=LogoutResult, dependencies=[Depends(require_same_origin)]
)
async def logout(request: Request, runtime: Runtime) -> JSONResponse:
    """Clear the session cookie; return the WorkOS logout URL to end the AuthKit session."""
    browser = require_browser(runtime)
    session_id = browser.session_id_from_cookie(
        request.cookies.get(browser.config.session_cookie_name)
    )
    response = JSONResponse({"logout_url": browser.logout_url(session_id)})
    response.headers["cache-control"] = "no-store"
    clear_session_cookie(response, browser)
    return response
