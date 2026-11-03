"""Privacy-safe audit logging of MCP JSON-RPC methods (F1, RG-7).

``McpAuditMiddleware`` is a pure ASGI wrapper around the SDK's Streamable HTTP app. For each
request to the MCP path it emits one structured line per JSON-RPC message on the
``revel.mcp.audit`` logger, so the onboarding client matrix can record which methods clients
(Muse, Claude, ...) call.

Logged: ``method`` (only names in ``MCP_METHODS``, else ``"unknown"``), a ``target`` (only a
tool, prompt, resource URI or resource template registered on the server, else
``"unknown"``), the authenticated OAuth ``client_id`` (set by the SDK bearer-auth middleware),
the response ``status``, ``batch_size`` and ``http_method``. No client-supplied free text is
logged. Never logged: params/arguments, bodies, results, tokens, subjects, emails, scopes, IPs
or user agents. The response body is never read. The request body is buffered (up to
``MAX_AUDIT_BODY_BYTES``) and replayed unchanged, and only when an ``Authorization`` header is
present: unauthenticated requests pass straight through and log ``method="unauthenticated"``.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.shared.uri_template import UriTemplate
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

AUDIT_LOGGER_NAME = "revel.mcp.audit"
MAX_AUDIT_BODY_BYTES = 1024 * 1024
# A batch of N messages yields N lines; bound it so one request cannot flood the logs.
MAX_LOGGED_MESSAGES = 50
UNPARSED = "unparsed"
UNKNOWN = "unknown"
UNAUTHENTICATED = "unauthenticated"

# Fixed allowlist of MCP method names (spec 2025-06-18 / 2025-11-25 plus those mcp==2.3.0
# serves). Anything else is logged as "unknown" so client-chosen strings never reach the logs.
MCP_METHODS: frozenset[str] = frozenset(
    {
        "initialize",
        "ping",
        "server/discover",
        "subscriptions/listen",
        "tools/list",
        "tools/call",
        "prompts/list",
        "prompts/get",
        "resources/list",
        "resources/read",
        "resources/templates/list",
        "resources/subscribe",
        "resources/unsubscribe",
        "completion/complete",
        "logging/setLevel",
        "sampling/createMessage",
        "roots/list",
        "elicitation/create",
        "tasks/get",
        "tasks/result",
        "tasks/list",
        "tasks/cancel",
        "notifications/initialized",
        "notifications/cancelled",
        "notifications/progress",
        "notifications/message",
        "notifications/roots/list_changed",
        "notifications/resources/updated",
        "notifications/resources/list_changed",
        "notifications/tools/list_changed",
        "notifications/prompts/list_changed",
        "notifications/elicitation/complete",
        "notifications/tasks/status",
    }
)

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


@dataclass(frozen=True)
class Catalog:
    """Names registered on the MCP server: the only targets that may be logged."""

    tools: frozenset[str] = frozenset()
    prompts: frozenset[str] = frozenset()
    resources: frozenset[str] = frozenset()
    resource_templates: tuple[str, ...] = ()


CatalogSource = Callable[[], Awaitable[Catalog]]


async def _empty_catalog() -> Catalog:
    return Catalog()


def server_catalog(server: MCPServer) -> CatalogSource:
    """Resolve the allowlist from ``server``'s public list APIs at request time, so tools,
    prompts and resources registered after the app is built are included."""

    async def load() -> Catalog:
        return Catalog(
            tools=frozenset(t.name for t in await server.list_tools()),
            prompts=frozenset(p.name for p in await server.list_prompts()),
            resources=frozenset(str(r.uri) for r in await server.list_resources()),
            resource_templates=tuple(
                t.uri_template for t in await server.list_resource_templates()
            ),
        )

    return load


@lru_cache(maxsize=256)
def _parsed_template(uri_template: str) -> UriTemplate | None:
    try:
        return UriTemplate.parse(uri_template)
    except Exception:
        return None


def _resource_target(uri: Any, catalog: Catalog) -> str:
    """The registered resource URI (query/fragment ignored) or matching template; otherwise
    ``"unknown"``. The result always equals a string from the server's registry."""
    if not isinstance(uri, str):
        return UNKNOWN
    if uri in catalog.resources:
        return uri
    try:
        parts = urlsplit(uri)
        bare = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else parts.path
    except ValueError:
        bare = ""
    if bare in catalog.resources:
        return bare
    for uri_template in catalog.resource_templates:
        parsed = _parsed_template(uri_template)
        try:
            if parsed is not None and parsed.match(uri) is not None:
                return uri_template
        except Exception:
            continue
    return UNKNOWN


