"""MCP tool and HTTP route adapters for questions, wired into test apps (not ``revel.app``).

The HTTP tests use F4's in-memory OpenFGA fake: ``TestClient`` runs the app on its own event
loop, which a real (aiohttp-based) OpenFGA client bound to the test loop cannot share. The
service behavior against real OpenFGA is covered in ``test_questions_service_db.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from authz_fakes import FakeFga
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from questions_support import (
    FGA_KIND,
    OPENFGA_TEST_API_URL,
    PURPOSE,
    REQUESTED,
    FakeMuse,
    World,
    agent_principal,
    build_world,
    proposal,
    requires_db,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from revel.api.questions import router
from revel.auth.authz import configure_authz, reset_authz
from revel.auth.deps import current_principal
from revel.db.models import QuestionEvent
from revel.mcp import question_tools
from revel.services.questions import (
    QuestionDeps,
    configure_question_deps,
    reset_question_deps,
)
from revel.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, app_env="test", public_base_url="http://localhost:8000")


@pytest.fixture
def fake_world(factory: sessionmaker[Session]) -> Iterator[tuple[World, FakeFga]]:
    reset_authz()
    reset_question_deps()
    fake = FakeFga()
    configure_authz(fake)
    world = anyio.run(build_world, factory, fake)
    try:
        yield world, fake
    finally:
        reset_authz()
        reset_question_deps()


def http_app(settings: Settings, principal: Any) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.include_router(router)
    app.dependency_overrides[current_principal] = lambda: principal
    return app


# --- HTTP --------------------------------------------------------------------------------------


@requires_db
def test_http_submit_and_updates(
    factory: sessionmaker[Session], settings: Settings, fake_world: tuple[World, FakeFga]
) -> None:
    world, _ = fake_world
    fake_muse = FakeMuse(content=proposal("Which flowers does Chelsea like best?", ["r1", "r2"]))
    configure_question_deps(settings, QuestionDeps(sessionmaker=factory, muse=fake_muse.client()))

    body = {
        "handle": "@Chelsea",
        "requested_information": REQUESTED,
        "private_purpose": PURPOSE,
        "expires_in_hours": 48,
    }
    with TestClient(http_app(settings, agent_principal(world.requester_d, via="api"))) as c:
        response = c.post("/api/questions", json=body)
    assert response.status_code == 201, response.text
    data = response.json()
    assert set(data) == {
        "question_id",
        "status",
        "expires_at",
        "deadline_clamped",
        "minimized_question",
        "routing",
        "profile_context",
    }
    assert data["routing"] == {"routing_step": 2, "respondent_count": 2, "minimizer": "model"}
    assert data["profile_context"]["tier"] == "connections"

    with TestClient(http_app(settings, agent_principal(world.owner_d1, via="api"))) as c:
        updates = c.get("/api/updates")
    assert updates.status_code == 200
    items = updates.json()["items"]
    assert len(items) == 1
    assert items[0]["type"] == "question_assignment"
    assert items[0]["minimized_question"] == "Which flowers does Chelsea like best?"
    text = updates.text
    for value in ["birthday", "peony", "$80", REQUESTED]:
        assert value not in text


@requires_db
@pytest.mark.parametrize(
    ("who", "body", "status", "code"),
    [
        ("stranger", {}, 403, "not_permitted"),
        ("owner", {}, 403, "not_permitted"),
        (
            "requester",
            {"requested_information": "What does she want for her birthday?"},
            422,
            "unsafe_requested_information",
        ),
    ],
)
def test_http_errors(
    factory: sessionmaker[Session],
    settings: Settings,
    fake_world: tuple[World, FakeFga],
    who: str,
    body: dict[str, Any],
    status: int,
    code: str,
) -> None:
    world, _ = fake_world
    configure_question_deps(settings, QuestionDeps(sessionmaker=factory))
    delegation = {
        "stranger": world.stranger_d,
        "owner": world.owner_d1,
        "requester": world.requester_d,
    }[who]
    payload = {
        "handle": "chelsea",
        "requested_information": REQUESTED,
        "private_purpose": PURPOSE,
        **body,
    }
    with TestClient(http_app(settings, agent_principal(delegation, via="api"))) as c:
        response = c.post("/api/questions", json=payload)
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    for value in ["30th", "peony", "lake house"]:
        assert value not in response.text


@requires_db
def test_http_rate_limit_retry_after(
    factory: sessionmaker[Session], settings: Settings, fake_world: tuple[World, FakeFga]
) -> None:
    from revel.services.questions import RATE_LIMIT_MAX_QUESTIONS

    world, _ = fake_world
    configure_question_deps(settings, QuestionDeps(sessionmaker=factory))
    payload = {"handle": "chelsea", "requested_information": REQUESTED}
    with TestClient(http_app(settings, agent_principal(world.requester_d, via="api"))) as c:
        for _ in range(RATE_LIMIT_MAX_QUESTIONS):
            assert c.post("/api/questions", json=payload).status_code == 201
        response = c.post("/api/questions", json=payload)
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "rate_limited"
    assert int(response.headers["retry-after"]) > 0


def test_http_requires_bearer(settings: Settings) -> None:
    app = FastAPI()
    app.state.settings = settings
    app.include_router(router)
    with TestClient(app) as c:
        assert c.get("/api/updates").status_code == 401
        assert c.post("/api/questions", json={}).status_code == 401


# --- MCP ---------------------------------------------------------------------------------------


def mcp_server(settings: Settings, monkeypatch: pytest.MonkeyPatch, principal: Any) -> MCPServer:
    async def fake_principal(runtime: Any) -> Any:
        return principal

    monkeypatch.setattr(question_tools, "principal_from_mcp_context", fake_principal)
    monkeypatch.setattr(question_tools, "runtime_for", lambda s: None)
    server = MCPServer(name="test")
    question_tools.register(server, settings)
    return server


@pytest.mark.anyio
async def test_mcp_tools_listed(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    server = mcp_server(settings, monkeypatch, None)
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools) == {"submit_question", "get_updates"}
    assert "private_purpose" in tools["submit_question"].input_schema["properties"]
    assert tools["get_updates"].input_schema.get("properties", {}) == {}
    assert tools["submit_question"].output_schema is not None


@pytest.mark.anyio
@requires_db
async def test_mcp_submit_and_get_updates(
    factory: sessionmaker[Session],
    settings: Settings,
    fga: Any,
    world_builder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await world_builder()
    fake_muse = FakeMuse(content=proposal("Which flowers does Chelsea like best?", ["r1", "r2"]))
    configure_question_deps(settings, QuestionDeps(sessionmaker=factory, muse=fake_muse.client()))

    asker = mcp_server(settings, monkeypatch, agent_principal(world.requester_d))
    result = await asker.call_tool(
        "submit_question",
        {"handle": "chelsea", "requested_information": REQUESTED, "private_purpose": PURPOSE},
    )
    submitted = result.structured_content
    assert submitted is not None
    assert submitted["routing"]["respondent_count"] == 2

    with pytest.raises(ToolError, match=r"unsafe_requested_information: "):
        await asker.call_tool(
            "submit_question",
            {
                "handle": "chelsea",
                "requested_information": "Would she like a peony bouquet?",
                "private_purpose": PURPOSE,
            },
        )
    with pytest.raises(ToolError, match=r"invalid_request: ") as exc:
        await asker.call_tool(
            "submit_question",
            {
                "requested_information": REQUESTED,
                "private_purpose": PURPOSE,
                "expires_in_hours": 5,
            },
        )
    assert "peony" not in str(exc.value)

    responder = mcp_server(settings, monkeypatch, agent_principal(world.owner_d2))
    updates = await responder.call_tool("get_updates", {})
    assert updates.structured_content is not None
    items = updates.structured_content["items"]
    assert len(items) == 1
    assert items[0]["question_id"] == submitted["question_id"]
    assert items[0]["requester"] == {"display_name": "Leo Example", "handle": "leo"}
    blob = json.dumps(updates.structured_content)
    for value in ["birthday", "peony", "$80", "pottery", REQUESTED]:
        assert value not in blob
    with factory() as s:
        kinds = [e.event_type for e in s.scalars(select(QuestionEvent))]
    assert kinds.count("retrieved") == 1

    stranger = mcp_server(settings, monkeypatch, agent_principal(world.stranger_d))
    with pytest.raises(ToolError, match=r"not_permitted: "):
        await stranger.call_tool(
            "submit_question", {"handle": "chelsea", "requested_information": REQUESTED}
        )
    assert (await stranger.call_tool("get_updates", {})).structured_content == {"items": []}


def test_real_openfga_used_when_configured() -> None:
    """Guard against silently testing only the fake when OpenFGA was requested."""
    if OPENFGA_TEST_API_URL:
        assert FGA_KIND == "openfga"


def test_composition_registers_tools_and_routes(settings: Settings) -> None:
    from revel.app import create_app

    app = create_app(settings)
    paths = set(app.openapi()["paths"])
    assert {"/api/questions", "/api/updates"} <= paths
    tools = anyio.run(app.state.mcp_server.list_tools)
    assert {"submit_question", "get_updates", "whoami"} <= {t.name for t in tools}
