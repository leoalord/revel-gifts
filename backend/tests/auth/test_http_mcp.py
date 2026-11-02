"""Discovery metadata, 401 challenges, GET /me and the MCP whoami tool, via F1's create_app."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver import MCPServer

from revel.app import create_app
from revel.auth.mcp_auth import mcp_auth_kwargs
from revel.auth.runtime import AuthRuntime
from revel.mcp.server import build_mcp_asgi_app
from revel.settings import Settings

from .conftest import ISSUER, RESOURCE, RESOURCE_METADATA_URL, InMemoryIdentityRepository

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
PRM = {
    "resource": RESOURCE,
    "authorization_servers": [ISSUER],
    "bearer_methods_supported": ["header"],
    "resource_name": "Revel Gifts",
}


def _rpc(client: TestClient, payload: dict[str, Any], token: str | None = None) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", json=payload, headers=headers)


def _call_tool(client: TestClient, token: str, name: str) -> dict[str, Any]:
    response = _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        },
        token,
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- discovery -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"]
)
def test_protected_resource_metadata(auth_client: TestClient, path: str) -> None:
    response = auth_client.get(path)
    assert response.status_code == 200
    assert response.json() == PRM


def test_served_prm_is_ours_and_matches_sdk(runtime: AuthRuntime, auth_settings: Settings) -> None:
    """The root router shadows the SDK's PRM route; content agrees with what the SDK would serve."""
    sdk_server = MCPServer(name="sdk-only", **mcp_auth_kwargs(auth_settings))
    sdk_app = build_mcp_asgi_app(sdk_server, auth_settings)
    with TestClient(sdk_app, base_url="http://localhost:8000") as sdk_client:
        sdk_prm = sdk_client.get("/.well-known/oauth-protected-resource/mcp").json()

    with TestClient(create_app(auth_settings), base_url="http://localhost:8000") as client:
        ours = client.get("/.well-known/oauth-protected-resource/mcp").json()

    assert ours["resource_name"] == "Revel Gifts"  # only our route sets this
    assert "resource_name" not in sdk_prm or sdk_prm["resource_name"] is None
    assert sdk_prm["resource"].rstrip("/") == ours["resource"]
    assert [s.rstrip("/") for s in sdk_prm["authorization_servers"]] == ours[
        "authorization_servers"
    ]
    assert sdk_prm.get("bearer_methods_supported") == ours["bearer_methods_supported"]


def test_authorization_server_metadata_proxied_and_cached(auth_client: TestClient, authkit) -> None:
    first = auth_client.get("/.well-known/oauth-authorization-server")
    second = auth_client.get("/.well-known/oauth-authorization-server")
    assert first.status_code == 200
    assert first.json()["issuer"] == ISSUER
    assert second.json() == first.json()


# --- 401 challenges ------------------------------------------------------------------


def test_me_without_token_401(auth_client: TestClient) -> None:
    response = auth_client.get("/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{RESOURCE_METADATA_URL}"'
    )


@pytest.mark.parametrize("header", ["Bearer not-a-jwt", "Bearer ", "Basic dXNlcjpwYXNz"])
def test_me_invalid_credentials_401(auth_client: TestClient, header: str) -> None:
    response = auth_client.get("/me", headers={"Authorization": header})
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{RESOURCE_METADATA_URL}"' in challenge
    if header == "Bearer not-a-jwt":
        assert 'error="invalid_token"' in challenge


@pytest.mark.parametrize("case", ["wrong-audience", "expired", "malformed"])
def test_rejected_tokens_401_on_me_and_mcp(auth_client: TestClient, make_token, case: str) -> None:
    now = int(time.time())
    token = {
        "wrong-audience": lambda: make_token(aud="https://elsewhere.test/mcp"),
        "expired": lambda: make_token(iat=now - 600, exp=now - 120),
        "malformed": lambda: "garbage",
    }[case]()
    me = auth_client.get("/me", headers=_bearer(token))
    assert me.status_code == 401
    assert 'error="invalid_token"' in me.headers["www-authenticate"]
    mcp = _rpc(auth_client, INITIALIZE, token)
    assert mcp.status_code == 401
    assert f'resource_metadata="{RESOURCE_METADATA_URL}"' in mcp.headers["www-authenticate"]


def test_mcp_initialize_without_token_401(auth_client: TestClient) -> None:
    response = _rpc(auth_client, INITIALIZE)
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'error="invalid_token"' in challenge
    assert f'resource_metadata="{RESOURCE_METADATA_URL}"' in challenge


def test_mcp_with_valid_token_ok(auth_client: TestClient, make_token) -> None:
    response = _rpc(auth_client, INITIALIZE, make_token())
    assert response.status_code == 200, response.text


# --- whoami ----------------------------------------------------------------------------


def test_me_returns_whoami(
    auth_client: TestClient, make_token, repo: InMemoryIdentityRepository
) -> None:
    response = auth_client.get("/me", headers=_bearer(make_token()))
    assert response.status_code == 200, response.text
    (user,) = repo.users.values()
    (agent,) = repo.agents.values()
    (delegation,) = repo.delegations.values()
    assert response.json() == {
        "user": {
            "id": str(user.id),
            "workos_user_id": "user_alice",
            "email": "alice@revel.test",
            "display_name": "Alice Liddell",
        },
        "agent": {
            "agent_id": str(agent.id),
            "delegation_id": str(delegation.id),
            "client_id": "client_muse",
            "name": None,
            "label": "Assistant",
        },
        "via": "api",
    }


def test_whoami_mcp_and_me_identical(auth_client: TestClient, make_token) -> None:
    token = make_token()
    me = auth_client.get("/me", headers=_bearer(token)).json()
    result = _call_tool(auth_client, token, "whoami")
    assert result.get("isError") is not True, result
    structured = result["structuredContent"]
    assert structured["via"] == "mcp"
    assert me["via"] == "api"
    # Identical apart from the transport marker.
    assert {**structured, "via": None} == {**me, "via": None}


def test_disconnected_delegation_rejected(
    auth_client: TestClient, make_token, repo: InMemoryIdentityRepository
) -> None:
    token = make_token()
    assert auth_client.get("/me", headers=_bearer(token)).status_code == 200
    (delegation,) = repo.delegations.values()
    repo.disconnect(delegation.id)

    result = _call_tool(auth_client, token, "whoami")
    assert result["isError"] is True
    assert "disconnected" in result["content"][0]["text"]

    response = auth_client.get("/me", headers=_bearer(token))
    assert response.status_code == 403

    (after,) = repo.delegations.values()
    assert after.id == delegation.id
    assert after.status == "disconnected"


def test_whoami_tool_listed_read_only(auth_client: TestClient, make_token) -> None:
    listed = _rpc(auth_client, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, make_token())
    tools = {t["name"]: t for t in listed.json()["result"]["tools"]}
    assert tools["whoami"]["annotations"]["readOnlyHint"] is True
    assert "authenticated Revel user" in tools["whoami"]["description"]
