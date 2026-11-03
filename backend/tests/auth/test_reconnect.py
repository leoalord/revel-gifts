"""Reconnect after disconnect (F2 decision, RG-10): a fresh authorization creates a NEW
delegation; tokens of the disconnected authorization (issued before the disconnect, or
refreshed from it afterwards) never act again. In-memory repository; the PostgreSQL
counterparts live in ``test_sql_repository.py``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AccessToken

from revel.auth.identity import DelegationDisconnected, IdentityResolver, Principal

from .authkit import TestAuthKit
from .conftest import InMemoryIdentityRepository, RecordingHooks
from .test_agents_routes import _disconnect, _signed_in

pytestmark = pytest.mark.anyio

_OMIT: Any = object()


def _token(
    sid: str | None = "app_consent_1",
    iat: Any = _OMIT,
    *,
    sub: str = "user_alice",
    client_id: str = "client_muse",
) -> AccessToken:
    claims: dict[str, Any] = {"iss": "x"}
    if sid is not None:
        claims["sid"] = sid
    if iat is not _OMIT:
        if iat is not None:
            claims["iat"] = iat
    else:
        claims["iat"] = int(time.time()) - 60
    return AccessToken(token="opaque", client_id=client_id, scopes=[], subject=sub, claims=claims)


def _later() -> int:
    """An ``iat`` safely after any disconnect performed so far in the test."""
    return int(time.time()) + 5


def _delegated(hooks: RecordingHooks) -> list[UUID]:
    return [c["delegation_id"] for name, c in hooks.calls if name == "on_agent_delegated"]


async def _connect_and_disconnect(
    resolver: IdentityResolver, sid: str = "app_consent_1"
) -> Principal:
    p = await resolver.resolve(_token(sid), via="mcp")
    assert await resolver.disconnect_delegation(user_id=p.user_id, delegation_id=p.delegation_id)
    return p


async def test_fresh_authorization_creates_new_delegation(
    repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    resolver = IdentityResolver(repo, hooks=hooks)
    old = await _connect_and_disconnect(resolver)
    old_record = repo.delegations[old.delegation_id]
    old_times = (repo.connected_at[old.delegation_id], repo.disconnected_at[old.delegation_id])

    new = await resolver.resolve(_token("app_consent_2", _later()), via="mcp")

    assert new.delegation_id != old.delegation_id
    assert (new.user_id, new.agent_id) == (old.user_id, old.agent_id)
    assert repo.delegations[new.delegation_id].status == "active"
    assert _delegated(hooks) == [old.delegation_id, new.delegation_id]
    assert hooks.calls[-1] == (
        "on_agent_delegated",
        {"user_id": new.user_id, "agent_id": new.agent_id, "delegation_id": new.delegation_id},
    )
    # The old row is history: unchanged, still disconnected.
    assert repo.delegations[old.delegation_id] == old_record
    assert old_record.status == "disconnected"
    assert (
        repo.connected_at[old.delegation_id],
        repo.disconnected_at[old.delegation_id],
    ) == old_times
    # The new sid is stored on the inserted row; later calls reuse the delegation, no hooks.
    assert repo.sids[new.delegation_id] == {"app_consent_2"}
    again = await resolver.resolve(_token("app_consent_2", _later()), via="api")
    assert again.delegation_id == new.delegation_id
    assert len(_delegated(hooks)) == 2


async def test_pre_disconnect_tokens_rejected(
    repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    resolver = IdentityResolver(repo, hooks=hooks)
    old = await _connect_and_disconnect(resolver)
    calls = list(hooks.calls)
    # The same authorization's access token, issued before the disconnect.
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(_token("app_consent_1"), via="mcp")
    # A sid Revel never saw, but issued before the disconnect: not a fresh authorization.
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(_token("app_consent_unseen"), via="mcp")
    assert list(repo.delegations) == [old.delegation_id]
    assert hooks.calls == calls


async def test_refreshed_old_session_token_rejected_before_and_after_reconnect(
    repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    """The crux: the client keeps refreshing the pre-disconnect refresh token, which yields
    tokens issued AFTER the disconnect but carrying the old ``sid``."""
    resolver = IdentityResolver(repo, hooks=hooks)
    old = await _connect_and_disconnect(resolver)
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(_token("app_consent_1", _later()), via="mcp")
    assert list(repo.delegations) == [old.delegation_id]

    new = await resolver.resolve(_token("app_consent_2", _later()), via="mcp")
    # Still refused once a new delegation is active: it never acts through the new one.
    for via in ("mcp", "api"):
        with pytest.raises(DelegationDisconnected):
            await resolver.resolve(_token("app_consent_1", _later()), via=via)
    assert "app_consent_1" not in repo.sids[new.delegation_id]
    assert len(repo.delegations) == 2


async def test_every_sid_seen_on_a_delegation_is_refused_after_disconnect(
    repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    """Two authorizations (e.g. two devices) acting through one delegation are both cut off."""
    resolver = IdentityResolver(repo, hooks=hooks)
    p = await resolver.resolve(_token("app_consent_a"), via="mcp")
    q = await resolver.resolve(_token("app_consent_b"), via="mcp")
    assert q.delegation_id == p.delegation_id
    assert repo.sids[p.delegation_id] == {"app_consent_a", "app_consent_b"}
    assert await resolver.disconnect_delegation(user_id=p.user_id, delegation_id=p.delegation_id)
    for sid in ("app_consent_a", "app_consent_b"):
        with pytest.raises(DelegationDisconnected):
            await resolver.resolve(_token(sid, _later()), via="mcp")
    assert len(repo.delegations) == 1


@pytest.mark.parametrize(
    ("sid", "iat"),
    [(None, "later"), ("app_consent_2", None), ("app_consent_2", "not-a-number"), ("", "later")],
)
async def test_missing_sid_or_iat_fails_closed(
    repo: InMemoryIdentityRepository, hooks: RecordingHooks, sid, iat
):
    resolver = IdentityResolver(repo, hooks=hooks)
    old = await _connect_and_disconnect(resolver)
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(_token(sid, _later() if iat == "later" else iat), via="mcp")
    assert list(repo.delegations) == [old.delegation_id]


async def test_sidless_token_refused_on_reconnected_pair(
    repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    """Once a pair has disconnected history, a token without ``sid`` cannot be attributed to
    the new authorization, so it fails closed even while a new delegation is active."""
    resolver = IdentityResolver(repo, hooks=hooks)
    await _connect_and_disconnect(resolver)
    await resolver.resolve(_token("app_consent_2", _later()), via="mcp")
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(_token(None, _later()), via="mcp")


async def test_first_connection_unchanged_without_sid(
    repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    """No disconnected history: any verified token connects, with or without ``sid``."""
    resolver = IdentityResolver(repo, hooks=hooks)
    p = await resolver.resolve(_token(None, None), via="mcp")
    assert repo.delegations[p.delegation_id].status == "active"
    assert repo.sids == {}


async def test_known_sid_path_writes_nothing(
    repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    resolver = IdentityResolver(repo, hooks=hooks)
    await resolver.resolve(_token("app_consent_1"), via="mcp")  # stored as authorized_sid
    assert repo.sid_writes == 0
    await resolver.resolve(_token("app_consent_1"), via="mcp")
    assert repo.sid_writes == 0
    await resolver.resolve(_token("app_consent_other"), via="mcp")  # a second authorization
    assert repo.sid_writes == 1
    await resolver.resolve(_token("app_consent_other"), via="mcp")
    assert repo.sid_writes == 1


async def test_concurrent_fresh_authorizations_create_one_delegation(
    repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    resolver = IdentityResolver(repo, hooks=hooks)
    old = await _connect_and_disconnect(resolver)
    principals = await asyncio.gather(
        *(
            resolver.resolve(_token(f"app_consent_{i % 2 + 2}", _later()), via="mcp")
            for i in range(8)
        )
    )
    ids = {p.delegation_id for p in principals}
    assert len(ids) == 1 and old.delegation_id not in ids
    (new_id,) = ids
    active = [d for d in repo.delegations.values() if d.status == "active"]
    assert [d.id for d in active] == [new_id]
    assert _delegated(hooks) == [old.delegation_id, new_id]
    assert repo.sids[new_id] == {"app_consent_2", "app_consent_3"}


async def test_disconnecting_the_new_delegation_and_reconnecting_again(
    repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    resolver = IdentityResolver(repo, hooks=hooks)
    first = await _connect_and_disconnect(resolver)
    second = await resolver.resolve(_token("app_consent_2", _later()), via="mcp")
    hooks.calls.clear()
    assert await resolver.disconnect_delegation(
        user_id=second.user_id, delegation_id=second.delegation_id
    )
    assert hooks.calls == [
        (
            "on_agent_disconnected",
            {
                "user_id": second.user_id,
                "delegation_id": second.delegation_id,
                "agent_id": second.agent_id,
            },
        )
    ]
    assert repo.delegations[second.delegation_id].status == "disconnected"
    # The first delegation was not touched by the second disconnect.
    assert not await resolver.disconnect_delegation(
        user_id=first.user_id, delegation_id=first.delegation_id
    )
    await asyncio.sleep(0.01)  # disconnect timestamps strictly before the next iat
    for sid in ("app_consent_1", "app_consent_2"):
        with pytest.raises(DelegationDisconnected):
            await resolver.resolve(_token(sid, _later()), via="mcp")
    third = await resolver.resolve(_token("app_consent_3", _later()), via="mcp")
    assert third.delegation_id not in (first.delegation_id, second.delegation_id)
    assert [d.status for d in repo.delegations.values()] == [
        "disconnected",
        "disconnected",
        "active",
    ]


async def test_other_users_and_assistants_unaffected(
    repo: InMemoryIdentityRepository, hooks: RecordingHooks
):
    resolver = IdentityResolver(repo, hooks=hooks)
    await _connect_and_disconnect(resolver)
    # Same sid value for another user or another assistant is a different pair.
    bob = await resolver.resolve(_token("app_consent_1", sub="user_bob"), via="mcp")
    other = await resolver.resolve(_token("app_consent_1", client_id="client_other"), via="mcp")
    assert repo.delegations[bob.delegation_id].status == "active"
    assert repo.delegations[other.delegation_id].status == "active"


def test_agents_list_shows_old_and_new_delegations(
    auth_client: TestClient,
    authkit: TestAuthKit,
    repo: InMemoryIdentityRepository,
    runtime,
) -> None:
    alice = _signed_in(auth_client, authkit, repo)
    resolver = runtime.require_resolver()

    old = asyncio.run(resolver.resolve(_token("app_consent_1"), via="mcp"))
    assert old.user_id == alice
    assert _disconnect(auth_client, old.delegation_id).status_code == 200
    new = asyncio.run(resolver.resolve(_token("app_consent_2", _later()), via="mcp"))

    body = auth_client.get("/api/agents").json()
    assert [(item["delegation_id"], item["status"]) for item in body] == [
        (str(new.delegation_id), "active"),
        (str(old.delegation_id), "disconnected"),
    ]
    assert body[0]["agent"] == body[1]["agent"]
    assert body[0]["disconnected_at"] is None and body[1]["disconnected_at"] is not None
    # The old one cannot be disconnected again; the new one can.
    assert _disconnect(auth_client, old.delegation_id).status_code == 404
    response = _disconnect(auth_client, new.delegation_id)
    assert response.status_code == 200 and response.json()["status"] == "disconnected"
