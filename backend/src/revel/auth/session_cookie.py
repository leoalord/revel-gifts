"""Deliver a refreshed browser session cookie on every response. Owned by F2.

:func:`revel.auth.ui.current_ui_principal` may refresh an expired WorkOS access token.
WorkOS rotates refresh tokens, so the re-sealed cookie *must* reach the browser or the
next request presents a spent refresh token and the user is signed out. FastAPI merges
the dependency's ``Response`` headers only when the endpoint returns plain data; they are
dropped when the endpoint returns its own ``Response`` (``JSONResponse``,
``RedirectResponse``...) or an ``HTTPException`` is raised after the dependency ran.

The dependency therefore also records the ``Set-Cookie`` header in the request's ASGI
state (:func:`record_refreshed_session_cookie`), and :class:`RefreshedSessionCookieMiddleware`
appends it to ``http.response.start`` unless the response already sets a cookie with the
same name (which covers both the merged-by-FastAPI case and any clearing header).

The middleware is pure ASGI: it never reads or buffers bodies, passes non-HTTP scopes
(lifespan, websocket) straight through and is a no-op for requests that recorded nothing.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Key in ``scope["state"]`` (i.e. ``request.state``); value is a list of
# ``(cookie_name, set_cookie_header_value)`` tuples. A list (mutable, shared by
# reference) stays visible to the middleware even if a layer shallow-copies the state dict.
STATE_KEY = "revel_auth_refreshed_session_cookies"


def record_refreshed_session_cookie(request: Request, name: str, header_value: str) -> None:
    """Record a refreshed session ``Set-Cookie`` for the middleware to guarantee delivery."""
    pending = getattr(request.state, STATE_KEY, None)
    if pending is None:
        pending = []
        setattr(request.state, STATE_KEY, pending)
    pending[:] = [(n, v) for n, v in pending if n != name]
    pending.append((name, header_value))


def _cookie_name(header_value: bytes) -> str:
    return header_value.split(b"=", 1)[0].strip().decode("latin-1")


class RefreshedSessionCookieMiddleware:
    """Append a recorded refreshed session cookie when the response lacks one."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        pending: list[tuple[str, str]] = []
        state[STATE_KEY] = pending

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start" and pending:
                headers = list(message.get("headers", []))
                present = {
                    _cookie_name(value) for key, value in headers if key.lower() == b"set-cookie"
                }
                added = False
                for name, value in pending:
                    if name not in present:
                        headers.append((b"set-cookie", value.encode("latin-1")))
                        added = True
                if added:
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cookie)
