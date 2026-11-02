"""Identity resolution: upserts, authz hooks, disconnected delegations, Principal invariants."""

from __future__ import annotations

import logging
import sys
import types
from uuid import uuid4

import pytest
from mcp.server.auth.provider import AccessToken

from revel.auth.hooks import AuthzHookMissing, ModuleAuthzHooks
from revel.auth.identity import DelegationDisconnected, IdentityResolver, Principal
from revel.auth.runtime import AuthRuntime
from revel.services.identity import whoami

from .authkit import TestAuthKit
from .conftest import InMemoryIdentityRepository, RecordingHooks

pytestmark = pytest.mark.anyio


def _token(sub: str = "user_alice", client_id: str = "client_muse") -> AccessToken:
    return AccessToken(
        token="opaque", client_id=client_id, scopes=[], subject=sub, claims={"iss": "x"}
    )


@pytest.mark.parametrize(
    ("via", "agent", "delegation"),
    [("ui", False, False), ("mcp", True, True), ("api", True, True)],
)
async def test_principal_valid_combinations(via, agent, delegation):
    Principal(
        user_id=uuid4(),
        agent_id=uuid4() if agent else None,
        delegation_id=uuid4() if delegation else None,
        via=via,
    )


@pytest.mark.parametrize(
    ("via", "agent", "delegation"),
    [
        ("mcp", False, False),  # bearer transport without a delegation
        ("api", False, False),  # bearer transport without a delegation
        ("ui", True, True),  # browser user acting directly must not carry an agent
        ("mcp", True, False),  # partial: agent without delegation
        ("api", False, True),  # partial: delegation without agent
        ("ui", True, False),  # partial on ui
    ],
)
async def test_principal_invalid_combinations(via, agent, delegation):
    with pytest.raises(ValueError):
        Principal(
            user_id=uuid4(),
            agent_id=uuid4() if agent else None,
            delegation_id=uuid4() if delegation else None,
            via=via,
        )


async def test_principal_rejects_unknown_via():
    with pytest.raises(ValueError):
        Principal(user_id=uuid4(), agent_id=None, delegation_id=None, via="cli")  # type: ignore[arg-type]


async def test_first_call_creates_rows_and_calls_hooks_once(
    runtime: AuthRuntime,
    repo: InMemoryIdentityRepository,
    hooks: RecordingHooks,
    authkit: TestAuthKit,
):
    resolver = runtime.require_resolver()
    first = await resolver.resolve(_token(), via="mcp")

    assert len(repo.users) == len(repo.agents) == len(repo.delegations) == 1
    user = repo.users[first.user_id]
    assert user.workos_user_id == "user_alice"
    assert user.email == "alice@revel.test"
    assert user.display_name == "Alice Liddell"
    assert repo.agents[first.agent_id].oauth_client_id == "client_muse"
    assert hooks.calls == [
        ("on_user_created", {"user_id": first.user_id}),
        (
            "on_agent_delegated",
            {
                "user_id": first.user_id,
                "agent_id": first.agent_id,
                "delegation_id": first.delegation_id,
            },
        ),
    ]
    assert authkit.user_requests == ["user_alice"]
    assert authkit.user_auth_headers == ["Bearer sk_test_fake"]

    second = await resolver.resolve(_token(), via="api")
    assert (second.user_id, second.agent_id, second.delegation_id) == (
        first.user_id,
        first.agent_id,
        first.delegation_id,
    )
    assert second.via == "api"
    assert len(repo.users) == len(repo.agents) == len(repo.delegations) == 1
    assert len(hooks.calls) == 2  # no hooks on the second call
    assert authkit.user_requests == ["user_alice"]  # no profile refetch


async def test_two_subjects_two_users(runtime: AuthRuntime, repo: InMemoryIdentityRepository):
    resolver = runtime.require_resolver()
    a = await resolver.resolve(_token(sub="user_alice"), via="mcp")
    b = await resolver.resolve(_token(sub="user_bob"), via="mcp")
    assert a.user_id != b.user_id
    assert len(repo.users) == 2


