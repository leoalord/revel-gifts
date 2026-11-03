"""RG-7: privacy-safe MCP method audit logging (revel.mcp.audit)."""

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from auth.authkit import TestAuthKit
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from revel.app import create_app
from revel.mcp.audit import AUDIT_LOGGER_NAME, Catalog, McpAuditMiddleware, server_catalog
from revel.mcp.server import create_mcp_server
from revel.settings import Settings

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
SECRET_ARG = "s3cr3t-argument-value"
SECRET_QUERY = "q=private-query-value"
FAKE_BEARER = "Bearer fake-secret-bearer-value"
AUTH_HEADERS = {**MCP_HEADERS, "Authorization": FAKE_BEARER}
CATALOG = Catalog(
    tools=frozenset({"ping", "save_profile"}),
    prompts=frozenset({"revel_onboarding"}),
    resources=frozenset({"revel://guidance/index", "https://example.com/a/b"}),
    resource_templates=("revel://profiles/{handle}",),
)


async def _catalog() -> Catalog:
    return CATALOG


def _mw(app: Any, **kwargs: Any) -> McpAuditMiddleware:
    return McpAuditMiddleware(app, "/mcp", catalog=_catalog, **kwargs)


@pytest.fixture
def settings() -> Settings:
    settings = TestAuthKit.settings(public_base_url="http://localhost:8000")
    TestAuthKit().install(settings)
    return settings


@pytest.fixture
def token(settings: Settings) -> str:
    return TestAuthKit.token(settings)


