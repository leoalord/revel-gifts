"""Shared profile reads (RG-56, #28): ``get_profile(handle)`` (MCP),
``GET /api/v1/profiles/{handle}`` (bearer) and ``GET /api/ui/profiles/{handle}`` (browser)
over F6's ``resolve_handle`` and F5's ``read_shared_profile`` (F4's ``visible_claims``).

The scenario tests run against a REAL OpenFGA server (model v2) and PostgreSQL, and every read
goes through ALL THREE adapters, which must agree. Connections are made through F6's service, so
the tier tuples come from F4's real hooks. Needs ``OPENFGA_TEST_API_URL`` and
``REVEL_TEST_DATABASE_URL``; the error-mapping tests use F4's FakeFga.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import pytest
from authz_fakes import FakeFga
from fastapi.routing import APIRoute
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from revel.api import profiles as api_profiles
from revel.app import create_app
from revel.auth import authz, fga_bootstrap, visibility
from revel.auth.authz import configure_authz, delegation_tuples, reset_authz
from revel.auth.deps import current_principal
from revel.auth.fga import OpenFgaServiceClient, create_fga_client
from revel.auth.identity import Principal
from revel.auth.ui import current_ui_principal
from revel.db.enums import DelegationStatus
from revel.db.models import Agent, AgentDelegation, Handle, Profile, User
from revel.mcp import profile_tools
from revel.mcp.server import create_mcp_server
from revel.services import connections, profiles
from revel.services.profiles import CategorySetting, PolicyUpdate
from revel.settings import Settings

OPENFGA_TEST_API_URL = os.environ.get("OPENFGA_TEST_API_URL", "")
BASE = "http://localhost:8000"
pytestmark = pytest.mark.anyio

SHARED_FACT_KEYS = {
    "id",
    "category",
    "statement",
    "details",
    "audience",
    "evidence_type",
    "confidence",
    "observed_at",
    "observed_precision",
    "provisional",
}
PROFILE_KEYS = {"handle", "display_name", "avatar_url", "facts", "provisional_caveat"}


# --- People -----------------------------------------------------------------------------------


@dataclass
class Person:
    user: User
    delegation: AgentDelegation
    handle: str

    @property
    def ui(self) -> Principal:
        return Principal(user_id=self.user.id, agent_id=None, delegation_id=None, via="ui")

    @property
    def agent(self) -> Principal:
        d = self.delegation
        return Principal(user_id=d.user_id, agent_id=d.agent_id, delegation_id=d.id, via="mcp")

    @property
    def api(self) -> Principal:
        d = self.delegation
        return Principal(user_id=d.user_id, agent_id=d.agent_id, delegation_id=d.id, via="api")


def add_people(session: Session, *names: str, hidden: frozenset[str] = frozenset()) -> dict:
    agent = Agent(oauth_client_id=f"client_muse_{uuid4().hex[:6]}", name="Muse")
    session.add(agent)
    session.flush()
    people = {}
    for name in names:
        user = User(workos_user_id=f"user_{name}_{uuid4().hex[:6]}", display_name=name.title())
        session.add(user)
        session.flush()
        d = AgentDelegation(
            user_id=user.id, agent_id=agent.id, status=DelegationStatus.ACTIVE.value
        )
        session.add(d)
        handle = f"{name}_demo"
        session.add(
            Handle(
                user_id=user.id,
                handle=handle,
                discoverable=name not in hidden,
                avatar_url=f"https://example.test/{name}.png",
            )
        )
        session.flush()
        people[name] = Person(user=user, delegation=d, handle=handle)
    session.commit()
    return people


@pytest.fixture(autouse=True)
def _reset_lookup_limits() -> Iterator[None]:
    connections.lookup_limiter.reset()
    yield
    connections.lookup_limiter.reset()


# --- Both adapters ----------------------------------------------------------------------------


class Acting:
    principal: Principal | None = None


@dataclass
class Readers:
    http: httpx.AsyncClient
    mcp: Any
    app: Any
    acting: Acting


@pytest.fixture
async def readers(
    settings: Settings, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Readers]:
    """The bearer route (in this event loop via ASGITransport, so a real OpenFGA client works)
    and the MCP server, both acting as ``acting.principal``."""
    acting = Acting()

    async def principal() -> Principal:
        assert acting.principal is not None
        return acting.principal

    async def mcp_principal(_runtime: Any) -> Principal:
        assert acting.principal is not None
        return acting.principal

    app = create_app(settings)
    app.dependency_overrides[current_principal] = principal
    app.dependency_overrides[current_ui_principal] = principal
    monkeypatch.setattr(profile_tools, "principal_from_mcp_context", mcp_principal)
    monkeypatch.setattr(connections, "_default_sessionmaker", lambda: factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
        yield Readers(http=http, mcp=create_mcp_server(settings), app=app, acting=acting)


async def read(readers: Readers, viewer: Principal, handle: str) -> tuple[int, Any]:
    """``(200, body)`` or ``(status, error)`` from the bearer route; asserts the browser route
    returns the identical response and MCP the same body (or a tool error)."""
    readers.acting.principal = viewer
    response = await readers.http.get(f"/api/v1/profiles/{handle}")
    body = response.json()
    browser = await readers.http.get(f"/api/ui/profiles/{handle}")
    assert (browser.status_code, browser.json()) == (response.status_code, body)
    try:
        result = await readers.mcp.call_tool("get_profile", {"handle": handle})
    except ToolError as exc:
        mcp: Any = ("error", str(exc))
    else:
        mcp = ("error", result.content[0].text) if result.is_error else result.structured_content
    if response.status_code == 200:
        assert mcp == body
    else:
        assert mcp[0] == "error", mcp
        body = (body, mcp[1])
    return response.status_code, body


def statements(body: dict[str, Any]) -> set[str]:
    return {f["statement"] for f in body["facts"]}


# --- Real OpenFGA -----------------------------------------------------------------------------


@pytest.fixture
def openfga_url() -> str:
    if not OPENFGA_TEST_API_URL:
        pytest.skip("OPENFGA_TEST_API_URL is not set")
    try:
        healthy = httpx.get(f"{OPENFGA_TEST_API_URL}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        healthy = False
    if not healthy:
        pytest.skip(f"OpenFGA not reachable at {OPENFGA_TEST_API_URL}")
    return OPENFGA_TEST_API_URL


@pytest.fixture
async def real_fga(openfga_url: str) -> AsyncIterator[OpenFgaServiceClient]:
    base = Settings(_env_file=None, openfga_api_url=openfga_url)
    boot = await fga_bootstrap.bootstrap(base, store_name=f"revel-f5-shared-{uuid4()}")
    client = create_fga_client(
        base.model_copy(
            update={
                "openfga_store_id": boot.store_id,
                "openfga_authorization_model_id": boot.authorization_model_id,
            }
        )
    )
    configure_authz(client)
    try:
        yield client
    finally:
        reset_authz(client)
        try:
            await client.sdk.delete_store()
        finally:
            await client.close()


def fact(category: str, statement: str, **kw: Any) -> dict[str, Any]:
    return {"category": category, "statement": statement, "evidence_type": "explicit", **kw}


async def connect(factory: sessionmaker[Session], viewer: Person, owner: Person, tier: str):
    sessions = lambda: factory  # noqa: E731
    req = await connections.lookup_and_request(viewer.ui, handle=owner.handle, sessions=sessions)
    await connections.approve(owner.ui, req.request.id, tier=tier, sessions=sessions)
    return req.request.id


async def test_shared_reads_follow_tier_policy_and_revocation_on_real_openfga(
    session: Session,
    factory: sessionmaker[Session],
    real_fga: OpenFgaServiceClient,
    readers: Readers,
) -> None:
    people = add_people(
        session, "alex", "sam", "tara", "cal", "bo", "dee", "hid", hidden=frozenset({"hid"})
    )
    alex, sam, tara, cal, bo, _dee, hid = (people[n] for n in people)
    for person in people.values():
        await real_fga.write_tuples(delegation_tuples(person.user.id, person.delegation.id))

    # Alex's profile: one fact per audience plus provisional, pending, superseded and removed.
    await profiles.set_policy(session, alex.ui, PolicyUpdate(public_opt_in=True))
    owner_facts = await profiles.add_facts(
        session,
        alex.ui,
        [
            fact("interest", "Collects stamps"),
            fact("favorite_brand", "Likes Muji", audience="connections"),
            fact("size", "Shoe size 42", details={"item": "shoes", "size": "42"}),
            fact("owned_item", "Owns a secret diary", audience="only_me"),
            fact("interest", "Maybe likes jazz", audience="connections", confidence="low"),
            fact("wish", "Old wish", audience="connections"),
            fact("wish", "Removed wish", audience="connections"),
        ],
    )
    ids = {r.index: r.claim_id for r in owner_facts.results}
    assert all(r.outcome == "accepted" for r in owner_facts.results)
    await profiles.add_facts(
        session,
        alex.agent,
        [
            fact("wish", "New wish", audience="connections", supersedes_id=str(ids[5])),
            fact("interest", "Pending: plans with partner", involves_others=True),
        ],
    )
    await profiles.remove_fact(session, alex.agent, ids[6])

    await connect(factory, sam, alex, "connections")
    await connect(factory, tara, alex, "trusted_circle")
    blocked_request = await connections.lookup_and_request(
        bo.ui, handle=alex.handle, sessions=lambda: factory
    )
    await connections.block(alex.ui, blocked_request.request.id, sessions=lambda: factory)
    # Dee has a handle but no profile yet; Hid has a profile but is undiscoverable.
    await profiles.add_facts(session, hid.ui, [fact("interest", "Hidden fact")])

    connections_view = {"Collects stamps", "Likes Muji", "Maybe likes jazz", "New wish"}
    never = {"Owns a secret diary", "Old wish", "Removed wish", "Pending: plans with partner"}

    # Connections viewer (person and agent): Connections + Public, provisional with caveat.
    for viewer in (sam.ui, sam.agent, sam.api):
        status, body = await read(readers, viewer, "alex_demo")
        assert status == 200
        assert set(body) == PROFILE_KEYS
        assert (body["handle"], body["display_name"]) == ("alex_demo", "Alex")
        assert body["avatar_url"] == "https://example.test/alex.png"
        assert statements(body) == connections_view
        for item in body["facts"]:
            assert set(item) == SHARED_FACT_KEYS  # no contributor/provenance/status fields
            assert item["provisional"] is (item["statement"] == "Maybe likes jazz")
        assert body["provisional_caveat"]
        assert "contributed_via" not in str(body) and "contributor" not in str(body)

    # Browser session (the person themselves): a connected user sees Connections facts.
    readers.acting.principal = sam.ui
    browser = await readers.http.get("/api/ui/profiles/alex_demo")
    assert browser.status_code == 200 and statements(browser.json()) == connections_view

    # Trusted Circle sees more; handle lookup is exact but case/@-insensitive.
    status, body = await read(readers, tara.agent, "@Alex_Demo")
    assert status == 200 and statements(body) == connections_view | {"Shoe size 42"}

    # Unconnected viewer: Public only while opted in.
    status, body = await read(readers, cal.agent, "alex_demo")
    assert status == 200
    assert statements(body) == {"Collects stamps"} and body["provisional_caveat"] is None

    # Owner (and owner's agent) reading their own handle: same shape, all live facts.
    status, body = await read(readers, alex.agent, "alex_demo")
    assert status == 200 and set(body) == PROFILE_KEYS
    assert statements(body) == connections_view | {"Shoe size 42", "Owns a secret diary"}

    # Never returned to anyone else: Only Me, pending, superseded, removed.
    for viewer in (sam.agent, tara.agent, cal.agent):
        assert not statements((await read(readers, viewer, "alex_demo"))[1]) & never

    # Hidden cases: identical not-found (HTTP body and MCP text).
    not_found = await read(readers, cal.agent, "nobody_here")
    assert not_found == (404, ({"detail": "target_not_found"}, not_found[1][1]))
    hidden_cases = {
        "blocked": (bo.agent, "alex_demo"),
        "malformed": (cal.agent, "no such handle!"),
        "no profile": (cal.agent, "dee_demo"),
        "undiscoverable": (cal.agent, "hid_demo"),
        "unconnected, not opted in": (sam.agent, "tara_demo"),
    }
    for case, (viewer, handle) in hidden_cases.items():
        assert await read(readers, viewer, handle) == not_found, case

    # Provisional sharing off: the provisional fact (and caveat) disappear on the next read.
    await profiles.set_policy(
        session, alex.ui, PolicyUpdate(expected_version=1, share_provisional=False)
    )
    status, body = await read(readers, sam.agent, "alex_demo")
    assert statements(body) == connections_view - {"Maybe likes jazz"}
    assert body["provisional_caveat"] is None

    # Policy tightening: favorite_brand to Trusted Circle narrows existing facts.
    await profiles.set_policy(
        session,
        alex.ui,
        PolicyUpdate(
            expected_version=2,
            category_settings={"favorite_brand": CategorySetting(audience="trusted_circle")},
        ),
    )
    assert "Likes Muji" not in statements((await read(readers, sam.agent, "alex_demo"))[1])
    assert "Likes Muji" in statements((await read(readers, tara.agent, "alex_demo"))[1])

    # Public withdrawal: the unconnected viewer now gets the identical not-found.
    await profiles.set_policy(
        session, alex.ui, PolicyUpdate(expected_version=3, public_opt_in=False)
    )
    assert await read(readers, cal.agent, "alex_demo") == not_found
    # Browser user with no tier: the identical 404 as a nonexistent handle.
    readers.acting.principal = cal.ui
    no_tier = await readers.http.get("/api/ui/profiles/alex_demo")
    missing = await readers.http.get("/api/ui/profiles/nobody_here")
    assert (no_tier.status_code, no_tier.json()) == (missing.status_code, missing.json())
    assert (no_tier.status_code, no_tier.json()) == (404, {"detail": "target_not_found"})
    assert no_tier.headers.get("content-length") == missing.headers.get("content-length")

    # Connection revocation: Sam's next read is the identical not-found.
    sam_request = session.execute(
        select(connections.ConnectionRequest.id).where(
            connections.ConnectionRequest.requester_user_id == sam.user.id,
            connections.ConnectionRequest.status == "approved",
        )
    ).scalar_one()
    session.rollback()
    await connections.revoke(alex.ui, sam_request, sessions=lambda: factory)
    for viewer in (sam.ui, sam.agent):
        assert await read(readers, viewer, "alex_demo") == not_found

    # Disconnected agent: F2 marks the delegation and F4's hook deletes its tuples. Tara herself
    # keeps access; her disconnected assistant is denied and learns nothing.
    with factory() as s:
        s.execute(
            update(AgentDelegation)
            .where(AgentDelegation.id == tara.delegation.id)
            .values(status=DelegationStatus.DISCONNECTED.value, disconnected_at=func.now())
        )
        s.commit()
    await authz.on_agent_disconnected(user_id=tara.user.id, delegation_id=tara.delegation.id)
    status, (body, message) = await read(readers, tara.agent, "alex_demo")
    assert (status, body) == (403, {"detail": "not_allowed"})
    assert "Shoe size" not in message
    assert statements((await read(readers, tara.ui, "alex_demo"))[1]) >= {"Shoe size 42"}
    # Tuples gone while the row still says active (e.g. mid-disconnect): tier none, not found.
    with factory() as s:
        s.execute(
            update(AgentDelegation)
            .where(AgentDelegation.id == tara.delegation.id)
            .values(status=DelegationStatus.ACTIVE.value, disconnected_at=None)
        )
        s.commit()
    assert await read(readers, tara.agent, "alex_demo") == not_found


# --- Adapter contract (FakeFga) ---------------------------------------------------------------


async def test_adapters_take_only_a_handle(readers: Readers) -> None:
    tools = {t.name: t for t in await readers.mcp.list_tools()}
    tool = tools["get_profile"]
    # RG-59: the handle is optional (omitted = the owner's own read); it is the only input.
    assert set(tool.input_schema["properties"]) == {"handle"}
    assert not tool.input_schema.get("required")
    assert tool.annotations.read_only_hint is True
    f5_tools = ("add_facts", "update_fact", "set_contribution_policy", "remove_fact")
    for name in (*f5_tools, "get_profile"):
        assert "profile_id" not in tools[name].input_schema.get("properties", {}), name

    # F5's routers as composed into the app (FastAPI keeps included routers nested).
    every = [r for router in (api_profiles.api, api_profiles.ui) for r in router.routes]
    routes = {
        r.path: r
        for router in (api_profiles.shared, api_profiles.ui_shared)
        for r in router.routes
        if isinstance(r, APIRoute)
    }
    assert all("profile_id" not in r.path for r in every if isinstance(r, APIRoute))
    assert set(routes) == {"/api/v1/profiles/{handle}", "/api/ui/profiles/{handle}"}
    for route in routes.values():
        assert route.methods == {"GET"}
        params = [p.name for p in route.dependant.path_params + route.dependant.query_params]
        assert params == ["handle"]
        assert route.dependant.body_params == []
    # The bearer route authenticates with F2's bearer dependency only (no session cookie).
    bearer_deps = {d.call for d in routes["/api/v1/profiles/{handle}"].dependant.dependencies}
    browser_deps = {d.call for d in routes["/api/ui/profiles/{handle}"].dependant.dependencies}
    assert current_principal in bearer_deps and current_ui_principal not in bearer_deps
    assert current_ui_principal in browser_deps and current_principal not in browser_deps


async def test_get_profile_owner_and_shared_paths_stay_separate(
    session: Session, fga: FakeFga, readers: Readers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RG-59: ``get_profile()`` is the owner read (get_my_profile + get_policy, owner_claims)
    and never resolves a handle; ``get_profile(handle)`` is the shared read (resolve_handle +
    read_shared_profile, visible_claims) and never touches the owner read, even for the
    caller's own handle. Unknown, hidden and sharing-nothing handles look the same."""
    people = add_people(session, "alex", "sam", "hidden", hidden=frozenset({"hidden"}))
    alex, sam = people["alex"], people["sam"]
    for person in people.values():
        fga.tuples |= set(delegation_tuples(person.user.id, person.delegation.id))
    await profiles.set_policy(session, alex.ui, PolicyUpdate(public_opt_in=True))
    await profiles.add_facts(
        session,
        alex.ui,
        [
            fact("interest", "Collects stamps"),
            fact("interest", "Secret hobby", audience="only_me"),
        ],
    )

    calls: list[str] = []
    for module, names in (
        (profiles, ("get_my_profile", "get_policy", "read_shared_profile")),
        (connections, ("resolve_handle",)),
    ):
        for name in names:
            original = getattr(module, name)

            def spy(*a: Any, _name: str = name, _orig: Any = original, **k: Any) -> Any:
                calls.append(_name)
                return _orig(*a, **k)

            monkeypatch.setattr(module, name, spy)

    readers.acting.principal = alex.agent
    owner = (await readers.mcp.call_tool("get_profile", {})).structured_content
    assert calls == ["get_my_profile", "get_policy"]
    assert set(owner) == {"profile_id", "policy_version", "facts", "summary", "policy"}
    assert {f["statement"] for f in owner["facts"]} == {"Collects stamps", "Secret hobby"}
    assert owner["policy_version"] == 1
    assert owner["policy"]["policy"]["version"] == 1
    assert owner["policy"]["policy"]["public_opt_in"] is True
    assert {"categories", "audiences", "provisional_caveat", "defaults"} <= set(owner["policy"])
    assert owner["policy"] == (await readers.http.get("/api/v1/profile/policy")).json()
    direct = (await readers.http.get("/api/v1/profile")).json()
    assert owner["facts"] == direct["facts"]
    # The summary window ("since") is relative to each call's clock; the rest is identical.
    assert {**owner["summary"], "since": None} == {**direct["summary"], "since": None}
    assert (direct["policy"], direct["defaults"]) == (
        owner["policy"]["policy"],
        owner["policy"]["defaults"],
    )

    calls.clear()
    own_shared = (
        await readers.mcp.call_tool("get_profile", {"handle": alex.handle})
    ).structured_content
    assert calls == ["resolve_handle", "read_shared_profile"]
    assert set(own_shared) == PROFILE_KEYS

    calls.clear()
    status, body = await read(readers, sam.agent, alex.handle)
    assert status == 200 and statements(body) == {"Collects stamps"}
    assert "get_my_profile" not in calls and "get_policy" not in calls

    # Non-revealing: the same tool error for unknown, hidden and sharing-nothing handles.
    messages = set()
    for handle in ("nobody_here", "hidden_demo", sam.handle):
        status, (_, message) = await read(readers, alex.agent, handle)
        assert status == 404
        messages.add(message)
    (message,) = messages
    assert message.endswith("No Revel profile is available to you under that handle")