async def test_same_client_two_users_one_agent_two_delegations(
    runtime: AuthRuntime, repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    resolver = runtime.require_resolver()
    a = await resolver.resolve(_token(sub="user_alice"), via="mcp")
    b = await resolver.resolve(_token(sub="user_bob"), via="mcp")
    assert a.agent_id == b.agent_id
    assert a.delegation_id != b.delegation_id
    assert len(repo.agents) == 1
    assert len(repo.delegations) == 2
    assert [name for name, _ in hooks.calls].count("on_agent_delegated") == 2


async def test_two_clients_same_user_two_agents(
    runtime: AuthRuntime, repo: InMemoryIdentityRepository
):
    resolver = runtime.require_resolver()
    a = await resolver.resolve(_token(client_id="client_muse"), via="mcp")
    b = await resolver.resolve(_token(client_id="client_other"), via="mcp")
    assert a.user_id == b.user_id
    assert a.agent_id != b.agent_id
    assert len(repo.delegations) == 2


async def test_disconnected_delegation_never_reactivated(
    runtime: AuthRuntime, repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    """Regression for #14: first-call upsert never recreates/reactivates a disconnected one."""
    resolver = runtime.require_resolver()
    principal = await resolver.resolve(_token(), via="mcp")
    repo.disconnect(principal.delegation_id)
    calls_before = list(hooks.calls)

    for via in ("mcp", "api"):
        with pytest.raises(DelegationDisconnected):
            await resolver.resolve(_token(), via=via)

    assert len(repo.delegations) == 1
    assert repo.delegations[principal.delegation_id].status == "disconnected"
    assert hooks.calls == calls_before


async def test_profile_lookup_failure_tolerated(
    runtime: AuthRuntime, repo: InMemoryIdentityRepository, authkit: TestAuthKit, caplog
):
    authkit.user_api_status = 500
    with caplog.at_level(logging.DEBUG):
        principal = await runtime.require_resolver().resolve(_token(sub="user_carol"), via="mcp")
    user = repo.users[principal.user_id]
    assert user.email is None and user.display_name is None
    assert "sk_test_fake" not in caplog.text


async def test_hook_failure_rolls_back_and_retries(
    runtime: AuthRuntime, repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    hooks.fail_next_user_hook = True
    with pytest.raises(RuntimeError):
        await runtime.require_resolver().resolve(_token(), via="mcp")
    assert repo.users == {}

    principal = await runtime.require_resolver().resolve(_token(), via="mcp")
    assert [name for name, _ in hooks.calls] == ["on_user_created", "on_agent_delegated"]
    assert principal.user_id in repo.users


async def test_resolution_ignores_non_token_identity(
    runtime: AuthRuntime, repo: InMemoryIdentityRepository
):
    """Only verified token fields (subject, client_id) determine identity."""
    token = AccessToken(
        token="opaque",
        client_id="client_muse",
        scopes=[],
        subject="user_alice",
        claims={"iss": "x", "email": "mallory@evil.test", "user_id": str(uuid4())},
    )
    principal = await runtime.require_resolver().resolve(token, via="mcp")
    assert repo.users[principal.user_id].workos_user_id == "user_alice"
    assert repo.users[principal.user_id].email == "alice@revel.test"


async def test_whoami_service_without_agent(runtime: AuthRuntime, repo: InMemoryIdentityRepository):
    principal = await runtime.require_resolver().resolve(_token(), via="mcp")
    direct = Principal(user_id=principal.user_id, agent_id=None, delegation_id=None, via="ui")
    result = await whoami(direct, repo)
    assert result.agent is None
    assert result.via == "ui"
    assert result.user.workos_user_id == "user_alice"


MISSING_AUTHZ = "revel.auth._missing_authz_for_tests"


async def test_module_hooks_noop_without_authz_module(monkeypatch):
    monkeypatch.setattr("revel.auth.hooks.AUTHZ_MODULE", MISSING_AUTHZ)
    hooks = ModuleAuthzHooks()
    await hooks.on_user_created(user_id=uuid4())
    await hooks.on_agent_delegated(user_id=uuid4(), agent_id=uuid4(), delegation_id=uuid4())


async def test_module_hooks_missing_function_fails_loud(monkeypatch):
    module = types.ModuleType("partial_authz")

    async def on_user_created(*, user_id):
        return None

    module.on_user_created = on_user_created  # on_agent_delegated deliberately missing
    monkeypatch.setitem(sys.modules, "revel.auth._partial_authz_for_tests", module)
    monkeypatch.setattr("revel.auth.hooks.AUTHZ_MODULE", "revel.auth._partial_authz_for_tests")

    repo = InMemoryIdentityRepository()
    with pytest.raises(AuthzHookMissing):
        await IdentityResolver(repo).resolve(_token(), via="mcp")
    # The delegation insert was rolled back, so a fixed hook gets another chance.
    assert repo.delegations == {}


async def test_module_hooks_call_authz_functions_keyword_only(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def on_user_created(*, user_id):
        calls.append(("user", {"user_id": user_id}))

    async def on_agent_delegated(*, user_id, agent_id, delegation_id):
        calls.append(
            (
                "delegation",
                {"user_id": user_id, "agent_id": agent_id, "delegation_id": delegation_id},
            )
        )

    module = types.ModuleType("fake_authz")
    module.on_user_created = on_user_created
    module.on_agent_delegated = on_agent_delegated
    monkeypatch.setitem(sys.modules, "revel.auth._fake_authz_for_tests", module)
    monkeypatch.setattr("revel.auth.hooks.AUTHZ_MODULE", "revel.auth._fake_authz_for_tests")

    repo = InMemoryIdentityRepository()
    principal = await IdentityResolver(repo).resolve(_token(), via="mcp")
    assert calls == [
        ("user", {"user_id": principal.user_id}),
        (
            "delegation",
            {
                "user_id": principal.user_id,
                "agent_id": principal.agent_id,
                "delegation_id": principal.delegation_id,
            },
        ),
    ]


async def test_resolver_default_hooks_without_authz(monkeypatch, repo: InMemoryIdentityRepository):
    monkeypatch.setattr("revel.auth.hooks.AUTHZ_MODULE", MISSING_AUTHZ)
    resolver = IdentityResolver(repo)
    principal = await resolver.resolve(_token(), via="mcp")
    assert principal.delegation_id in repo.delegations
