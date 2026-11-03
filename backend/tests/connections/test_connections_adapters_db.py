"""F6 HTTP routes and MCP tools: thin adapters, identical hidden-target responses, safe errors,
CSRF-guarded writes, and an end-to-end flow on a real OpenFGA server."""

import os
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from authz_fakes import FakeFga
from conn_helpers import Person, add_person, seed_fga
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from revel.api import connections as api
from revel.app import create_app
from revel.auth import authz, fga_bootstrap, fga_reconcile
from revel.auth.authz import ViewerTier, resolve_viewer_tier
from revel.auth.fga import create_fga_client
from revel.auth.identity import Principal
from revel.auth.ui import current_ui_principal, same_origin_ui_principal
from revel.db.models import Profile
from revel.mcp import connection_tools
from revel.services import connections as svc
from revel.settings import Settings

ORIGIN = {"Origin": "http://localhost:8000"}
OPENFGA_TEST_API_URL = os.environ.get("OPENFGA_TEST_API_URL", "")


# --- HTTP -------------------------------------------------------------------------------------


class Acting:
    """Which UI principal the overridden session dependency returns."""

    principal: Principal | None = None


@pytest.fixture
def http(
    settings: Settings,
    factory: sessionmaker[Session],
    fake_fga: FakeFga,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Acting]]:
    monkeypatch.setattr(svc, "_default_sessionmaker", lambda: factory)
    app: FastAPI = create_app(settings)
    acting = Acting()

    def principal() -> Principal:
        assert acting.principal is not None
        return acting.principal

    app.dependency_overrides[current_ui_principal] = principal
    app.dependency_overrides[same_origin_ui_principal] = principal
    with TestClient(app, base_url="http://localhost:8000") as client:
        authz.configure_authz(fake_fga)  # the lifespan installed an unconfigured client
        yield client, acting
    authz.reset_authz()


def test_every_unsafe_route_is_same_origin_guarded() -> None:
    for route in api.router.routes:
        methods = getattr(route, "methods", set())
        calls = {d.call for d in route.dependant.dependencies}
        if methods & {"POST", "PUT", "PATCH", "DELETE"}:
            assert same_origin_ui_principal in calls, route.path
        else:
            assert current_ui_principal in calls, route.path


