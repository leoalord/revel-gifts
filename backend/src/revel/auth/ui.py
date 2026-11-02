"""Browser (human UI) authentication contract for F8 routes. Owned by F2.

Use in FastAPI routes::

    from revel.auth.ui import CurrentUIPrincipal, SameOriginUIPrincipal

    @router.get("/api/ui/profile")
    async def read(principal: CurrentUIPrincipal): ...        # safe methods

    @router.post("/api/ui/policy")
    async def write(principal: SameOriginUIPrincipal): ...    # unsafe methods (CSRF-checked)

* :func:`current_ui_principal` reads the sealed session cookie, verifies the WorkOS
  access token (refreshing it when expired and re-setting the cookie on the route's
  response) and returns ``Principal(user_id, None, None, via="ui")`` for the same
  ``users`` row the MCP OAuth path resolves. Missing/invalid session: **401 JSON**
  ``{"detail": "Not signed in"}`` (an invalid cookie is also cleared); the UI should
  then navigate to ``/auth/login?return_to=<current path>``. WorkOS outage: 503.
* :func:`require_same_origin` rejects cookie-authenticated unsafe requests
  (POST/PUT/PATCH/DELETE) whose ``Origin`` (or, if absent, ``Referer``) is not this
  service's origin: **403**. :data:`SameOriginUIPrincipal` applies both.

Cookies never authenticate ``/mcp`` or bearer-only routes such as ``GET /me``.
A route that returns a ``Response`` object directly must copy the refreshed cookie
itself; returning data (dict/model) lets FastAPI merge it automatically.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response, status
from starlette.responses import Response as StarletteResponse

from revel.auth.browser import (
    SESSION_MAX_AGE_SECONDS,
    STATE_MAX_AGE_SECONDS,
    BrowserAuthNotConfigured,
    BrowserSessionManager,
    SessionInvalid,
    WorkOSUnavailable,
)
from revel.auth.identity import IdentityError, Principal
from revel.auth.runtime import AuthNotConfigured, AuthRuntime, get_auth_runtime

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


# --- cookie helpers ----------------------------------------------------------------------


def set_session_cookie(response: StarletteResponse, browser: BrowserSessionManager, value: str):
    response.set_cookie(
        browser.config.session_cookie_name,
        value,
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
        secure=browser.config.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: StarletteResponse, browser: BrowserSessionManager) -> None:
    response.delete_cookie(
        browser.config.session_cookie_name,
        path="/",
        secure=browser.config.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def set_state_cookie(response: StarletteResponse, browser: BrowserSessionManager, value: str):
    response.set_cookie(
        browser.config.state_cookie_name,
        value,
        max_age=STATE_MAX_AGE_SECONDS,
        path="/",
        secure=browser.config.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def clear_state_cookie(response: StarletteResponse, browser: BrowserSessionManager) -> None:
    response.delete_cookie(
        browser.config.state_cookie_name,
        path="/",
        secure=browser.config.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def _clearing_header(browser: BrowserSessionManager) -> dict[str, str]:
    """``Set-Cookie`` header that clears the session, for use on an HTTPException."""
    scratch = StarletteResponse()
    clear_session_cookie(scratch, browser)
    return {"set-cookie": scratch.headers["set-cookie"]}


def require_browser(runtime: AuthRuntime) -> BrowserSessionManager:
    try:
        browser = runtime.require_browser()
        browser.require_configured()
    except (AuthNotConfigured, BrowserAuthNotConfigured):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Browser sign-in is not configured",
        ) from None
    return browser


# --- dependencies ------------------------------------------------------------------------


async def current_ui_principal(
    request: Request,
    response: Response,
    runtime: Annotated[AuthRuntime, Depends(get_auth_runtime)],
) -> Principal:
    """Authenticate the browser session cookie and resolve the ``ui`` principal."""
    browser = require_browser(runtime)
    sealed = request.cookies.get(browser.config.session_cookie_name)
    if not sealed:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    try:
        session = await browser.authenticate_cookie(sealed)
    except SessionInvalid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not signed in",
            headers=_clearing_header(browser),
        ) from None
    except (WorkOSUnavailable, BrowserAuthNotConfigured):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sign-in service unavailable",
        ) from None
    try:
        principal = await runtime.require_resolver().resolve_user(session.workos_user_id)
    except AuthNotConfigured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Identity store unavailable"
        ) from None
    except IdentityError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not signed in",
            headers=_clearing_header(browser),
        ) from None
    if session.refreshed:
        try:
            set_session_cookie(
                response, browser, browser.seal_session(session.tokens, session.session_id)
            )
        except WorkOSUnavailable:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Sign-in service unavailable",
            ) from None
    return principal


def _origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def _origin_allowed(origin: str, browser: BrowserSessionManager) -> bool:
    config = browser.config
    allowed = {config.public_origin.lower(), *(o.lower() for o in config.extra_origins)}
    if origin in allowed:
        return True
    if config.development:
        host = urlsplit(origin).hostname or ""
        bracketed = f"[{host}]" if ":" in host else host
        return urlsplit(origin).scheme == "http" and bracketed in _LOCAL_HOSTS
    return False


async def require_same_origin(
    request: Request,
    runtime: Annotated[AuthRuntime, Depends(get_auth_runtime)],
) -> None:
    """CSRF guard for cookie-authenticated routes.

    Safe methods pass. For unsafe methods the ``Origin`` header (or, when a browser
    omits it, the ``Referer`` origin) must be ``PUBLIC_BASE_URL``'s origin, an
    ``ALLOWED_ORIGINS`` entry, or (development/test only) ``http://localhost``. A request
    with neither header, or ``Origin: null``, is rejected with 403. Combined with
    ``SameSite=Lax`` cookies this blocks cross-site form posts and fetches.
    """
    if request.method.upper() in SAFE_METHODS:
        return
    browser = require_browser(runtime)
    raw = request.headers.get("origin")
    origin = _origin_of(raw) if raw and raw != "null" else None
    if raw is None:
        referer = request.headers.get("referer")
        origin = _origin_of(referer) if referer else None
    if origin is None or not _origin_allowed(origin, browser):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-origin request")


CurrentUIPrincipal = Annotated[Principal, Depends(current_ui_principal)]


async def same_origin_ui_principal(
    _: Annotated[None, Depends(require_same_origin)],
    principal: CurrentUIPrincipal,
) -> Principal:
    """``current_ui_principal`` plus the same-origin (CSRF) check, for unsafe methods."""
    return principal


SameOriginUIPrincipal = Annotated[Principal, Depends(same_origin_ui_principal)]
