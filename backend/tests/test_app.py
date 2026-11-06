from typing import Any

import httpx
import httpx2
import pytest
from auth.authkit import TestAuthKit
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


@pytest.fixture
def settings() -> Settings:
    """Test settings with the mocked AuthKit (tests/auth/authkit.py) installed.

    The MCP endpoint requires a WorkOS AuthKit bearer token (F2). The app's real verifier
    is used; only its outbound HTTP client is mocked to serve a local test JWKS.
    """
    settings = TestAuthKit.settings(public_base_url="http://localhost:8000")
    TestAuthKit().install(settings)
    return settings


@pytest.fixture
def token(settings: Settings) -> str:
    return TestAuthKit.token(settings)


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def _rpc(client: TestClient, payload: dict[str, Any], **headers: str) -> httpx.Response:
    return client.post("/mcp", json=payload, headers={**MCP_HEADERS, **headers})


def test_mcp_raw_jsonrpc_initialize_list_call(client: TestClient, token: str) -> None:
    auth = {"Authorization": f"Bearer {token}"}
    init = _rpc(client, INITIALIZE, **auth)
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "revel"

    tools = _rpc(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, **auth)
    assert tools.status_code == 200, tools.text
    names = [t["name"] for t in tools.json()["result"]["tools"]]
    assert "whoami" in names and "ping" not in names


def test_mcp_endpoint_exact_path_no_redirect(client: TestClient, token: str) -> None:
    response = client.post(
        "/mcp",
        json=INITIALIZE,
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
        follow_redirects=False,
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_mcp_sdk_client_over_streamable_http(app: FastAPI, token: str) -> None:
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://localhost:8000",
            headers={"Authorization": f"Bearer {token}"},
        ) as hc:
            streams = streamable_http_client("http://localhost:8000/mcp", http_client=hc)
            async with Client(streams) as mcp_client:
                tools = await mcp_client.list_tools()
                assert "whoami" in [t.name for t in tools.tools]


def test_mcp_unauthenticated_401(client: TestClient) -> None:
    response = _rpc(client, INITIALIZE)
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert (
        'resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )


def test_mcp_rejects_disallowed_host(client: TestClient, token: str) -> None:
    # Bearer auth runs before the transport's Host check, so this needs a valid token.
    response = _rpc(client, INITIALIZE, Host="evil.example.com", Authorization=f"Bearer {token}")
    assert response.status_code == 421


def test_mcp_unauthenticated_disallowed_host_401(client: TestClient) -> None:
    response = _rpc(client, INITIALIZE, Host="evil.example.com")
    assert response.status_code == 401


def test_mcp_rejects_disallowed_origin(client: TestClient, token: str) -> None:
    response = _rpc(
        client, INITIALIZE, Origin="https://evil.example.com", Authorization=f"Bearer {token}"
    )
    assert response.status_code == 403


def test_mcp_allows_public_origin_in_production() -> None:
    settings = TestAuthKit.settings(
        app_env="production",
        public_base_url="https://api.revel.gifts",
        allowed_hosts="revel-abc123-uc.a.run.app",
    )
    TestAuthKit().install(settings)
    auth = {"Authorization": f"Bearer {TestAuthKit.token(settings)}"}
    app = create_app(settings)
    with TestClient(app, base_url="https://api.revel.gifts") as client:
        ok = _rpc(client, INITIALIZE, Origin="https://api.revel.gifts", **auth)
        assert ok.status_code == 200, ok.text
        cloud_run = _rpc(client, INITIALIZE, Host="revel-abc123-uc.a.run.app", **auth)
        assert cloud_run.status_code == 200, cloud_run.text
        # localhost is only allowed in development/test.
        assert _rpc(client, INITIALIZE, Host="localhost:8000", **auth).status_code == 421


def test_root_routes_not_shadowed_by_mcp_mount(
    settings: Settings, token: str, monkeypatch: pytest.MonkeyPatch
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
        assert _rpc(client, INITIALIZE, Authorization=f"Bearer {token}").status_code == 200


def test_mcp_resource_url(settings: Settings) -> None:
    assert settings.mcp_resource_url == "http://localhost:8000/mcp"
    custom = Settings(_env_file=None, workos_token_audience="https://api.revel.gifts/mcp")
    assert custom.mcp_resource_url == "https://api.revel.gifts/mcp"


def test_health_matches_healthz(client: TestClient) -> None:
    for path in ("/health", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_settings_repr_hides_database_password() -> None:
    password = "s3cr3t-db-password"
    settings = Settings(
        _env_file=None,
        database_url=f"postgresql+psycopg://revel_app:{password}@/revel?host=/cloudsql/p:r:i",
    )
    assert password not in repr(settings)
    assert password not in str(settings)
    assert "database_url" not in repr(settings)
    # The type consumed by F3 is unchanged.
    assert isinstance(settings.database_url, str)
    assert password in settings.database_url


@pytest.mark.parametrize("url", ["http://api.revel.gifts", "api.revel.gifts", ""])
def test_production_requires_https_public_base_url(url: str) -> None:
    settings = Settings(_env_file=None, app_env="production", public_base_url=url)
    with pytest.raises(ValueError, match="https"):
        create_app(settings)


def test_development_allows_http_public_base_url() -> None:
    settings = Settings(_env_file=None, app_env="development", public_base_url="http://localhost:1")
    assert create_app(settings).state.settings is settings