def test_http_flow_and_identical_hidden_responses(
    http: tuple[TestClient, Acting], session: Session, fake_fga: FakeFga
) -> None:
    client, acting = http
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    add_person(session, "hidden", discoverable=False)
    blocker = add_person(session, "blocker")
    seed_fga(fake_fga, sam, alex, blocker)

    acting.principal = sam.ui
    r = client.post("/api/connections/requests", json={"handle": "blocker_demo"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    acting.principal = blocker.ui
    assert client.post(f"/api/connections/requests/{r.json()['request']['id']}/block").json()[
        "status"
    ] == ("blocked")

    acting.principal = sam.ui
    hidden = [
        client.post("/api/connections/requests", json={"handle": h})
        for h in ("nobody_here", "hidden_demo", "blocker_demo", "sam_demo", "x")
    ]
    expected = {"detail": svc.TargetNotFound.message, "code": "target_not_found"}
    assert {(h.status_code, h.content) for h in hidden} == {(404, hidden[0].content)}
    assert hidden[0].json() == expected

    created = client.post("/api/connections/requests", json={"handle": "@Alex_Demo"})
    assert created.status_code == 200
    body = created.json()
    assert body["target"] == {
        "display_name": "Alex",
        "handle": "alex_demo",
        "avatar_url": "https://example.test/alex.png",
    }
    rid = body["request"]["id"]

    # The requester cannot approve their own request; it looks unknown.
    bad = client.post(f"/api/connections/requests/{rid}/approve", json={"tier": "connections"})
    assert bad.status_code == 404 and bad.json()["code"] == "request_not_found"

    acting.principal = alex.ui
    incoming = client.get("/api/connections/requests/incoming").json()
    assert [i["id"] for i in incoming] == [rid]
    assert client.post(
        f"/api/connections/requests/{rid}/approve", json={"tier": "only_me"}
    ).status_code == (422)
    ok = client.post(f"/api/connections/requests/{rid}/approve", json={"tier": "connections"})
    assert ok.json() == {"request_id": rid, "status": "approved", "tier": "connections"}
    again = client.post(f"/api/connections/requests/{rid}/approve", json={"tier": "connections"})
    assert again.status_code == 409 and again.json()["code"] == "invalid_state"
    conns = client.get("/api/connections").json()
    assert [(c["request_id"], c["tier"]) for c in conns] == [(rid, "connections")]
    tier = client.post(f"/api/connections/requests/{rid}/tier", json={"tier": "trusted_circle"})
    assert tier.json()["tier"] == "trusted_circle"
    assert client.post(f"/api/connections/requests/{rid}/revoke").json()["status"] == "revoked"
    assert client.get("/api/connections").json() == []
    assert client.get("/api/connections/blocked").json() == []

    handle = client.put("/api/connections/handle", json={"discoverable": False})
    assert handle.json() == {
        "handle": "alex_demo",
        "discoverable": False,
        "avatar_url": "https://example.test/alex.png",
    }
    assert client.get("/api/connections/handle").json()["discoverable"] is False
    taken = client.put("/api/connections/handle", json={"handle": "sam_demo"})
    assert taken.status_code == 409 and taken.json()["code"] == "handle_unavailable"


def test_http_unexpected_errors_are_generic(
    http: tuple[TestClient, Acting], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, acting = http
    acting.principal = add_person(session, "sam").ui

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("internal detail profile:1234 should never leak")

    monkeypatch.setattr(svc, "list_incoming", boom)
    r = client.get("/api/connections/requests/incoming")
    assert r.status_code == 503
    assert r.json() == {"detail": svc.ConnectionsUnavailable.message, "code": "unavailable"}


# --- MCP --------------------------------------------------------------------------------------


@pytest.fixture
def mcp_server(
    settings: Settings,
    factory: sessionmaker[Session],
    fake_fga: FakeFga,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Acting]:
    from revel.mcp.server import create_mcp_server

    monkeypatch.setattr(svc, "_default_sessionmaker", lambda: factory)
    acting = Acting()

    async def principal(_runtime: Any) -> Principal:
        assert acting.principal is not None
        return acting.principal

    monkeypatch.setattr(connection_tools, "principal_from_mcp_context", principal)
    return create_mcp_server(settings), acting


def _structured(result: Any) -> Any:
    assert not result.is_error, result
    return result.structured_content


@pytest.mark.anyio
async def test_mcp_tools_flow_and_identical_hidden_errors(
    mcp_server: tuple[Any, Acting], session: Session, fake_fga: FakeFga
) -> None:
    server, acting = mcp_server
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    add_person(session, "hidden", discoverable=False)
    seed_fga(fake_fga, sam, alex)

    acting.principal = sam.agent
    messages = set()
    for handle in ("nobody_here", "hidden_demo", "sam_demo"):
        try:
            result = await server.call_tool("request_connection", {"handle": handle})
        except ToolError as exc:
            messages.add(str(exc))
        else:
            assert result.is_error
            messages.add(result.content[0].text)
    assert len(messages) == 1 and svc.TargetNotFound.message in messages.pop()

    req = _structured(await server.call_tool("request_connection", {"handle": "alex_demo"}))
    rid = req["request"]["id"]

    acting.principal = alex.agent
    listed = _structured(await server.call_tool("list_connections", {"include": "requests"}))
    assert set(listed) == {"incoming", "outgoing"}
    assert [r["id"] for r in listed["incoming"]] == [rid]
    assert listed["incoming"][0]["requester"]["assistant"]["client_id"] == "client_muse"
    approved = _structured(
        await server.call_tool(
            "respond_to_connection_request",
            {
                "request_id": rid,
                "action": "approve",
                "tier": "trusted_circle",
                "owner_confirmed": True,
            },
        )
    )
    assert approved == {"request_id": rid, "status": "approved", "tier": "trusted_circle"}
    assert (
        await resolve_viewer_tier(fake_fga, sam.agent, alex.profile.id)
    ) is ViewerTier.trusted_circle
    conns = _structured(await server.call_tool("list_connections", {"include": "connections"}))
    assert set(conns) == {"connections"}
    assert conns["connections"][0]["tier"] == "trusted_circle"
    changed = _structured(
        await server.call_tool(
            "respond_to_connection_request",
            {"request_id": rid, "action": "change_tier", "tier": "connections"},
        )
    )
    assert changed["tier"] == "connections"
    revoked = _structured(
        await server.call_tool("end_connection", {"request_id": rid, "action": "revoke"})
    )
    assert revoked["status"] == "revoked"
    assert (await resolve_viewer_tier(fake_fga, sam.agent, alex.profile.id)) is ViewerTier.none

    acting.principal = sam.agent
    everything = _structured(await server.call_tool("list_connections", {}))
    assert everything == {"connections": [], "incoming": [], "outgoing": []}
    with pytest.raises(ToolError, match="Connection request not found"):
        await server.call_tool("end_connection", {"request_id": str(uuid4()), "action": "revoke"})


async def _tool_error_text(server: Any, name: str, args: dict[str, Any]) -> str:
    """The tool's own error message, without the SDK's ``Error executing tool <name>: `` prefix."""
    try:
        result = await server.call_tool(name, args)
    except ToolError as exc:
        text = str(exc)
    else:
        assert result.is_error, result
        text = result.content[0].text
    return text.removeprefix(f"Error executing tool {name}: ")


@pytest.mark.anyio
async def test_mcp_list_connections_include_variants(
    mcp_server: tuple[Any, Acting], session: Session, fake_fga: FakeFga
) -> None:
    server, acting = mcp_server
    sam, alex, kim = (add_person(session, n) for n in ("sam", "alex", "kim"))
    seed_fga(fake_fga, sam, alex, kim)
    # sam -> alex approved (alex's connection), kim -> alex pending (alex's incoming),
    # alex -> kim pending (alex's outgoing).
    acting.principal = sam.agent
    approved = _structured(await server.call_tool("request_connection", {"handle": "alex_demo"}))
    acting.principal = kim.agent
    incoming = _structured(await server.call_tool("request_connection", {"handle": "alex_demo"}))
    acting.principal = alex.agent
    outgoing = _structured(await server.call_tool("request_connection", {"handle": "kim_demo"}))
    await server.call_tool(
        "respond_to_connection_request",
        {
            "request_id": approved["request"]["id"],
            "action": "approve",
            "tier": "connections",
            "owner_confirmed": True,
        },
    )

    full = _structured(await server.call_tool("list_connections", {}))
    assert set(full) == {"connections", "incoming", "outgoing"}
    assert [c["request_id"] for c in full["connections"]] == [approved["request"]["id"]]
    assert [r["id"] for r in full["incoming"]] == [incoming["request"]["id"]]
    assert [r["id"] for r in full["outgoing"]] == [outgoing["request"]["id"]]
    assert _structured(await server.call_tool("list_connections", {"include": "all"})) == full

    conns = _structured(await server.call_tool("list_connections", {"include": "connections"}))
    assert conns == {"connections": full["connections"]}
    reqs = _structured(await server.call_tool("list_connections", {"include": "requests"}))
    assert reqs == {"incoming": full["incoming"], "outgoing": full["outgoing"]}

    message = await _tool_error_text(server, "list_connections", {"include": "friends"})
    assert message == (
        "invalid_request: Unknown include 'friends' for list_connections; "
        "expected one of: connections, requests, all."
    )


@pytest.mark.anyio
async def test_mcp_end_connection_revoke_and_cancel(
    mcp_server: tuple[Any, Acting], session: Session, fake_fga: FakeFga
) -> None:
    server, acting = mcp_server
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    seed_fga(fake_fga, sam, alex)

    acting.principal = sam.agent
    first = _structured(await server.call_tool("request_connection", {"handle": "alex_demo"}))
    rid = first["request"]["id"]
    cancelled = _structured(
        await server.call_tool("end_connection", {"request_id": rid, "action": "cancel"})
    )
    assert cancelled["status"] == "cancelled"
    assert _structured(await server.call_tool("list_connections", {"include": "requests"})) == {
        "incoming": [],
        "outgoing": [],
    }

    again = _structured(await server.call_tool("request_connection", {"handle": "alex_demo"}))
    rid = again["request"]["id"]
    acting.principal = alex.agent
    await server.call_tool(
        "respond_to_connection_request",
        {"request_id": rid, "action": "approve", "tier": "connections", "owner_confirmed": True},
    )
    assert (
        await resolve_viewer_tier(fake_fga, sam.agent, alex.profile.id)
    ) is ViewerTier.connections
    revoked = _structured(
        await server.call_tool("end_connection", {"request_id": rid, "action": "revoke"})
    )
    assert revoked["status"] == "revoked"
    assert (await resolve_viewer_tier(fake_fga, sam.agent, alex.profile.id)) is ViewerTier.none

    message = await _tool_error_text(
        server, "end_connection", {"request_id": rid, "action": "delete"}
    )
    assert message == (
        "invalid_request: Unknown action 'delete' for end_connection; "
        "expected one of: revoke, cancel."
    )


@pytest.mark.anyio
async def test_mcp_respond_validates_per_action_params(
    mcp_server: tuple[Any, Acting], session: Session, fake_fga: FakeFga
) -> None:
    server, acting = mcp_server
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    seed_fga(fake_fga, sam, alex)
    acting.principal = sam.agent
    req = _structured(await server.call_tool("request_connection", {"handle": "alex_demo"}))
    rid = req["request"]["id"]
    acting.principal = alex.agent
    name = "respond_to_connection_request"

    async def error(args: dict[str, Any]) -> str:
        return await _tool_error_text(server, name, {"request_id": rid, **args})

    assert await error({"action": "accept"}) == (
        "invalid_request: Unknown action 'accept' for respond_to_connection_request; "
        "expected one of: approve, decline, block, change_tier."
    )
    for action in ("approve", "change_tier"):
        assert await error({"action": action, "owner_confirmed": True}) == (
            f"invalid_request: Action '{action}' requires tier."
        )
    for action in ("decline", "block"):
        assert await error({"action": action, "tier": "connections"}) == (
            f"invalid_request: Action '{action}' does not accept tier."
        )
        assert await error({"action": action, "owner_confirmed": True}) == (
            f"invalid_request: Action '{action}' does not accept owner_confirmed."
        )
    # Nothing above reached the service: the request is still pending and ungranted.
    pending = _structured(await server.call_tool("list_connections", {"include": "requests"}))
    assert [r["id"] for r in pending["incoming"]] == [rid]
    assert (await resolve_viewer_tier(fake_fga, sam.agent, alex.profile.id)) is ViewerTier.none

    declined = _structured(await server.call_tool(name, {"request_id": rid, "action": "decline"}))
    assert declined["status"] == "declined"


@pytest.mark.anyio
async def test_mcp_connection_tool_set_and_annotations(mcp_server: tuple[Any, Acting]) -> None:
    server, _ = mcp_server
    tools = {t.name: t for t in await server.list_tools()}
    for old in (
        "list_connection_requests",
        "change_connection_tier",
        "revoke_connection",
        "cancel_connection_request",
    ):
        assert old not in tools
    for name in (
        "set_handle",
        "request_connection",
        "list_connections",
        "respond_to_connection_request",
        "end_connection",
    ):
        assert name in tools

    listing = tools["list_connections"]
    assert listing.annotations.read_only_hint is True
    assert listing.annotations.destructive_hint is None
    include = listing.input_schema["properties"]["include"]
    assert include["enum"] == ["connections", "requests", "all"]
    assert include["default"] == "all"
    assert listing.input_schema.get("required", []) == []

    respond = tools["respond_to_connection_request"]
    assert respond.annotations.read_only_hint is False
    assert respond.annotations.destructive_hint is True
    assert respond.annotations.idempotent_hint is False
    props = respond.input_schema["properties"]
    assert props["action"]["enum"] == ["approve", "decline", "block", "change_tier"]
    assert set(respond.input_schema["required"]) == {"request_id", "action"}

    end = tools["end_connection"]
    assert end.annotations.read_only_hint is False
    assert end.annotations.destructive_hint is True
    assert end.input_schema["properties"]["action"]["enum"] == ["revoke", "cancel"]
    assert set(end.input_schema["required"]) == {"request_id", "action"}


# --- Real OpenFGA -----------------------------------------------------------------------------


def _openfga_reachable() -> bool:
    if not OPENFGA_TEST_API_URL:
        return False
    try:
        return httpx.get(f"{OPENFGA_TEST_API_URL}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.openfga
@pytest.mark.anyio
async def test_end_to_end_on_real_openfga(
    session: Session, sessions: Any, factory: sessionmaker[Session]
) -> None:
    if not _openfga_reachable():
        pytest.skip("OPENFGA_TEST_API_URL is not set or not reachable")
    base = Settings(_env_file=None, openfga_api_url=OPENFGA_TEST_API_URL)
    boot = await fga_bootstrap.bootstrap(base, store_name=f"revel-test-{uuid4()}")
    fga = create_fga_client(
        base.model_copy(
            update={
                "openfga_store_id": boot.store_id,
                "openfga_authorization_model_id": boot.authorization_model_id,
            }
        )
    )
    authz.configure_authz(fga)
    try:
        sam, alex = add_person(session, "sam"), add_person(session, "alex")
        await fga_reconcile.reconcile(fga, lambda: factory, window=timedelta(0))

        async def tier(viewer: Principal, owner: Person) -> ViewerTier:
            assert owner.profile is not None
            return await resolve_viewer_tier(fga, viewer, owner.profile.id)

        req = await svc.lookup_and_request(sam.agent, handle="alex_demo", sessions=sessions)
        rid = req.request.id
        await svc.approve(
            alex.agent, rid, tier="connections", owner_confirmed=True, sessions=sessions
        )
        assert await tier(sam.ui, alex) is ViewerTier.connections
        assert await tier(sam.agent, alex) is ViewerTier.connections
        assert await tier(alex.ui, sam) is ViewerTier.none  # directional

        await svc.change_tier(alex.ui, rid, tier="trusted_circle", sessions=sessions)
        assert await tier(sam.agent, alex) is ViewerTier.trusted_circle
        await svc.change_tier(alex.ui, rid, tier="connections", sessions=sessions)
        assert await tier(sam.agent, alex) is ViewerTier.connections

        await svc.block(alex.ui, rid, sessions=sessions)
        assert await tier(sam.agent, alex) is ViewerTier.none
        await svc.unblock(alex.ui, rid, sessions=sessions)
        assert await tier(sam.agent, alex) is ViewerTier.none  # no tier restored

        again = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
        await svc.approve(alex.ui, again.request.id, tier="trusted_circle", sessions=sessions)
        assert await tier(sam.ui, alex) is ViewerTier.trusted_circle
        await svc.revoke(alex.ui, again.request.id, sessions=sessions)
        assert await tier(sam.ui, alex) is ViewerTier.none

        # Reconcile agrees with the service's writes (nothing to re-grant or revoke).
        await fga_reconcile.reconcile(fga, lambda: factory, window=timedelta(0))
        assert await tier(sam.ui, alex) is ViewerTier.none
    finally:
        authz.reset_authz()
        await fga.close()


@pytest.mark.openfga
@pytest.mark.anyio
async def test_block_and_approve_without_profiles_on_real_openfga(
    session: Session, sessions: Any, factory: sessionmaker[Session]
) -> None:
    """ensure_profile inside block/approve: the blocked tuple and the grant land at once."""
    if not _openfga_reachable():
        pytest.skip("OPENFGA_TEST_API_URL is not set or not reachable")
    base = Settings(_env_file=None, openfga_api_url=OPENFGA_TEST_API_URL)
    boot = await fga_bootstrap.bootstrap(base, store_name=f"revel-test-{uuid4()}")
    fga = create_fga_client(
        base.model_copy(
            update={
                "openfga_store_id": boot.store_id,
                "openfga_authorization_model_id": boot.authorization_model_id,
            }
        )
    )
    authz.configure_authz(fga)
    try:
        sam = add_person(session, "sam")
        alex = add_person(session, "alex", profile=False)
        cal = add_person(session, "cal", profile=False)
        await fga_reconcile.reconcile(fga, lambda: factory, window=timedelta(0))

        blocked = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
        await svc.block(alex.ui, blocked.request.id, sessions=sessions)
        alex_profile = session.execute(
            select(Profile.id).where(Profile.user_id == alex.user.id)
        ).scalar_one()
        assert await fga.check(
            user=f"user:{sam.user.id}", relation="blocked", object=f"profile:{alex_profile}"
        )
        # Even a Public opt-in would not reach Sam: the block applies already.
        await authz.on_public_opt_in_changed(profile_id=alex_profile, enabled=True)
        assert await resolve_viewer_tier(fga, sam.ui, alex_profile) is ViewerTier.none

        req = await svc.lookup_and_request(sam.agent, handle="cal_demo", sessions=sessions)
        await svc.approve(
            cal.agent,
            req.request.id,
            tier="trusted_circle",
            owner_confirmed=True,
            sessions=sessions,
        )
        cal_profile = session.execute(
            select(Profile.id).where(Profile.user_id == cal.user.id)
        ).scalar_one()
        assert await resolve_viewer_tier(fga, sam.agent, cal_profile) is ViewerTier.trusted_circle
        assert await resolve_viewer_tier(fga, cal.ui, cal_profile) is ViewerTier.only_me
    finally:
        authz.reset_authz()
        await fga.close()
