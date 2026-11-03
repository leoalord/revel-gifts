"""RG-10 design (b): revoke the user's WorkOS consent after a disconnect commits, best effort.

Also the 401 contract for disconnected authorizations at the MCP auth layer
(``DelegationAwareVerifier``) and an end-to-end reconnect over HTTP.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AccessToken

from revel.auth.identity import IdentityResolver
from revel.auth.mcp_auth import DelegationAwareVerifier
from revel.auth.runtime import AuthRuntime
from revel.auth.workos_authorized_apps import (
    NullAuthorizedApps,
    WorkOSAuthorizedApps,
    WorkOSRequestFailed,
)

from .authkit import TestAuthKit
from .conftest import RESOURCE_METADATA_URL, InMemoryIdentityRepository, RecordingHooks
from .test_http_mcp import INITIALIZE, _rpc

pytestmark = pytest.mark.anyio

API = "https://api.workos.com"
KEY = "sk_test_consent_key"
USER = "user_alice"
CLIENT = "https://claude.ai/oauth/mcp-oauth-client-metadata"
LIST_PATH = f"/user_management/users/{USER}/authorized_applications"


def _item(app_id: str, client_id: str) -> dict[str, Any]:
    return {
        "object": "authorized_connect_application",
        "id": f"authorized_connect_app_{app_id}",
        "granted_scopes": ["openid"],
        "application": {
            "object": "connect_application",
            "id": app_id,
            "name": "Claude",
            "client_id": client_id,
        },
    }


class FakeWorkOS:
    """``httpx.MockTransport`` handler for the authorized-applications endpoints."""

    def __init__(self, pages: list[list[dict[str, Any]]] | None = None) -> None:
        self.pages = pages if pages is not None else [[_item("connect_app_1", CLIENT)]]
        self.requests: list[httpx.Request] = []
        self.list_status = 200
        self.delete_status = 204
        self.delay = 0.0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await anyio.sleep(self.delay)
        if request.method == "GET" and request.url.path == LIST_PATH:
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={"message": "error"})
            page = int(request.url.params.get("after", "0") or 0)
            after = str(page + 1) if page + 1 < len(self.pages) else None
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": self.pages[page],
                    "list_metadata": {"before": None, "after": after},
                },
            )
        if request.method == "DELETE" and request.url.path.startswith(LIST_PATH + "/"):
            return httpx.Response(self.delete_status)
        return httpx.Response(599)

    @property
    def deletes(self) -> list[str]:
        return [r.url.path for r in self.requests if r.method == "DELETE"]


def _apps(fake: FakeWorkOS) -> WorkOSAuthorizedApps:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return WorkOSAuthorizedApps(KEY, base_url=API, http_client=client)


def _token(sid: str = "app_consent_1", iat: int | None = None) -> AccessToken:
    return AccessToken(
        token="opaque",
        client_id=CLIENT,
        scopes=[],
        subject=USER,
        claims={"iss": "x", "sid": sid, "iat": int(time.time()) - 60 if iat is None else iat},
    )


async def _resolver_with(fake: FakeWorkOS, **kwargs: Any) -> tuple[IdentityResolver, Any]:
    repo = InMemoryIdentityRepository()
    resolver = IdentityResolver(repo, hooks=RecordingHooks(), authorized_apps=_apps(fake), **kwargs)
    principal = await resolver.resolve(_token(), via="mcp")
    return resolver, principal


def _assert_revel_logs_clean(caplog) -> None:
    """Revel's own log records never carry the key or any id. (httpx's INFO request log does
    include URLs; it is not emitted under the default WARNING root level.)"""
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("revel"))
    for secret in (KEY, USER, CLIENT, "connect_app_1"):
        assert secret not in text


# --- WorkOSAuthorizedApps ----------------------------------------------------------------


async def test_revoke_lists_matches_client_id_and_deletes_application_id():
    fake = FakeWorkOS(
        [[_item("connect_app_other", "client_other"), _item("connect_app_1", CLIENT)]]
    )
    assert await _apps(fake).revoke(USER, CLIENT) is True
    listing, _delete = fake.requests
    assert listing.method == "GET" and listing.url.path == LIST_PATH
    assert listing.url.params["limit"] == "100" and listing.url.params["order"] == "asc"
    assert fake.deletes == [f"{LIST_PATH}/connect_app_1"]
    for request in fake.requests:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.url.host == "api.workos.com"


async def test_revoke_paginates():
    fake = FakeWorkOS(
        [
            [_item("connect_app_a", "client_a")],
            [_item("connect_app_b", "client_b")],
            [_item("connect_app_1", CLIENT)],
        ]
    )
    assert await _apps(fake).revoke(USER, CLIENT) is True
    gets = [r for r in fake.requests if r.method == "GET"]
    assert [r.url.params.get("after") for r in gets] == [None, "1", "2"]
    assert fake.deletes == [f"{LIST_PATH}/connect_app_1"]


async def test_revoke_no_matching_app_deletes_nothing():
    fake = FakeWorkOS([[_item("connect_app_other", "client_other")]])
    assert await _apps(fake).revoke(USER, CLIENT) is False
    assert fake.deletes == []


async def test_revoke_delete_404_is_already_gone():
    fake = FakeWorkOS()
    fake.delete_status = 404
    assert await _apps(fake).revoke(USER, CLIENT) is False


@pytest.mark.parametrize(("list_status", "delete_status"), [(500, 204), (401, 204), (200, 500)])
async def test_revoke_errors_raise(list_status: int, delete_status: int):
    fake = FakeWorkOS()
    fake.list_status, fake.delete_status = list_status, delete_status
    with pytest.raises(WorkOSRequestFailed):
        await _apps(fake).revoke(USER, CLIENT)


async def test_null_authorized_apps_is_noop():
    assert await NullAuthorizedApps().revoke(USER, CLIENT) is False


# --- IdentityResolver.disconnect_delegation ----------------------------------------------


async def test_disconnect_revokes_consent_after_commit(caplog):
    fake = FakeWorkOS()
    resolver, p = await _resolver_with(fake)
    repo = resolver.repository
    with caplog.at_level(logging.DEBUG):
        assert await resolver.disconnect_delegation(
            user_id=p.user_id, delegation_id=p.delegation_id
        )
    assert repo.delegations[p.delegation_id].status == "disconnected"
    assert fake.deletes == [f"{LIST_PATH}/connect_app_1"]
    _assert_revel_logs_clean(caplog)
    assert "WorkOS consent revocation: revoked" in caplog.text


@pytest.mark.parametrize("case", ["not_found", "list_error", "delete_error", "transport_error"])
async def test_disconnect_survives_revocation_failures(caplog, case: str):
    fake = FakeWorkOS([[]] if case == "not_found" else None)
    if case == "list_error":
        fake.list_status = 500
    if case == "delete_error":
        fake.delete_status = 503
    resolver, p = await _resolver_with(fake)
    if case == "transport_error":

        async def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        resolver._authorized_apps = WorkOSAuthorizedApps(
            KEY, base_url=API, http_client=httpx.AsyncClient(transport=httpx.MockTransport(broken))
        )
    with caplog.at_level(logging.DEBUG):
        assert await resolver.disconnect_delegation(
            user_id=p.user_id, delegation_id=p.delegation_id
        )
    assert resolver.repository.delegations[p.delegation_id].status == "disconnected"
    _assert_revel_logs_clean(caplog)
    if case == "not_found":
        assert "WorkOS consent revocation: none found" in caplog.text
    else:
        assert "WorkOS consent revocation failed" in caplog.text


async def test_disconnect_revocation_bounded_by_deadline(caplog):
    fake = FakeWorkOS()
    fake.delay = 5.0
    resolver, p = await _resolver_with(fake, consent_revocation_timeout_seconds=0.2)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        assert await resolver.disconnect_delegation(
            user_id=p.user_id, delegation_id=p.delegation_id
        )
    assert time.monotonic() - started < 2.0
    assert "WorkOS consent revocation failed: TimeoutError" in caplog.text
    assert resolver.repository.delegations[p.delegation_id].status == "disconnected"


async def test_no_workos_call_when_disconnect_changes_nothing():
    fake = FakeWorkOS()
    resolver, p = await _resolver_with(fake)
    # Wrong owner, unknown id: nothing changed, so no WorkOS call.
    assert not await resolver.disconnect_delegation(user_id=uuid4(), delegation_id=p.delegation_id)
    assert not await resolver.disconnect_delegation(user_id=p.user_id, delegation_id=uuid4())
    assert fake.requests == []
    # Already disconnected: the second call changes nothing and calls nothing.
    assert await resolver.disconnect_delegation(user_id=p.user_id, delegation_id=p.delegation_id)
    calls = len(fake.requests)
    assert not await resolver.disconnect_delegation(
        user_id=p.user_id, delegation_id=p.delegation_id
    )
    assert len(fake.requests) == calls


async def test_no_workos_call_when_disconnect_hook_fails():
    fake = FakeWorkOS()
    repo = InMemoryIdentityRepository()
    hooks = RecordingHooks()
    resolver = IdentityResolver(repo, hooks=hooks, authorized_apps=_apps(fake))
    p = await resolver.resolve(_token(), via="mcp")
    hooks.fail_next_disconnect_hook = True
    with pytest.raises(RuntimeError):
        await resolver.disconnect_delegation(user_id=p.user_id, delegation_id=p.delegation_id)
    assert fake.requests == []


# --- 401 at the MCP auth layer -----------------------------------------------------------


class _Runtime:
    def __init__(self, resolver: Any, token: AccessToken | None) -> None:
        self.resolver = resolver
        self.verifier = self

    async def verify_token(self, token: str) -> AccessToken | None:
        return _token() if token == "valid" else None


async def test_verifier_returns_none_for_disconnected_authorization():
    repo = InMemoryIdentityRepository()
    resolver = IdentityResolver(repo, hooks=RecordingHooks())
    verifier = DelegationAwareVerifier(_Runtime(resolver, None))  # type: ignore[arg-type]
    # First connection: nothing exists yet and nothing is created by the check.
    assert await verifier.verify_token("valid") is not None
    assert repo.users == {} and repo.delegations == {}
    p = await resolver.resolve(_token(), via="mcp")
    assert await verifier.verify_token("valid") is not None
    assert await resolver.disconnect_delegation(user_id=p.user_id, delegation_id=p.delegation_id)
    assert await verifier.verify_token("valid") is None
    assert await verifier.verify_token("invalid") is None


async def test_verifier_passes_token_through_on_store_error(caplog):
    class Broken:
        async def is_disconnected(self, access_token: AccessToken) -> bool:
            raise ConnectionError("db down")

    verifier = DelegationAwareVerifier(_Runtime(Broken(), None))  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING):
        assert await verifier.verify_token("valid") is not None
    assert "Disconnect check skipped: ConnectionError" in caplog.text


def test_reconnect_end_to_end_over_http(
    auth_client: TestClient,
    authkit: TestAuthKit,
    repo: InMemoryIdentityRepository,
    runtime: AuthRuntime,
    make_token,
) -> None:
    """Connect, disconnect (website), old token 401 on /me and MCP, consent revoked in WorkOS,
    fresh authorization (new sid, later iat) reconnects as a new delegation."""
    from .test_agents_routes import _disconnect, _signed_in

    _signed_in(auth_client, authkit, repo)
    old_token = make_token(sid="app_consent_1", iat=int(time.time()) - 60)
    me = auth_client.get("/me", headers={"Authorization": f"Bearer {old_token}"})
    assert me.status_code == 200
    (old,) = repo.delegations.values()
    authkit.authorize_application("user_alice", "client_muse", "connect_app_muse")

    assert _disconnect(auth_client, old.id).status_code == 200
    assert ("DELETE", f"{LIST_PATH.replace(USER, 'user_alice')}/connect_app_muse") in (
        authkit.authorized_app_requests
    )
    assert authkit.authorized_apps["user_alice"] == []

    refreshed_old = make_token(sid="app_consent_1", iat=int(time.time()) + 5)
    for token in (old_token, refreshed_old):
        assert (
            auth_client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        )
        mcp = _rpc(auth_client, INITIALIZE, token)
        assert mcp.status_code == 401
        assert f'resource_metadata="{RESOURCE_METADATA_URL}"' in mcp.headers["www-authenticate"]

    fresh = make_token(sid="app_consent_2", iat=int(time.time()) + 5)
    assert _rpc(auth_client, INITIALIZE, fresh).status_code == 200
    me = auth_client.get("/me", headers={"Authorization": f"Bearer {fresh}"})
    assert me.status_code == 200
    assert len(repo.delegations) == 2
    assert repo.delegations[old.id].status == "disconnected"
