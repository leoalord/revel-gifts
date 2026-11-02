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
  ``{"detail": "Not signed in"}``; the UI should then navigate to
  ``/auth/login?return_to=<current path>``. An unreadable/invalid cookie is also cleared;
  a cookie whose refresh token WorkOS rejected is left in place (a concurrent request may
  have just replaced it) and is overwritten by the next sign-in. WorkOS outage: 503.
* :func:`require_same_origin` rejects cookie-authenticated unsafe requests
  (POST/PUT/PATCH/DELETE) whose ``Origin`` (or, if absent, ``Referer``) is not this
  service's origin: **403**. :data:`SameOriginUIPrincipal` applies both.

Sign-out: call ``POST /auth/logout`` (same-origin) and then **always navigate the browser
to the returned ``logout_url``** when it is not ``null``. The POST only clears Revel's
cookie; the WorkOS/AuthKit session (and its refresh token, should a copy of the cookie
exist) ends only when the browser visits that URL.

Residual risk: refresh single-flight is per process. If two Cloud Run instances refresh
the same single-use refresh token at once, the loser answers 401 (cookie kept) and the UI
sends the user through sign-in again, which AuthKit completes without re-entering
credentials while its session is alive.

Cookies never authenticate ``/mcp`` or bearer-only routes such as ``GET /me``.
A refreshed cookie reaches the browser whether the route returns data, returns its own
``Response`` or raises ``HTTPException``: :mod:`revel.auth.session_cookie` appends it
when FastAPI did not merge it (routes need not copy it themselves).
"""

from __future__ import annotations

import re
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
    SessionRefreshRejected,
    WorkOSUnavailable,
)
from revel.auth.identity import IdentityError, Principal
from revel.auth.runtime import AuthNotConfigured, AuthRuntime, get_auth_runtime
from revel.auth.session_cookie import record_refreshed_session_cookie

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


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
    except SessionRefreshRejected:
        # Keep the cookie: a concurrent request may have rotated the refresh token and
        # set a fresher cookie, which a deletion here could race with and wipe.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in"
        ) from None
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
            sealed_new = browser.seal_session(session.tokens, session.session_id)
        except WorkOSUnavailable:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Sign-in service unavailable",
            ) from None
        # Merged by FastAPI when the route returns data. Also recorded for
        # RefreshedSessionCookieMiddleware, which delivers it (once) when the route returns
        # its own Response or raises: the rotated refresh token must reach the browser.
        set_session_cookie(response, browser, sealed_new)
        scratch = StarletteResponse()
        set_session_cookie(scratch, browser, sealed_new)
        record_refreshed_session_cookie(
            request, browser.config.session_cookie_name, scratch.headers["set-cookie"]
        )
    return principal


_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


def _parse_origin(value: str, *, allow_path: bool = False) -> tuple[str, str, int | None] | None:
    """Strictly parse ``scheme://host[:port]`` into ``(scheme, host, port)``, else ``None``.

    Rejects userinfo, non-numeric or out-of-range ports (``http://localhost:8000.evil``),
    invalid host characters and, for ``Origin`` values, any path/query/fragment.
    """
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host or "@" in parts.netloc:
        return None
    if not allow_path and (parts.path not in ("", "/") or parts.query or parts.fragment):
        return None
    if ":" in host:  # IPv6 literal; only loopback is ever allowed
        if host != "::1":
            return None
    elif not _HOST_RE.match(host):
        return None
    return scheme, host, port


def _origin_key(parsed: tuple[str, str, int | None]) -> str:
    scheme, host, port = parsed
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}" + (f":{port}" if port is not None else "")


def _origin_allowed(parsed: tuple[str, str, int | None], browser: BrowserSessionManager) -> bool:
    config = browser.config
    allowed = set()
    for candidate in (config.public_origin, *config.extra_origins):
        configured = _parse_origin(candidate)
        if configured is not None:
            allowed.add(_origin_key(configured))
    if _origin_key(parsed) in allowed:
        return True
    scheme, host, _ = parsed
    # Development/test only: any port on plain-http loopback (separately served dev UIs).
    return config.development and scheme == "http" and host in _LOOPBACK_HOSTS


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
    if raw is not None:
        origin = _parse_origin(raw) if raw != "null" else None
    else:
        referer = request.headers.get("referer")
        origin = _parse_origin(referer, allow_path=True) if referer else None
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
