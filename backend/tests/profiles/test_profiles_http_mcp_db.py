"""End to end through F1's ``create_app``: the bearer API (``/api/v1/profile*``), the MCP tools
and the browser routes (``/api/ui/profile*``, same-origin writes) reach the same F5 service.

Identity is F2's real resolver over PostgreSQL (``SqlAlchemyIdentityRepository``) with the
AuthKit test double; OpenFGA is F4's FakeFga, fed by F2's real hooks (``ModuleAuthzHooks``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from auth.authkit import TestAuthKit
from authz_fakes import FakeFga
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from revel.app import create_app
from revel.auth.authz import configure_authz, reset_authz
from revel.auth.browser import SessionTokens
from revel.auth.hooks import ModuleAuthzHooks
from revel.auth.repository import SqlAlchemyIdentityRepository
from revel.auth.runtime import AuthRuntime, reset_auth_runtimes
from revel.db.models import Claim

BASE = "http://localhost:8000"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
ORIGIN = {"Origin": BASE}


@pytest.fixture
def stack(factory: sessionmaker[Session]) -> Iterator[tuple[TestClient, AuthRuntime, FakeFga]]:
    authkit = TestAuthKit()
    settings = TestAuthKit.settings(public_base_url=BASE)
    runtime = authkit.install(
        settings,
        repository=SqlAlchemyIdentityRepository(lambda: factory),
        hooks=ModuleAuthzHooks(),
    )
    fga = FakeFga()
    try:
        with TestClient(create_app(settings), base_url=BASE) as client:
            configure_authz(fga)  # after the lifespan installed its (unconfigured) client
            client.headers["Authorization"] = f"Bearer {TestAuthKit.token(settings)}"
            yield client, runtime, fga
    finally:
        reset_authz()
        reset_auth_runtimes()


def mcp_call(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=MCP_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


@contextmanager
def ui_client(client: TestClient, runtime: AuthRuntime) -> Iterator[TestClient]:
    """Same app (lifespan already running), signed in through the browser session cookie and
    without a bearer token."""
    browser = runtime.require_browser()
    sealed = browser.seal_session(SessionTokens(TestAuthKit.session_token(), "rt_x"), None)
    ui = TestClient(client.app, base_url=BASE)
    ui.cookies.set(browser.config.session_cookie_name, sealed)
    try:
        yield ui
    finally:
        ui.close()


FACT = {"category": "owned_item", "statement": "Owns a Moka pot", "evidence_type": "explicit"}


def test_app_still_serves_health_and_mcp(
    stack: tuple[TestClient, AuthRuntime, FakeFga],
) -> None:
    client, _, _ = stack
    assert client.get("/health").json() == {"status": "ok"}
    listed = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=MCP_HEADERS
    )
    assert listed.status_code == 200
    tools = {t["name"]: t for t in listed.json()["result"]["tools"]}
    assert "whoami" in tools
    f5 = ("get_profile", "add_facts", "update_fact", "set_contribution_policy")
    hints = {name: tools[name]["annotations"]["readOnlyHint"] for name in f5}
    assert hints["get_profile"] is True
    assert hints["add_facts"] is False and hints["update_fact"] is False
    assert tools["update_fact"]["annotations"]["destructiveHint"] is True
    assert tools["update_fact"]["annotations"]["idempotentHint"] is False
    assert "volunteers" in tools["get_profile"]["description"]


def test_bearer_mcp_and_ui_share_one_profile(
    stack: tuple[TestClient, AuthRuntime, FakeFga], factory: sessionmaker[Session]
) -> None:
    client, runtime, _ = stack
    initial = client.get("/api/v1/profile/policy").json()
    assert initial["policy"] is None and initial["defaults"]["share_provisional"] is True
    assert [a["id"] for a in initial["audiences"]] == [
        "only_me",
        "trusted_circle",
        "connections",
        "public",
    ]
    assert client.get("/api/v1/profile").json()["defaults"]["enrichment_enabled"] is True
    # Structural body errors are FastAPI's 422; semantic ones are per item or 400.
    bad = client.post(
        "/api/v1/profile/facts", json={"facts": [{**FACT, "evidence_type": "rumour"}]}
    )
    assert bad.status_code == 422
    assert client.get("/api/ui/profile").status_code == 401  # a bearer token is no UI session

    # Bearer API contribution (no policy yet: held for review).
    added = client.post("/api/v1/profile/facts", json={"facts": [FACT]})
    assert added.status_code == 200, added.text
    (result,) = added.json()["results"]
    assert result["outcome"] == "pending_review"

    with ui_client(client, runtime) as ui:
        # SameOrigin enforcement on UI writes.
        assert ui.put("/api/ui/profile/policy", json={"expected_version": None}).status_code == 403
        approved = ui.put("/api/ui/profile/policy", json={"expected_version": None}, headers=ORIGIN)
        assert approved.status_code == 200, approved.text
        assert approved.json()["policy"]["version"] == 1
        stale = ui.put("/api/ui/profile/policy", json={"expected_version": None}, headers=ORIGIN)
        assert stale.status_code == 409
        assert stale.json()["detail"] == "policy_version_conflict"
        assert stale.json()["policy"]["version"] == 1
        bad = ui.put(
            "/api/ui/profile/policy",
            json={"expected_version": 1, "category_settings": {"size": {"audience": "public"}}},
            headers=ORIGIN,
        )
        assert bad.status_code == 400

        ok = ui.post(f"/api/ui/profile/facts/{result['claim_id']}/approve", headers=ORIGIN)
        assert ok.status_code == 200 and ok.json()["status"] == "active"
        again = ui.post(f"/api/ui/profile/facts/{result['claim_id']}/approve", headers=ORIGIN)
        assert again.status_code == 409
        assert again.json()["fact"]["id"] == result["claim_id"]

        # MCP contribution under the approved policy: accepted.
        tool = mcp_call(client, "add_facts", {"facts": [{**FACT, "statement": "Owns a kettle"}]})
        assert tool.get("isError") is not True, tool
        (mcp_result,) = tool["structuredContent"]["results"]
        assert mcp_result["outcome"] == "accepted"

        profile = ui.get("/api/ui/profile").json()
        assert profile["policy"]["version"] == 1 and profile["policy_version"] == 1
        vias = sorted(f["contributed_via"] for f in profile["facts"])
        assert vias == ["api", "mcp"]
        mcp_profile = mcp_call(client, "get_profile", {})["structuredContent"]
        assert {f["id"] for f in mcp_profile["facts"]} == {f["id"] for f in profile["facts"]}
        assert mcp_profile["profile_id"] == profile["profile_id"]
        assert mcp_profile["policy_version"] == 1
        assert mcp_profile["policy"] == client.get("/api/v1/profile/policy").json()
        assert client.get("/api/v1/profile").json()["profile_id"] == profile["profile_id"]
        assert client.get("/api/v1/profile/summary").json()["pending_review"] == 0
        assert ui.get("/api/ui/profile/summary").status_code == 200

        # Agent narrowing via MCP and bearer; widening without the owner's confirmation is
        # 403 / tool error (#29, covered in test_owner_controls_http_mcp_db); UI may widen.
        fid = mcp_result["claim_id"]
        narrowed = mcp_call(
            client,
            "update_fact",
            {"fact_id": fid, "action": "set_audience", "audience": "only_me"},
        )
        assert narrowed["structuredContent"]["audience"] == "only_me"
        widen = client.patch(
            f"/api/v1/profile/facts/{fid}/audience", json={"audience": "connections"}
        )
        assert widen.status_code == 403
        assert widen.json()["detail"] == "owner_confirmation_required"
        tool_widen = mcp_call(
            client,
            "update_fact",
            {"fact_id": fid, "action": "set_audience", "audience": "connections"},
        )
        assert tool_widen["isError"] is True
        assert "owner_confirmation_required" in tool_widen["content"][0]["text"]
        assert (
            ui.patch(f"/api/ui/profile/facts/{fid}", json={"audience": "connections"}).status_code
            == 403
        )  # no Origin
        widened = ui.patch(
            f"/api/ui/profile/facts/{fid}", json={"audience": "connections"}, headers=ORIGIN
        )
        assert widened.status_code == 200 and widened.json()["audience"] == "connections"

        # Correction via UI creates a replacement.
        corrected = ui.patch(
            f"/api/ui/profile/facts/{fid}",
            json={"statement": "Owns a gooseneck kettle"},
            headers=ORIGIN,
        )
        assert corrected.status_code == 200
        assert corrected.json()["supersedes_id"] == fid
        empty = ui.patch(
            f"/api/ui/profile/facts/{corrected.json()['id']}",
            json={"statement": ""},
            headers=ORIGIN,
        )
        assert empty.status_code == 400
        assert empty.json()["detail"][0]["loc"] == ["body", "statement"]

        # Removal: MCP and DELETE; unknown ids are 404 / generic tool errors.
        new_id = corrected.json()["id"]
        removed = mcp_call(client, "update_fact", {"fact_id": new_id, "action": "remove"})
        assert removed["structuredContent"]["status"] == "removed"
        assert client.delete(f"/api/v1/profile/facts/{new_id}").status_code == 409
        missing = "00000000-0000-0000-0000-000000000000"
        assert client.delete(f"/api/v1/profile/facts/{missing}").status_code == 404
        assert ui.delete(f"/api/ui/profile/facts/{missing}", headers=ORIGIN).status_code == 404
        tool_missing = mcp_call(client, "update_fact", {"fact_id": missing, "action": "remove"})
        assert tool_missing["isError"] is True
        assert tool_missing["content"][0]["text"].endswith("Fact not found")

        # Agents approve only with the owner's confirmation (#29): a live fact is a conflict.
        assert client.post(f"/api/v1/profile/facts/{fid}/approve").status_code == 409

    with factory() as s:
        vias = {c.contributed_via for c in s.execute(select(Claim)).scalars()}
    assert vias == {"api", "mcp", "ui"}


def test_unavailable_authorization_is_generic_503(
    stack: tuple[TestClient, AuthRuntime, FakeFga],
) -> None:
    client, _, fga = stack
    assert client.post("/api/v1/profile/facts", json={"facts": [FACT]}).status_code == 200
    fga.error = OSError("dial tcp fga.internal:8080: connection refused")
    response = client.get("/api/v1/profile")
    assert response.status_code == 503
    assert response.json() == {"detail": "Authorization service unavailable"}
    tool = mcp_call(client, "get_profile", {})
    assert tool["isError"] is True
    assert tool["content"][0]["text"].endswith("Authorization service unavailable")
    fga.error = None
    reset_authz()  # unconfigured OpenFGA: also 503, never an allow
    assert client.get("/api/v1/profile").status_code == 503
