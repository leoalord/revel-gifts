"""Agent display names: WorkOS client-name capture, sanitisation and the shared label."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AccessToken

from revel.auth.identity import DelegationDisconnected, IdentityResolver
from revel.auth.repository import AgentRecord
from revel.auth.workos_clients import (
    MAX_AGENT_NAME_LENGTH,
    WorkOSClientDirectory,
    sanitize_agent_name,
)
from revel.services.agents import agent_label as agents_module_label
from revel.services.identity import DEFAULT_AGENT_LABEL, agent_label, whoami

from .authkit import TestAuthKit
from .conftest import InMemoryIdentityRepository, RecordingHooks
from .test_http_mcp import INITIALIZE, _bearer, _call_tool, _rpc

pytestmark = pytest.mark.anyio

WORKOS_API = "https://api.workos.com"


def _token(sub: str = "user_alice", client_id: str = "client_01MUSE") -> AccessToken:
    return AccessToken(
        token="opaque", client_id=client_id, scopes=[], subject=sub, claims={"iss": "x"}
    )


class FakeClients:
    def __init__(self, names: dict[str, Any] | None = None, *, fail: bool = False) -> None:
        self.names = names or {}
        self.fail = fail
        self.calls: list[str] = []

    async def fetch_client_name(self, client_id: str) -> str | None:
        self.calls.append(client_id)
        if self.fail:
            raise RuntimeError("secret detail sk_live_should_not_be_logged")
        return self.names.get(client_id)


def _resolver(
    repo: InMemoryIdentityRepository, clients: FakeClients, **kwargs: Any
) -> IdentityResolver:
    return IdentityResolver(repo, hooks=RecordingHooks(), client_directory=clients, **kwargs)


# --- sanitisation -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Muse", "Muse"),
        ("  Claude  ", "Claude"),
        ("Claude\t\n  Desktop", "Claude Desktop"),
        ("Mu\x00se\x1b[31m", "Muse[31m"),
        ("evil‮gnp.exe", "evilgnp.exe"),  # bidi override dropped
        ("Zero​Width", "ZeroWidth"),  # format char dropped
        ("Ünïcødé 助手", "Ünïcødé 助手"),
        ("", None),
        ("   \n\t", None),
        ("\x00\x01", None),
        (None, None),
        (42, None),
        ({"name": "x"}, None),
    ],
)
async def test_sanitize_agent_name(raw: Any, expected: str | None):
    assert sanitize_agent_name(raw) == expected


async def test_sanitize_agent_name_caps_length():
    name = sanitize_agent_name("x" * 500)
    assert name == "x" * MAX_AGENT_NAME_LENGTH
    # Cut at the cap never leaves trailing whitespace.
    assert sanitize_agent_name("a" * (MAX_AGENT_NAME_LENGTH - 1) + " bcd") == "a" * (
        MAX_AGENT_NAME_LENGTH - 1
    )


# --- label helper ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "client_id", "expected"),
    [
        ("Muse", "client_01MUSE", "Muse"),
        ("  Claude ", "client_01X", "Claude"),
        (None, "client_01MUSE", DEFAULT_AGENT_LABEL),
        ("", "client_01MUSE", DEFAULT_AGENT_LABEL),
        ("   ", "client_01MUSE", DEFAULT_AGENT_LABEL),
        (None, "https://claude.ai/oauth/client.json", "claude.ai"),
        (None, "HTTPS://Muse.Example/cimd", "muse.example"),
        (None, "http://insecure.example/cimd", DEFAULT_AGENT_LABEL),
        (None, "https://[bad", DEFAULT_AGENT_LABEL),
        ("Named", "https://claude.ai/oauth/client.json", "Named"),
    ],
)
async def test_agent_label(name, client_id, expected):
    assert agent_label(name, client_id) == expected


async def test_agents_module_reexports_shared_label():
    assert agents_module_label is agent_label


# --- capture in the resolver ------------------------------------------------------------------


async def test_name_captured_on_first_resolve(repo: InMemoryIdentityRepository):
    clients = FakeClients({"client_01MUSE": "  Muse\n"})
    resolver = _resolver(repo, clients)
    principal = await resolver.resolve(_token(), via="mcp")
    agent = await repo.get_agent(principal.agent_id)
    assert agent is not None and agent.name == "Muse"
    # Named now: later resolves never look it up or write again.
    await resolver.resolve(_token(), via="mcp")
    await resolver.resolve(_token(sub="user_bob"), via="api")
    assert clients.calls == ["client_01MUSE"]
    assert repo.agent_name_writes == 1
    result = await whoami(principal, repo)
    assert result.agent is not None
    assert (result.agent.name, result.agent.label) == ("Muse", "Muse")


async def test_existing_name_never_overwritten(repo: InMemoryIdentityRepository):
    agent, _ = await repo.ensure_agent("client_01MUSE")
    repo.agents[agent.id] = AgentRecord(agent.id, agent.oauth_client_id, "Original")
    clients = FakeClients({"client_01MUSE": "Impostor"})
    await _resolver(repo, clients).resolve(_token(), via="mcp")
    assert repo.agents[agent.id].name == "Original"
    assert clients.calls == []
    assert repo.agent_name_writes == 0


async def test_repository_write_is_first_writer_wins(repo: InMemoryIdentityRepository):
    agent, _ = await repo.ensure_agent("client_01MUSE")
    assert await repo.set_agent_name_if_empty(agent.id, "Muse") is True
    assert await repo.set_agent_name_if_empty(agent.id, "Other") is False
    assert repo.agents[agent.id].name == "Muse"
    with pytest.raises(ValueError):
        await repo.set_agent_name_if_empty(agent.id, "")


async def test_unknown_client_rate_limited(repo: InMemoryIdentityRepository):
    clients = FakeClients({})
    resolver = _resolver(repo, clients)
    for _ in range(3):
        principal = await resolver.resolve(_token(), via="mcp")
    assert clients.calls == ["client_01MUSE"]
    assert repo.agents[principal.agent_id].name is None
    assert repo.agent_name_writes == 0


async def test_unknown_client_retried_after_interval(repo: InMemoryIdentityRepository):
    clients = FakeClients({})
    resolver = _resolver(repo, clients, name_retry_seconds=0)
    await resolver.resolve(_token(), via="mcp")
    clients.names["client_01MUSE"] = "Muse"
    principal = await resolver.resolve(_token(), via="mcp")
    assert clients.calls == ["client_01MUSE", "client_01MUSE"]
    assert repo.agents[principal.agent_id].name == "Muse"


async def test_unusable_name_not_written(repo: InMemoryIdentityRepository):
    clients = FakeClients({"client_01MUSE": " \x00\t "})
    principal = await _resolver(repo, clients).resolve(_token(), via="mcp")
    assert repo.agents[principal.agent_id].name is None
    assert repo.agent_name_writes == 0


async def test_cimd_client_not_looked_up(repo: InMemoryIdentityRepository):
    clients = FakeClients({})
    client_id = "https://claude.ai/oauth/client.json"
    principal = await _resolver(repo, clients).resolve(_token(client_id=client_id), via="mcp")
    assert clients.calls == []
    assert repo.agents[principal.agent_id].name is None


async def test_write_failure_does_not_fail_resolve(
    repo: InMemoryIdentityRepository, caplog: pytest.LogCaptureFixture
):
    repo.fail_agent_name_write = True
    clients = FakeClients({"client_01MUSE": "Muse"})
    with caplog.at_level(logging.WARNING, logger="revel.auth.identity"):
        principal = await _resolver(repo, clients).resolve(_token(), via="mcp")
    assert principal.delegation_id is not None
    assert repo.agents[principal.agent_id].name is None
    assert "agent name capture failed: RuntimeError" in caplog.text


async def test_lookup_failure_does_not_fail_resolve_or_leak(
    repo: InMemoryIdentityRepository, caplog: pytest.LogCaptureFixture
):
    clients = FakeClients(fail=True)
    with caplog.at_level(logging.WARNING):
        principal = await _resolver(repo, clients).resolve(_token(), via="mcp")
    assert principal.agent_id is not None
    assert "RuntimeError" in caplog.text
    assert "sk_live" not in caplog.text and "secret detail" not in caplog.text


async def test_disconnected_delegation_writes_no_name(repo: InMemoryIdentityRepository):
    clients = FakeClients({"client_01MUSE": "Muse"})
    resolver = _resolver(repo, clients)
    # First connect with no name available, then disconnect, then make a name available.
    clients.names.clear()
    principal = await resolver.resolve(_token(), via="mcp")
    repo.disconnect(principal.delegation_id)
    clients.names["client_01MUSE"] = "Muse"
    resolver = _resolver(repo, clients)  # fresh rate-limit state
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(_token(), via="mcp")
    assert repo.agents[principal.agent_id].name is None
    assert clients.calls == ["client_01MUSE"]


# --- WorkOS Connect Applications client -------------------------------------------------------


def _directory(handler) -> WorkOSClientDirectory:
    return WorkOSClientDirectory(
        "sk_test_fake",
        base_url=WORKOS_API,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_workos_client_directory_reads_application_name():
    authkit = TestAuthKit()
    authkit.register_application("client_01MUSE", "Muse")
    directory = _directory(authkit.handler)
    assert await directory.fetch_client_name("client_01MUSE") == "Muse"
    assert authkit.application_requests == ["client_01MUSE"]
    assert authkit.application_auth_headers == ["Bearer sk_test_fake"]
    assert await directory.fetch_client_name("client_unknown") is None


async def test_workos_client_directory_skips_url_client_ids():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    assert await _directory(handler).fetch_client_name("https://claude.ai/c.json") is None
    assert await _directory(handler).fetch_client_name("") is None
    assert requests == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"message": "boom"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["not", "an", "object"]),
        # A different application's record is never trusted.
        httpx.Response(200, json={"client_id": "client_OTHER", "name": "Other"}),
        httpx.Response(200, json={"client_id": "client_01MUSE", "name": None}),
        httpx.Response(200, json={"client_id": "client_01MUSE", "name": "   "}),
    ],
)
async def test_workos_client_directory_rejects_bad_responses(response: httpx.Response):
    assert await _directory(lambda _r: response).fetch_client_name("client_01MUSE") is None


async def test_workos_client_directory_network_error_returns_none(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with caplog.at_level(logging.WARNING):
        assert await _directory(handler).fetch_client_name("client_01MUSE") is None
    assert "ConnectError" in caplog.text
    assert "sk_test_fake" not in caplog.text


async def test_workos_client_directory_sanitizes_and_escapes_path():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"client_id": "client/../x", "name": "A\x1bB  C"})

    assert await _directory(handler).fetch_client_name("client/../x") == "AB C"
    assert seen == ["/connect/applications/client%2F..%2Fx"]


# --- end to end through create_app -------------------------------------------------------------


def test_me_and_whoami_report_workos_name(
    auth_client: TestClient, authkit: TestAuthKit, make_token, repo: InMemoryIdentityRepository
) -> None:
    authkit.register_application("client_01MUSE", "Muse")
    token = make_token(client_id="client_01MUSE")
    me = auth_client.get("/me", headers=_bearer(token))
    assert me.status_code == 200, me.text
    assert me.json()["agent"]["name"] == "Muse"
    assert me.json()["agent"]["label"] == "Muse"
    structured = _call_tool(auth_client, token, "whoami")["structuredContent"]
    assert structured["agent"]["label"] == "Muse"
    assert authkit.application_requests == ["client_01MUSE"]
    (agent,) = repo.agents.values()
    assert agent.name == "Muse"


def test_unnamed_client_label_falls_back(auth_client: TestClient, make_token) -> None:
    structured = _call_tool(auth_client, make_token(), "whoami")["structuredContent"]
    assert structured["agent"]["name"] is None
    assert structured["agent"]["label"] == DEFAULT_AGENT_LABEL


def test_unauthenticated_initialize_writes_nothing(
    auth_client: TestClient, authkit: TestAuthKit, repo: InMemoryIdentityRepository
) -> None:
    authkit.register_application("client_muse", "Muse")
    response = _rpc(auth_client, INITIALIZE)
    assert response.status_code == 401
    assert (repo.users, repo.agents, repo.delegations) == ({}, {}, {})
    assert authkit.application_requests == []