@pytest.fixture(autouse=True)
def _capture_audit_logger(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """The audit logger does not propagate to root, so attach caplog's handler directly."""
    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    audit_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    yield
    audit_logger.removeHandler(caplog.handler)


def _audit(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    records = [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME and hasattr(r, "audit")]
    lines = [json.loads(r.getMessage()) for r in records]
    for line in lines:
        assert set(line) == {
            "event",
            "method",
            "target",
            "client_id",
            "status",
            "batch_size",
            "http_method",
        }
        assert line["event"] == "mcp_request"
    return lines


def _all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        f"{r.getMessage()} {getattr(r, 'audit', '')} {r.args!r}" for r in caplog.records
    )


class Recorder:
    """Downstream ASGI app that records the exact request it received and answers ``status``."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.messages: list[Message] = []

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while (await receive())["type"] != "lifespan.shutdown":
                await send({"type": "lifespan.startup.complete"})
            await send({"type": "lifespan.shutdown.complete"})
            return
        while True:
            message = await receive()
            self.messages.append(message)
            if message["type"] != "http.request" or not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})


def _post(
    app: Any, content: bytes, path: str = "/mcp", headers: dict[str, str] | None = None
) -> int:
    with TestClient(app, base_url="http://localhost:8000") as client:
        return client.post(path, content=content, headers=headers or AUTH_HEADERS).status_code


def _rpc(method: Any, params: dict[str, Any] | None = None, id_: int = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        message["params"] = params
    return message


# --- middleware with a fixed catalog -------------------------------------------------------


def test_tools_call_logs_name_not_arguments(caplog: pytest.LogCaptureFixture) -> None:
    recorder = Recorder()
    body = json.dumps(
        _rpc("tools/call", {"name": "save_profile", "arguments": {"note": SECRET_ARG}})
    ).encode()
    assert _post(_mw(recorder), body) == 200
    assert _audit(caplog) == [
        {
            "event": "mcp_request",
            "method": "tools/call",
            "target": "save_profile",
            "client_id": None,
            "status": 200,
            "batch_size": 1,
            "http_method": "POST",
        }
    ]
    text = _all_log_text(caplog)
    assert SECRET_ARG not in text
    assert FAKE_BEARER.split()[1] not in text
    assert recorder.body == body


def test_prompts_get_logs_prompt_name(caplog: pytest.LogCaptureFixture) -> None:
    body = json.dumps(
        _rpc("prompts/get", {"name": "revel_onboarding", "arguments": {"x": SECRET_ARG}})
    ).encode()
    _post(_mw(Recorder()), body)
    [line] = _audit(caplog)
    assert (line["method"], line["target"]) == ("prompts/get", "revel_onboarding")
    assert SECRET_ARG not in _all_log_text(caplog)


@pytest.mark.parametrize(
    ("uri", "target"),
    [
        (f"revel://guidance/index?{SECRET_QUERY}#frag", "revel://guidance/index"),
        ("revel://guidance/index", "revel://guidance/index"),
        ("https://example.com/a/b#x", "https://example.com/a/b"),
        (f"https://user:pw@example.com:8443/a/b?{SECRET_QUERY}", "unknown"),
        ("revel://profiles/alice", "revel://profiles/{handle}"),
        (f"revel://guidance/private-{SECRET_ARG}", "unknown"),
        ("not a uri at all", "unknown"),
    ],
)
def test_resources_read_allowlisted_and_drops_query(
    caplog: pytest.LogCaptureFixture, uri: str, target: str
) -> None:
    _post(_mw(Recorder()), json.dumps(_rpc("resources/read", {"uri": uri})).encode())
    [line] = _audit(caplog)
    assert (line["method"], line["target"]) == ("resources/read", target)
    text = _all_log_text(caplog)
    assert SECRET_QUERY not in text
    assert SECRET_ARG not in text
    assert "pw" not in json.dumps(line)
    assert "alice" not in json.dumps(line)


def test_unregistered_tool_and_prompt_logged_unknown(caplog: pytest.LogCaptureFixture) -> None:
    batch = [
        _rpc("tools/call", {"name": f"evil-{SECRET_ARG}", "arguments": {}}, 1),
        _rpc("prompts/get", {"name": f"evil-{SECRET_ARG}"}, 2),
        _rpc("tools/call", {"name": 42}, 3),
        _rpc("tools/call", None, 4),
    ]
    _post(_mw(Recorder()), json.dumps(batch).encode())
    assert [(line["method"], line["target"]) for line in _audit(caplog)] == [
        ("tools/call", "unknown"),
        ("prompts/get", "unknown"),
        ("tools/call", "unknown"),
        ("tools/call", "unknown"),
    ]
    assert SECRET_ARG not in _all_log_text(caplog)


def test_unknown_method_not_logged_verbatim(caplog: pytest.LogCaptureFixture) -> None:
    batch = [
        _rpc(f"custom/{SECRET_ARG}", {}, 1),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": f"notifications/{SECRET_ARG}"},
        _rpc(["not", "a", "string"], None, 2),
        "not-an-object",
    ]
    _post(_mw(Recorder()), json.dumps(batch).encode())
    assert [line["method"] for line in _audit(caplog)] == [
        "unknown",
        "notifications/initialized",
        "unknown",
        "unknown",
        "unparsed",
    ]
    assert SECRET_ARG not in _all_log_text(caplog)


def test_without_catalog_targets_are_unknown(caplog: pytest.LogCaptureFixture) -> None:
    body = json.dumps(_rpc("tools/call", {"name": "ping"})).encode()
    _post(McpAuditMiddleware(Recorder(), "/mcp"), body)
    assert [line["target"] for line in _audit(caplog)] == ["unknown"]


def test_catalog_failure_logs_unknown_and_request_proceeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken() -> Catalog:
        raise RuntimeError("boom")

    recorder = Recorder()
    body = json.dumps(_rpc("tools/call", {"name": "ping"})).encode()
    assert _post(McpAuditMiddleware(recorder, "/mcp", catalog=broken), body) == 200
    assert [line["target"] for line in _audit(caplog)] == ["unknown"]
    assert recorder.body == body


def test_batch_logs_one_line_per_message(caplog: pytest.LogCaptureFixture) -> None:
    batch = [
        _rpc("tools/list", {}, 1),
        _rpc("tools/call", {"name": "ping", "arguments": {"a": SECRET_ARG}}, 2),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 7, "result": {"secret": SECRET_ARG}},
    ]
    recorder = Recorder()
    body = json.dumps(batch).encode()
    _post(_mw(recorder), body)
    lines = _audit(caplog)
    assert [(line["method"], line["target"]) for line in lines] == [
        ("tools/list", None),
        ("tools/call", "ping"),
        ("notifications/initialized", None),
        (None, None),
    ]
    assert {line["batch_size"] for line in lines} == {4}
    assert SECRET_ARG not in _all_log_text(caplog)
    assert recorder.body == body


def test_malformed_json_unparsed_and_body_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    recorder = Recorder()
    body = b'{"jsonrpc": "2.0", "method": "tools/call", "params": {"x": "' + SECRET_ARG.encode()
    _post(_mw(recorder), body)
    [line] = _audit(caplog)
    assert (line["method"], line["target"]) == ("unparsed", None)
    assert recorder.body == body
    assert SECRET_ARG not in _all_log_text(caplog)


def test_oversized_body_passes_through(caplog: pytest.LogCaptureFixture) -> None:
    recorder = Recorder()
    body = json.dumps(_rpc("tools/call", {"name": "ping", "arguments": {"x": "y" * 5000}})).encode()
    _post(_mw(recorder, max_body_bytes=1024), body)
    [line] = _audit(caplog)
    assert (line["method"], line["status"]) == ("unparsed", 200)
    assert recorder.body == body


@pytest.mark.anyio
async def test_oversized_chunked_body_replayed_in_order(caplog: pytest.LogCaptureFixture) -> None:
    """No Content-Length: buffered chunks are replayed, then the rest is streamed through."""
    chunks = [b"a" * 600, b"b" * 600, b"c" * 600]
    incoming: list[Message] = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]

    async def receive() -> Message:
        return incoming.pop(0)

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    recorder = Recorder()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", FAKE_BEARER.encode())],
    }
    await _mw(recorder, max_body_bytes=1000)(scope, receive, send)
    assert [m.get("body") for m in recorder.messages] == chunks
    assert [line["method"] for line in _audit(caplog)] == ["unparsed"]
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_unauthenticated_post_does_not_read_body(caplog: pytest.LogCaptureFixture) -> None:
    """No Authorization header: ``receive`` reaches the app untouched and is never awaited."""

    async def receive() -> Message:
        raise AssertionError("middleware must not read an unauthenticated body")

    async def app(scope: Scope, receive_: Receive, send: Send) -> None:
        assert receive_ is receive
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message: Message) -> None:
        pass

    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}
    await _mw(app)(scope, receive, send)
    assert _audit(caplog) == [
        {
            "event": "mcp_request",
            "method": "unauthenticated",
            "target": None,
            "client_id": None,
            "status": 401,
            "batch_size": None,
            "http_method": "POST",
        }
    ]


def test_non_post_logs_null_method(caplog: pytest.LogCaptureFixture) -> None:
    with TestClient(_mw(Recorder(405))) as client:
        client.get("/mcp")
        client.delete("/mcp", headers={"Authorization": FAKE_BEARER})
    lines = _audit(caplog)
    assert [(line["method"], line["http_method"], line["status"]) for line in lines] == [
        (None, "GET", 405),
        (None, "DELETE", 405),
    ]


def test_other_paths_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    _post(_mw(Recorder()), b"{}", path="/other")
    assert _audit(caplog) == []


@pytest.mark.anyio
async def test_server_catalog_is_resolved_lazily() -> None:
    """Names registered after the catalog source is built are still allowlisted."""
    settings = Settings(_env_file=None, app_env="test", public_base_url="http://localhost:8000")
    server = create_mcp_server(settings)
    load = server_catalog(server)

    @server.tool(description="late")
    def late_tool() -> str:
        return "x"

    catalog = await load()
    assert {"ping", "late_tool"} <= catalog.tools
    assert "revel://guidance/index" in catalog.resources  # F9 guidance resources
    assert catalog.prompts  # F9 guidance prompts


# --- through the real app (SDK bearer auth + transport security) ---------------------------


def test_unauthenticated_401_logged_without_client_id(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(create_app(settings), base_url="http://localhost:8000") as client:
        response = client.post("/mcp", json=_rpc("tools/list", {}), headers=MCP_HEADERS)
    assert response.status_code == 401
    assert _audit(caplog) == [
        {
            "event": "mcp_request",
            "method": "unauthenticated",
            "target": None,
            "client_id": None,
            "status": 401,
            "batch_size": None,
            "http_method": "POST",
        }
    ]


def test_bogus_bearer_401_parsed_without_client_id(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    call = _rpc("resources/read", {"uri": f"revel://guidance/index?{SECRET_QUERY}"})
    with TestClient(create_app(settings), base_url="http://localhost:8000") as client:
        response = client.post("/mcp", json=call, headers=AUTH_HEADERS)
    assert response.status_code == 401
    [line] = _audit(caplog)
    assert (line["method"], line["target"], line["client_id"], line["status"]) == (
        "resources/read",
        "revel://guidance/index",
        None,
        401,
    )
    text = _all_log_text(caplog)
    assert FAKE_BEARER.split()[1] not in text
    assert SECRET_QUERY not in text


def test_authenticated_call_logs_client_id_only(
    settings: Settings, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    auth = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    call = _rpc("tools/call", {"name": "ping", "arguments": {}})
    unknown = _rpc("tools/call", {"name": f"nope-{SECRET_ARG}", "arguments": {}})
    with TestClient(create_app(settings), base_url="http://localhost:8000") as client:
        response = client.post("/mcp", json=call, headers=auth)
        assert response.status_code == 200, response.text
        assert response.json()["result"]["content"][0]["text"] == "pong"
        assert client.post("/mcp", json=unknown, headers=auth).status_code == 200
        bad_host = client.post("/mcp", json=call, headers={**auth, "Host": "evil.example.com"})
        assert bad_host.status_code == 421
    lines = _audit(caplog)
    assert [
        (line["method"], line["target"], line["client_id"], line["status"]) for line in lines
    ] == [
        ("tools/call", "ping", "client_muse", 200),
        ("tools/call", "unknown", "client_muse", 200),
        ("tools/call", "ping", "client_muse", 421),
    ]
    audit_text = json.dumps(lines)
    text = _all_log_text(caplog)
    assert token not in text
    assert token.split(".")[1] not in text  # not even the JWT payload segment
    assert "user_alice" not in audit_text
    assert SECRET_ARG not in audit_text


def test_audit_handler_writes_json_line_to_stdout(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(create_app(settings), base_url="http://localhost:8000") as client:
        client.post("/mcp", json=_rpc("initialize", {"x": SECRET_ARG}), headers=AUTH_HEADERS)
    captured = capsys.readouterr()
    assert "mcp_request" not in captured.err  # not duplicated via the SDK's root handler
    lines = [json.loads(line) for line in captured.out.splitlines() if '"mcp_request"' in line]
    assert lines == [
        {
            "severity": "INFO",
            "logger": AUDIT_LOGGER_NAME,
            "event": "mcp_request",
            "method": "initialize",
            "target": None,
            "client_id": None,
            "status": 401,
            "batch_size": 1,
            "http_method": "POST",
        }
    ]
    assert SECRET_ARG not in captured.out
    assert FAKE_BEARER.split()[1] not in captured.out