async def test_errors_are_generic_and_rate_limits_map(
    session: Session,
    fga: FakeFga,
    readers: Readers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    people = add_people(session, "alex", "sam")
    alex, sam = people["alex"], people["sam"]
    for person in people.values():
        fga.tuples |= set(delegation_tuples(person.user.id, person.delegation.id))
    await profiles.set_policy(session, alex.ui, PolicyUpdate(public_opt_in=True))
    await profiles.add_facts(session, alex.ui, [fact("interest", "Collects stamps")])
    status, body = await read(readers, sam.agent, "alex_demo")
    assert status == 200 and statements(body) == {"Collects stamps"}

    # An OpenFGA failure is a generic 503 / tool error: no SDK or exception text.
    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("openfga sdk detail: store 01ABC unreachable")

    with monkeypatch.context() as m:
        m.setattr(visibility, "resolve_viewer_tier", broken)
        status, (body, message) = await read(readers, sam.agent, "alex_demo")
    assert (status, body) == (503, {"detail": "Authorization service unavailable"})
    assert message.endswith("Authorization service unavailable") and "sdk" not in message

    # Rate limited before resolution, whatever the handle.
    with monkeypatch.context() as m:
        m.setattr(connections.lookup_limiter, "reserve", lambda *_a, **_k: None)
        for handle in ("alex_demo", "nobody_here"):
            status, (body, message) = await read(readers, sam.agent, handle)
            assert (status, body) == (429, {"detail": "rate_limited"})
            assert "try again later" in message

    # A profile id in the query string is ignored: the handle alone decides.
    pid = session.execute(select(Profile.id).where(Profile.user_id == alex.user.id)).scalar_one()
    session.rollback()
    readers.acting.principal = sam.agent
    for prefix in ("/api/v1/profiles", "/api/ui/profiles"):
        response = await readers.http.get(f"{prefix}/nobody_here?profile_id={pid}")
        assert response.status_code == 404 and response.json() == {"detail": "target_not_found"}


async def test_read_shared_profile_hides_missing_profiles_and_tier_none(
    session: Session, fga: FakeFga
) -> None:
    people = add_people(session, "alex", "sam")
    alex, sam = people["alex"], people["sam"]
    for person in people.values():
        fga.tuples |= set(delegation_tuples(person.user.id, person.delegation.id))
    with pytest.raises(profiles.SharedProfileNotFound):
        await profiles.read_shared_profile(session, sam.agent, profile_id=None)
    await profiles.add_facts(session, alex.ui, [fact("interest", "Collects stamps")])
    pid = session.execute(select(Profile.id).where(Profile.user_id == alex.user.id)).scalar_one()
    session.rollback()
    with pytest.raises(profiles.SharedProfileNotFound):  # no tier: not opted in, unconnected
        await profiles.read_shared_profile(session, sam.agent, profile_id=pid)
    # Read-only: nothing left open on the session.
    assert not session.in_transaction()