def _registered(name: Any, names: frozenset[str]) -> str:
    return name if isinstance(name, str) and name in names else UNKNOWN


# (method, target kind, raw value) read from one message. The raw value is client-supplied:
# it is only compared against the catalog, never logged.
_Raw = tuple[str | None, str | None, Any]

_TARGET_KIND = {"tools/call": "tool", "prompts/get": "prompt", "resources/read": "resource"}


def _describe(message: Any) -> _Raw:
    if not isinstance(message, dict):
        return UNPARSED, None, None
    method = message.get("method")
    if method is None:
        return None, None, None  # a JSON-RPC response from the client
    if not isinstance(method, str) or method not in MCP_METHODS:
        return UNKNOWN, None, None
    kind = _TARGET_KIND.get(method)
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    value = params.get("uri") if kind == "resource" else params.get("name")
    return method, kind, value


def _parse(body: bytes) -> tuple[list[_Raw], int | None]:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return [(UNPARSED, None, None)], None
    if isinstance(payload, dict):
        return [_describe(payload)], 1
    if isinstance(payload, list) and payload:
        return [_describe(m) for m in payload[:MAX_LOGGED_MESSAGES]], len(payload)
    return [(UNPARSED, None, None)], None


async def _resolve(raw: list[_Raw], source: CatalogSource) -> list[tuple[str | None, str | None]]:
    catalog: Catalog | None = None
    entries: list[tuple[str | None, str | None]] = []
    for method, kind, value in raw:
        if kind is None:
            entries.append((method, None))
            continue
        if catalog is None:
            try:
                catalog = await source()
            except Exception:
                logger.warning("mcp audit catalog unavailable; targets logged as unknown")
                catalog = Catalog()
        if kind == "tool":
            target = _registered(value, catalog.tools)
        elif kind == "prompt":
            target = _registered(value, catalog.prompts)
        else:
            target = _resource_target(value, catalog)
        entries.append((method, target))
    return entries


def _client_id(scope: Scope) -> str | None:
    user = scope.get("user")
    if isinstance(user, AuthenticatedUser):
        return user.access_token.client_id[:200]
    return None


def _route_path(scope: Scope) -> str:
    path: str = scope.get("path", "")
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        path = path[len(root_path) :] or "/"
    return path


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value
    return None


def _content_length(scope: Scope) -> int | None:
    value = _header(scope, b"content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
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

    POST with an ``Authorization`` header: one line per JSON-RPC message
    (``method="unparsed"`` for malformed or oversized bodies). POST without one: the body is
    not read; one line with ``method="unauthenticated"``. Other HTTP methods (GET/DELETE): one
    line with ``method=null`` and their ``http_method``. ``catalog`` supplies the registered
    names ``target`` may take (none by default). The line is emitted when the response
    starts, so long-lived streams are logged immediately; the response body is untouched.
    """

    def __init__(
        self,
        app: ASGIApp,
        mcp_path: str,
        *,
        catalog: CatalogSource | None = None,
        max_body_bytes: int = MAX_AUDIT_BODY_BYTES,
    ) -> None:
        self.app = app
        self.mcp_path = mcp_path
        self.catalog = catalog or _empty_catalog
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _route_path(scope) != self.mcp_path:
            await self.app(scope, receive, send)
            return

        http_method: str = scope.get("method", "")
        downstream_receive = receive
        entries: list[tuple[str | None, str | None]]
        batch_size: int | None = None
        if http_method != "POST":
            entries = [(None, None)]
        elif _header(scope, b"authorization") is None:
            # The SDK answers 401 without reading the body; don't hold the request for it.
            entries = [(UNAUTHENTICATED, None)]
        else:
            raw, batch_size, downstream_receive = await self._read_body(scope, receive)
            entries = await _resolve(raw, self.catalog)

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
    ) -> tuple[list[_Raw], int | None, Receive]:
        """Buffer up to the cap; return parsed messages and a receive that replays the body."""
        declared = _content_length(scope)
        if declared is not None and declared > self.max_body_bytes:
            return [(UNPARSED, None, None)], None, receive

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
            return [(UNPARSED, None, None)], None, replay
        raw, batch_size = _parse(b"".join(m.get("body", b"") for m in buffered))
        return raw, batch_size, replay
