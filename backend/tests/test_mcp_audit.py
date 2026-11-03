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
from revel.mcp.audit import AUDIT_LOGGER_NAME, McpAuditMiddleware
from revel.settings import Settings

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
SECRET_ARG = "s3cr3t-argument-value"
SECRET_QUERY = "q=private-query-value"


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
    yield
    audit_logger.removeHandler(caplog.handler)


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME]


def _audit(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    lines = [json.loads(r.getMessage()) for r in _audit_records(caplog)]
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
    """Downstream ASGI app that records the exact request it received and answers 200."""

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


def _post(app: Any, content: bytes, path: str = "/mcp") -> int:
    with TestClient(app, base_url="http://localhost:8000") as client:
        return client.post(path, content=content, headers=MCP_HEADERS).status_code


def _rpc(method: str, params: dict[str, Any] | None = None, id_: int = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        message["params"] = params
    return message


def test_tools_call_logs_name_not_arguments(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    recorder = Recorder()
    body = json.dumps(
        _rpc("tools/call", {"name": "save_profile", "arguments": {"note": SECRET_ARG}})
    ).encode()
    assert _post(McpAuditMiddleware(recorder, "/mcp"), body) == 200
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
    assert SECRET_ARG not in _all_log_text(caplog)
    assert recorder.body == body


def test_prompts_get_logs_prompt_name(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    body = json.dumps(
        _rpc("prompts/get", {"name": "revel_onboarding", "arguments": {"x": SECRET_ARG}})
    ).encode()
    _post(McpAuditMiddleware(Recorder(), "/mcp"), body)
    [line] = _audit(caplog)
    assert (line["method"], line["target"]) == ("prompts/get", "revel_onboarding")
    assert SECRET_ARG not in _all_log_text(caplog)


@pytest.mark.parametrize(
    ("uri", "target"),
    [
        (f"revel://guidance/index?{SECRET_QUERY}#frag", "revel://guidance/index"),
        (f"https://user:pw@example.com:8443/a/b?{SECRET_QUERY}", "https://example.com/a/b"),
        ("revel://guidance/index", "revel://guidance/index"),
    ],
)
def test_resources_read_drops_query(
    caplog: pytest.LogCaptureFixture, uri: str, target: str
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    _post(
        McpAuditMiddleware(Recorder(), "/mcp"),
        json.dumps(_rpc("resources/read", {"uri": uri})).encode(),
    )
    [line] = _audit(caplog)
    assert (line["method"], line["target"]) == ("resources/read", target)
    text = _all_log_text(caplog)
    assert SECRET_QUERY not in text
    assert "pw" not in json.dumps(line)


def test_batch_logs_one_line_per_message(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    batch = [
        _rpc("tools/list", {}, 1),
        _rpc("tools/call", {"name": "ping", "arguments": {"a": SECRET_ARG}}, 2),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 7, "result": {"secret": SECRET_ARG}},
    ]
    recorder = Recorder()
    body = json.dumps(batch).encode()
    _post(McpAuditMiddleware(recorder, "/mcp"), body)
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
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    recorder = Recorder()
    body = b'{"jsonrpc": "2.0", "method": "tools/call", "params": {"x": "' + SECRET_ARG.encode()
    _post(McpAuditMiddleware(recorder, "/mcp"), body)
    [line] = _audit(caplog)
    assert line["method"] == "unparsed"
    assert line["target"] is None
    assert recorder.body == body
    assert SECRET_ARG not in _all_log_text(caplog)


def test_oversized_body_passes_through(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    recorder = Recorder()
    body = json.dumps(_rpc("tools/call", {"name": "ping", "arguments": {"x": "y" * 5000}})).encode()
    _post(McpAuditMiddleware(recorder, "/mcp", max_body_bytes=1024), body)
    [line] = _audit(caplog)
    assert (line["method"], line["status"]) == ("unparsed", 200)
    assert recorder.body == body


@pytest.mark.anyio
async def test_oversized_chunked_body_replayed_in_order(caplog: pytest.LogCaptureFixture) -> None:
    """No Content-Length: buffered chunks are replayed, then the rest is streamed through."""
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
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
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}
    await McpAuditMiddleware(recorder, "/mcp", max_body_bytes=1000)(scope, receive, send)
    assert recorder.body == b"".join(chunks)
    assert [m.get("body") for m in recorder.messages] == chunks
    assert [line["method"] for line in _audit(caplog)] == ["unparsed"]
    assert sent[0]["status"] == 200


def test_non_post_logs_null_method(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    with TestClient(McpAuditMiddleware(Recorder(405), "/mcp")) as client:
        client.get("/mcp")
        client.delete("/mcp")
    lines = _audit(caplog)
    assert [(line["method"], line["http_method"], line["status"]) for line in lines] == [
        (None, "GET", 405),
        (None, "DELETE", 405),
    ]


def test_other_paths_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    _post(McpAuditMiddleware(Recorder(), "/mcp"), b"{}", path="/other")
    assert _audit(caplog) == []


# --- through the real app (SDK bearer auth + transport security) ---------------------------


def test_unauthenticated_401_logged_without_client_id(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    with TestClient(create_app(settings), base_url="http://localhost:8000") as client:
        response = client.post("/mcp", json=_rpc("tools/list", {}), headers=MCP_HEADERS)
    assert response.status_code == 401
    [line] = _audit(caplog)
    assert line == {
        "event": "mcp_request",
        "method": "tools/list",
        "target": None,
        "client_id": None,
        "status": 401,
        "batch_size": 1,
        "http_method": "POST",
    }


def test_authenticated_call_logs_client_id_only(
    settings: Settings, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    auth = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    call = _rpc("tools/call", {"name": "ping", "arguments": {}})
    with TestClient(create_app(settings), base_url="http://localhost:8000") as client:
        response = client.post("/mcp", json=call, headers=auth)
        assert response.status_code == 200, response.text
        assert response.json()["result"]["content"][0]["text"] == "pong"
        bad_host = client.post("/mcp", json=call, headers={**auth, "Host": "evil.example.com"})
        assert bad_host.status_code == 421
    lines = _audit(caplog)
    assert [
        (line["method"], line["target"], line["client_id"], line["status"]) for line in lines
    ] == [
        ("tools/call", "ping", "client_muse", 200),
        ("tools/call", "ping", "client_muse", 421),
    ]
    text = _all_log_text(caplog)
    assert token not in text
    assert token.split(".")[1] not in text  # not even the JWT payload segment
    assert "user_alice" not in json.dumps(lines)


def test_audit_handler_writes_json_line_to_stdout(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(create_app(settings), base_url="http://localhost:8000") as client:
        client.post("/mcp", json=_rpc("initialize", {"x": SECRET_ARG}), headers=MCP_HEADERS)
    captured = capsys.readouterr()
    out = captured.out
    assert "mcp_request" not in captured.err  # not duplicated via the SDK's root handler
    lines = [json.loads(line) for line in out.splitlines() if '"mcp_request"' in line]
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
    assert SECRET_ARG not in out
