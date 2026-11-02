"""``GET /api/agents`` and ``POST /api/agents/{id}/disconnect`` (F8's connected agents, #16)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from revel.app import create_app
from revel.auth import authz
from revel.auth.fga import FgaConfigurationError
from revel.auth.hooks import AuthzHookMissing
from revel.auth.repository import AgentRecord, DelegationRecord
from revel.auth.runtime import reset_auth_runtimes
from revel.services.agents import agent_label

from .authkit import TestAuthKit
from .conftest import PUBLIC_BASE_URL, InMemoryIdentityRepository, RecordingHooks
from .test_browser_session import ORIGIN, _sign_in

NOT_FOUND = {"detail": "Not found"}
UNAVAILABLE = {"detail": "Service unavailable"}
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _signed_in(client: TestClient, authkit: TestAuthKit, repo, user: str = "user_alice") -> UUID:
    assert _sign_in(client, authkit, user).status_code == 302
    return next(u.id for u in repo.users.values() if u.workos_user_id == user)


def _add(
    repo: InMemoryIdentityRepository,
    user_id: UUID,
    client_id: str,
    *,
    name: str | None = None,
    connected_at: datetime = T0,
    disconnected: bool = False,
) -> UUID:
    agent = next((a for a in repo.agents.values() if a.oauth_client_id == client_id), None)
    if agent is None:
        agent = AgentRecord(uuid4(), client_id, name)
        repo.agents[agent.id] = agent
    record = DelegationRecord(
        uuid4(), user_id, agent.id, "disconnected" if disconnected else "active"
    )
    repo.delegations[record.id] = record
    repo.connected_at[record.id] = connected_at
    if disconnected:
        repo.disconnected_at[record.id] = connected_at + timedelta(hours=1)
    return record.id


def _disconnect(client: TestClient, delegation_id: Any, headers=ORIGIN):
    return client.post(f"/api/agents/{delegation_id}/disconnect", headers=headers)


# --- labels ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "client_id", "label"),
    [
        ("Muse", "https://muse.example/client.json", "Muse"),
        ("  Muse  ", "client_muse", "Muse"),
        (None, "https://Muse.Example:8443/oauth/client.json", "muse.example"),
        ("   ", "https://muse.example/client.json", "muse.example"),
        (None, "client_01HXYZ", "Assistant"),
        (None, "http://muse.example/client.json", "Assistant"),
        ("", "https://", "Assistant"),
        (None, "https://[::1", "Assistant"),  # unparseable
    ],
)
def test_agent_label(name: str | None, client_id: str, label: str) -> None:
    assert agent_label(name, client_id) == label


# --- list --------------------------------------------------------------------------------


def test_list_requires_sign_in(auth_client: TestClient) -> None:
    response = auth_client.get("/api/agents")
    assert response.status_code == 401
    assert response.json() == {"detail": "Not signed in"}


def test_list_own_delegations_newest_first(
    auth_client: TestClient, authkit: TestAuthKit, repo: InMemoryIdentityRepository
) -> None:
    alice = _signed_in(auth_client, authkit, repo)
    bob_id = uuid4()
    old = _add(repo, alice, "https://muse.example/client.json", connected_at=T0)
    gone = _add(repo, alice, "client_old", connected_at=T0 + timedelta(days=1), disconnected=True)
    new = _add(repo, alice, "client_new", name="Helper", connected_at=T0 + timedelta(days=2))
    _add(repo, bob_id, "client_bob", connected_at=T0 + timedelta(days=3))

    response = auth_client.get("/api/agents")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [item["delegation_id"] for item in body] == [str(new), str(gone), str(old)]
    assert body[0] == {
        "delegation_id": str(new),
        "agent": {"name": "Helper", "client_id": "client_new", "label": "Helper"},
        "status": "active",
        "connected_at": (T0 + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        "disconnected_at": None,
    }
    assert body[1]["status"] == "disconnected" and body[1]["disconnected_at"] is not None
    assert body[1]["agent"]["label"] == "Assistant"
    assert body[2]["agent"] == {
        "name": None,
        "client_id": "https://muse.example/client.json",
        "label": "muse.example",
    }


def test_users_never_see_each_others_delegations(
    auth_client: TestClient, authkit: TestAuthKit, repo: InMemoryIdentityRepository
) -> None:
    alice = _signed_in(auth_client, authkit, repo, "user_alice")
    _add(repo, alice, "client_alice")
    auth_client.cookies.clear()
    bob = _signed_in(auth_client, authkit, repo, "user_bob")
    assert auth_client.get("/api/agents").json() == []
    mine = _add(repo, bob, "client_bob")
    assert [i["delegation_id"] for i in auth_client.get("/api/agents").json()] == [str(mine)]


# --- disconnect --------------------------------------------------------------------------


def test_disconnect_happy_path(
    auth_client: TestClient,
    authkit: TestAuthKit,
    repo: InMemoryIdentityRepository,
    hooks: RecordingHooks,
) -> None:
    alice = _signed_in(auth_client, authkit, repo)
    delegation = _add(repo, alice, "client_muse", name="Muse")
    agent_id = repo.delegations[delegation].agent_id

    response = _disconnect(auth_client, delegation)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["delegation_id"] == str(delegation)
    assert body["status"] == "disconnected"
    assert body["disconnected_at"] is not None
    assert body["agent"] == {"name": "Muse", "client_id": "client_muse", "label": "Muse"}
    assert repo.delegations[delegation].status == "disconnected"
    assert hooks.calls[-1] == (
        "on_agent_disconnected",
        {"user_id": alice, "delegation_id": delegation, "agent_id": agent_id},
    )
    # The list reflects it.
    assert auth_client.get("/api/agents").json()[0]["status"] == "disconnected"


def test_disconnect_not_found_cases_are_identical(
    auth_client: TestClient,
    authkit: TestAuthKit,
    repo: InMemoryIdentityRepository,
    hooks: RecordingHooks,
) -> None:
    alice = _signed_in(auth_client, authkit, repo)
    foreign = _add(repo, uuid4(), "client_bob")
    gone = _add(repo, alice, "client_old", disconnected=True)
    calls_before = list(hooks.calls)

    responses = [
        _disconnect(auth_client, uuid4()),  # unknown
        _disconnect(auth_client, foreign),  # someone else's
        _disconnect(auth_client, gone),  # already disconnected
        _disconnect(auth_client, "not-a-uuid"),  # malformed
        _disconnect(auth_client, "123"),
    ]
    for response in responses:
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
        assert response.content == responses[0].content
    assert repo.delegations[foreign].status == "active"
    assert hooks.calls == calls_before


def test_second_disconnect_is_not_found(
    auth_client: TestClient, authkit: TestAuthKit, repo: InMemoryIdentityRepository
) -> None:
    alice = _signed_in(auth_client, authkit, repo)
    delegation = _add(repo, alice, "client_muse")
    assert _disconnect(auth_client, delegation).status_code == 200
    response = _disconnect(auth_client, delegation)
    assert (response.status_code, response.json()) == (404, NOT_FOUND)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("authz unavailable user:secret-id"),
        FgaConfigurationError("OpenFGA is not configured"),
        TimeoutError("hook timed out"),
        authz.AuthorizationCheckTimeout(),
        AuthzHookMissing("revel.auth.authz.on_agent_disconnected is not defined"),
    ],
)
def test_disconnect_hook_failure_is_503_and_stays_active(
    auth_client: TestClient,
    authkit: TestAuthKit,
    repo: InMemoryIdentityRepository,
    hooks: RecordingHooks,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    alice = _signed_in(auth_client, authkit, repo)
    delegation = _add(repo, alice, "client_muse")

    async def failing(**_: Any) -> None:
        raise error

    monkeypatch.setattr(hooks, "on_agent_disconnected", failing)
    with caplog.at_level(logging.DEBUG):
        response = _disconnect(auth_client, delegation)
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert repo.delegations[delegation].status == "active"
    assert delegation not in repo.disconnected_at
    ours = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("revel"))
    assert str(delegation) not in ours and "secret-id" not in ours

    # Once authorization recovers, the same delegation disconnects normally.
    monkeypatch.undo()
    assert _disconnect(auth_client, delegation).status_code == 200


def test_disconnect_with_unconfigured_openfga_is_503(
    authkit: TestAuthKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through F4's real hook module (no injected hooks): OpenFGA unconfigured fails closed."""
    monkeypatch.setattr(authz, "_state", None)
    repo = InMemoryIdentityRepository()
    settings = authkit.settings()
    authkit.install(settings, repository=repo)
    try:
        with TestClient(create_app(settings), base_url=PUBLIC_BASE_URL) as client:
            alice = _signed_in(client, authkit, repo)
            delegation = _add(repo, alice, "client_muse")
            response = _disconnect(client, delegation)
            assert (response.status_code, response.json()) == (503, UNAVAILABLE)
            assert repo.delegations[delegation].status == "active"
    finally:
        reset_auth_runtimes()


def test_disconnect_requires_sign_in(auth_client: TestClient) -> None:
    response = _disconnect(auth_client, uuid4())
    assert (response.status_code, response.json()) == (401, {"detail": "Not signed in"})


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example"}, {"Origin": "null"}])
def test_disconnect_requires_same_origin(
    auth_client: TestClient,
    authkit: TestAuthKit,
    repo: InMemoryIdentityRepository,
    hooks: RecordingHooks,
    headers: dict[str, str],
) -> None:
    alice = _signed_in(auth_client, authkit, repo)
    delegation = _add(repo, alice, "client_muse")
    response = _disconnect(auth_client, delegation, headers=headers)
    assert response.status_code == 403
    assert repo.delegations[delegation].status == "active"
    assert not any(name == "on_agent_disconnected" for name, _ in hooks.calls)
