from typing import Any

import httpx
import httpx2
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import revel.app
from revel.app import create_app
from revel.settings import Settings

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def _rpc(client: TestClient, payload: dict[str, Any], **headers: str) -> httpx.Response:
    return client.post("/mcp", json=payload, headers={**MCP_HEADERS, **headers})


def test_mcp_raw_jsonrpc_initialize_list_call(client: TestClient) -> None:
    init = _rpc(client, INITIALIZE)
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "revel"

    tools = _rpc(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert tools.status_code == 200, tools.text
    assert [t["name"] for t in tools.json()["result"]["tools"]] == ["ping"]

    call = _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        },
    )
    assert call.status_code == 200, call.text
    assert call.json()["result"]["content"][0]["text"] == "pong"


def test_mcp_endpoint_exact_path_no_redirect(client: TestClient) -> None:
    response = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS, follow_redirects=False)
    assert response.status_code == 200


@pytest.mark.anyio
async def test_mcp_sdk_client_over_streamable_http(app: FastAPI) -> None:
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8000") as hc:
            streams = streamable_http_client("http://localhost:8000/mcp", http_client=hc)
            async with Client(streams) as mcp_client:
                tools = await mcp_client.list_tools()
                assert [t.name for t in tools.tools] == ["ping"]
                result = await mcp_client.call_tool("ping", {})
                assert result.content[0].text == "pong"


def test_mcp_rejects_disallowed_host(client: TestClient) -> None:
    response = _rpc(client, INITIALIZE, Host="evil.example.com")
    assert response.status_code == 421


def test_mcp_rejects_disallowed_origin(client: TestClient) -> None:
    response = _rpc(client, INITIALIZE, Origin="https://evil.example.com")
    assert response.status_code == 403


def test_mcp_allows_public_origin_in_production() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        public_base_url="https://api.revel.gifts",
        allowed_hosts="revel-abc123-uc.a.run.app",
    )
    app = create_app(settings)
    with TestClient(app, base_url="https://api.revel.gifts") as client:
        ok = _rpc(client, INITIALIZE, Origin="https://api.revel.gifts")
        assert ok.status_code == 200, ok.text
        cloud_run = _rpc(client, INITIALIZE, Host="revel-abc123-uc.a.run.app")
        assert cloud_run.status_code == 200, cloud_run.text
        # localhost is only allowed in development/test.
        assert _rpc(client, INITIALIZE, Host="localhost:8000").status_code == 421


def test_root_routes_not_shadowed_by_mcp_mount(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = APIRouter()

    @router.get("/.well-known/oauth-protected-resource/mcp")
    async def metadata() -> dict[str, str]:
        return {"served_by": "fastapi"}

    monkeypatch.setattr(revel.app, "routers", lambda: [router])
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost:8000") as client:
        response = client.get("/.well-known/oauth-protected-resource/mcp")
        assert response.status_code == 200
        assert response.json() == {"served_by": "fastapi"}
        assert client.get("/healthz").status_code == 200
        assert _rpc(client, INITIALIZE).status_code == 200


def test_mcp_resource_url(settings: Settings) -> None:
    assert settings.mcp_resource_url == "http://localhost:8000/mcp"
    custom = Settings(_env_file=None, workos_token_audience="https://api.revel.gifts/mcp")
    assert custom.mcp_resource_url == "https://api.revel.gifts/mcp"
