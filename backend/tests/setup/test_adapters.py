"""Transport authorization and error contracts for polling setup."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from revel.auth.identity import Principal
from revel.services import inbox_setup as svc


def test_browser_and_bearer_setup_require_authentication(client):
    for path in ("/api/setup", "/api/v1/setup"):
        assert client.get(path).status_code in (401, 503)
        result = client.put(path, json={"expected_revision": 0, "enabled": False})
        assert result.status_code in (401, 403, 503)


def test_browser_write_still_requires_same_origin(client, app, monkeypatch):
    from revel.auth import ui
    from revel.auth.ui import current_ui_principal

    monkeypatch.setattr(
        ui,
        "require_browser",
        lambda runtime: SimpleNamespace(
            config=SimpleNamespace(
                public_origin="http://localhost:8000", extra_origins=(), development=True
            )
        ),
    )

    app.dependency_overrides[current_ui_principal] = lambda: Principal(uuid4(), None, None, "ui")
    called = []
    monkeypatch.setattr(svc, "configure", lambda *args: called.append(args))
    result = client.put(
        "/api/setup",
        headers={"Origin": "https://untrusted.example.com"},
        json={"expected_revision": 0, "enabled": False},
    )
    assert result.status_code == 403 and not called


@pytest.mark.anyio
async def test_service_refusal_uses_existing_error_contract(monkeypatch):
    from revel.api import setup

    def refuse(*args):
        raise svc.SetupError("stale_revision", "Read current setup.")

    monkeypatch.setattr(setup, "get_sessionmaker", lambda: None)
    result = await setup._call(refuse, Principal(uuid4(), None, None, "ui"))
    assert result.status_code == 409
    assert result.body == b'{"code":"stale_revision","detail":"Read current setup."}'


@pytest.mark.anyio
async def test_mcp_schedule_requires_authenticated_principal_and_preserves_owner_flag(
    settings, monkeypatch
):
    from revel.mcp import setup_tools
    from revel.mcp.server import create_mcp_server

    server = create_mcp_server(settings)
    with pytest.raises(ToolError, match="Authentication required"):
        await server.call_tool("get_setup", {})
    owner, agent, delegation = uuid4(), uuid4(), uuid4()

    async def principal(runtime):
        return Principal(owner, agent, delegation, "mcp")

    monkeypatch.setattr(setup_tools, "principal_from_mcp_context", principal)
    monkeypatch.setattr(setup_tools, "get_sessionmaker", lambda: None)
    captured = []

    def configure(actor, body, factory):
        captured.append(body)
        raise svc.SetupError("owner_confirmation_required", "Ask the owner.", 403)

    monkeypatch.setattr(svc, "configure", configure)
    with pytest.raises(ToolError, match="owner_confirmation_required"):
        await server.call_tool(
            "configure_inbox_checks",
            {
                "action": "schedule",
                "expected_revision": 0,
                "enabled": True,
                "delegation_id": str(delegation),
                "timezone": "UTC",
            },
        )
    assert captured[0].owner_confirmed is False
