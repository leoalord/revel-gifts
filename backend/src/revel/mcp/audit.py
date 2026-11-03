"""Privacy-safe audit logging of MCP JSON-RPC methods (F1, RG-7).

``McpAuditMiddleware`` is a pure ASGI wrapper around the SDK's Streamable HTTP app. For each
request to the MCP path it emits one structured line per JSON-RPC message on the
``revel.mcp.audit`` logger, so the onboarding client matrix can record which methods clients
(Muse, Claude, ...) call.

Logged: ``method``, a ``target`` (tool name for ``tools/call``, prompt name for
``prompts/get``, resource URI without query/fragment/userinfo for ``resources/read``), the
authenticated OAuth ``client_id`` (set by the SDK bearer-auth middleware), the response
``status``, ``batch_size`` and ``http_method``. Never logged: params/arguments, bodies,
results, tokens, subjects, emails, scopes, IPs or user agents. The response body is never
read; the request body is buffered (up to ``MAX_AUDIT_BODY_BYTES``) and replayed unchanged.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.types import ASGIApp, Message, Receive, Scope, Send

AUDIT_LOGGER_NAME = "revel.mcp.audit"
MAX_AUDIT_BODY_BYTES = 1024 * 1024
# A batch of N messages yields N lines; bound it so one request cannot flood the logs.
MAX_LOGGED_MESSAGES = 50
_MAX_FIELD_CHARS = 200
UNPARSED = "unparsed"

logger = logging.getLogger(AUDIT_LOGGER_NAME)


class _StdoutJsonHandler(logging.Handler):
    """Write each record as one JSON line to the *current* ``sys.stdout``.

    Uvicorn's default logging config configures only ``uvicorn.*`` loggers, so without a
    handler INFO records from ``revel.*`` would be dropped. Cloud Run parses JSON stdout lines
    into ``jsonPayload`` and maps ``severity``.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields = getattr(record, "audit", None) or {"message": record.getMessage()}
            line = json.dumps(
                {"severity": record.levelname, "logger": record.name, **fields},
                separators=(",", ":"),
            )
            stream = sys.stdout
            stream.write(line + "\n")
            stream.flush()
        except Exception:  # pragma: no cover - logging must never break a request
            self.handleError(record)


def configure_audit_logger() -> None:
    """Idempotently give the audit logger INFO level and a stdout JSON handler.

    Propagation is off: the MCP SDK's ``MCPServer`` calls ``logging.basicConfig`` (a plain-text
    root handler on stderr), which would otherwise emit every audit line a second time.
    """
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(isinstance(h, _StdoutJsonHandler) for h in logger.handlers):
        logger.addHandler(_StdoutJsonHandler())


def _clip(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:_MAX_FIELD_CHARS]


def _resource_target(uri: Any) -> str | None:
    """Scheme, host and path of a resource URI; no userinfo, port, query or fragment."""
    if not isinstance(uri, str):
        return None
    try:
        parts = urlsplit(uri)
    except ValueError:
        return UNPARSED
    host = parts.hostname or ""
    if parts.scheme:
        return _clip(f"{parts.scheme}://{host}{parts.path}")
    return _clip(parts.path)


def _describe(message: Any) -> tuple[str | None, str | None]:
    """Return ``(method, target)`` for one JSON-RPC message; never anything else from it."""
    if not isinstance(message, dict):
        return UNPARSED, None
    method = _clip(message.get("method"))
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    if method in ("tools/call", "prompts/get"):
        return method, _clip(params.get("name"))
    if method == "resources/read":
        return method, _resource_target(params.get("uri"))
    return method, None


def _parse(body: bytes) -> tuple[list[tuple[str | None, str | None]], int | None]:
    """Return per-message ``(method, target)`` pairs and the batch size."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return [(UNPARSED, None)], None
    if isinstance(payload, dict):
        return [_describe(payload)], 1
    if isinstance(payload, list) and payload:
        return [_describe(m) for m in payload[:MAX_LOGGED_MESSAGES]], len(payload)
    return [(UNPARSED, None)], None


def _client_id(scope: Scope) -> str | None:
    user = scope.get("user")
    if isinstance(user, AuthenticatedUser):
        return _clip(user.access_token.client_id)
    return None


def _route_path(scope: Scope) -> str:
    path: str = scope.get("path", "")
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        path = path[len(root_path) :] or "/"
    return path


def _content_length(scope: Scope) -> int | None:
    for key, value in scope.get("headers", ()):
        if key == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _emit(
    entries: Iterable[tuple[str | None, str | None]],
    *,
    batch_size: int | None,
    http_method: str,
    status: int | None,
    client_id: str | None,
) -> None:
    for method, target in entries:
        fields = {
            "event": "mcp_request",
            "method": method,
            "target": target,
            "client_id": client_id,
            "status": status,
            "batch_size": batch_size,
            "http_method": http_method,
        }
        logger.info(json.dumps(fields, separators=(",", ":")), extra={"audit": fields})


class McpAuditMiddleware:
    """ASGI wrapper logging MCP JSON-RPC methods for requests to ``mcp_path``.

    POST: one line per JSON-RPC message (``method="unparsed"`` for malformed or oversized
    bodies). Other HTTP methods (GET/DELETE): one line with ``method=null`` and their
    ``http_method``. The line is emitted when the response starts, so long-lived streams are
    logged immediately; the response body itself is passed through untouched.
    """

    def __init__(
        self, app: ASGIApp, mcp_path: str, *, max_body_bytes: int = MAX_AUDIT_BODY_BYTES
    ) -> None:
        self.app = app
        self.mcp_path = mcp_path
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _route_path(scope) != self.mcp_path:
            await self.app(scope, receive, send)
            return

        http_method: str = scope.get("method", "")
        downstream_receive = receive
        if http_method == "POST":
            entries, batch_size, downstream_receive = await self._read_body(scope, receive)
        else:
            entries, batch_size = [(None, None)], None

        logged = False

        def log(status: int | None) -> None:
            nonlocal logged
            if logged:
                return
            logged = True
            try:
                _emit(
                    entries,
                    batch_size=batch_size,
                    http_method=http_method,
                    status=status,
                    client_id=_client_id(scope),
                )
            except Exception:  # pragma: no cover - auditing must never break a request
                logger.exception("mcp audit logging failed")

        async def audited_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                log(message.get("status"))
            await send(message)

        try:
            await self.app(scope, downstream_receive, audited_send)
        finally:
            log(None)

    async def _read_body(
        self, scope: Scope, receive: Receive
    ) -> tuple[list[tuple[str | None, str | None]], int | None, Receive]:
        """Buffer up to the cap; return parsed entries and a receive that replays the body."""
        declared = _content_length(scope)
        if declared is not None and declared > self.max_body_bytes:
            return [(UNPARSED, None)], None, receive

        buffered: list[Message] = []
        size = 0
        complete = False
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break  # e.g. http.disconnect: replay it and let the app handle it
            size += len(message.get("body", b""))
            if size > self.max_body_bytes:
                break
            if not message.get("more_body", False):
                complete = True
                break

        pending = list(buffered)

        async def replay() -> Message:
            if pending:
                return pending.pop(0)
            return await receive()

        if not complete:
            return [(UNPARSED, None)], None, replay
        body = b"".join(m.get("body", b"") for m in buffered)
        entries, batch_size = _parse(body)
        return entries, batch_size, replay
