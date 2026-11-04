"""Agent-first owner controls (F5 #29) end to end through ``create_app``: the bearer agent API
(``PUT /api/v1/profile/policy``, ``POST /api/v1/profile/facts/{id}/approve|reject``,
``PATCH .../audience`` with ``owner_confirmed``) and the MCP tools ``set_contribution_policy``,
``update_fact`` and ``remove_fact`` (consolidated in RG-59, #31). The browser routes stay
unchanged."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from auth.authkit import TestAuthKit
from authz_fakes import FakeFga
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_profiles_http_mcp_db import BASE, FACT, MCP_HEADERS, ORIGIN, mcp_call, ui_client

from revel.app import create_app
from revel.auth.authz import configure_authz, reset_authz
from revel.auth.hooks import ModuleAuthzHooks
from revel.auth.repository import SqlAlchemyIdentityRepository
from revel.auth.runtime import AuthRuntime, reset_auth_runtimes
from revel.db.models import ClaimEvent

CONFIRM = "owner_confirmation_required"
# Removed in RG-59 (#31), not aliased.
OLD_F5_TOOLS = (
    "get_my_profile",
    "get_contribution_policy",
    "set_fact_audience",
    "review_pending_fact",
)


@pytest.fixture
def stack(factory: sessionmaker[Session]) -> Iterator[tuple[TestClient, AuthRuntime, Any]]:
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
            configure_authz(fga)
            client.headers["Authorization"] = f"Bearer {TestAuthKit.token(settings)}"
            yield client, runtime, settings
    finally:
        reset_authz()
        reset_auth_runtimes()


def tool_error(result: dict[str, Any]) -> str:
    assert result.get("isError") is True, result
    return result["content"][0]["text"]


def ok(result: dict[str, Any]) -> dict[str, Any]:
    assert result.get("isError") is not True, result
    return result["structuredContent"]


def preview_revision(text: str) -> str:
    line = next(line for line in text.splitlines() if "expected_updated_at=" in line)
    return line.split("expected_updated_at=", 1)[1].removesuffix(".")


def test_tools_are_listed_with_the_confirmation_rule(
    stack: tuple[TestClient, AuthRuntime, Any],
) -> None:
    client, _, _ = stack
    listed = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=MCP_HEADERS
    )
    tools = {t["name"]: t for t in listed.json()["result"]["tools"]}
    profile_schema = tools["get_profile"]["outputSchema"]
    assert profile_schema["type"] == "object"
    assert {item["$ref"] for item in profile_schema["anyOf"]} == {
        "#/$defs/OwnerProfileView",
        "#/$defs/SharedProfileOut",
    }
    for name in ("set_contribution_policy", "update_fact"):
        tool = tools[name]
        props = tool["inputSchema"]["properties"]
        assert "owner_confirmed" in props and "owner_confirmed" not in tool["inputSchema"].get(
            "required", []
        )
        assert "Never infer consent" in tool["description"]
        assert "quote exactly what will change" in tool["description"]
        assert tool["annotations"]["readOnlyHint"] is False
    assert "expected_version" in tools["set_contribution_policy"]["inputSchema"]["required"]
    update = tools["update_fact"]["inputSchema"]
    assert update["properties"]["action"]["enum"] == ["set_audience", "approve"]
    assert update["properties"]["audience"]["enum"] == [
        "public",
        "connections",
        "trusted_circle",
        "only_me",
        None,
    ]
    assert "expected_updated_at" in update["properties"]
    assert "audience" not in update["required"]
    assert set(update["required"]) == {"fact_id", "action"}
    assert "set_audience" in tools["update_fact"]["description"]
    assert "requires audience" in tools["update_fact"]["description"]
    assert "remove_fact" in tools["update_fact"]["description"]  # where rejecting went
    remove = tools["remove_fact"]
    assert remove["inputSchema"]["required"] == ["fact_id"]
    assert (
        "explicitly told you to reject"
        in (remove["inputSchema"]["properties"]["owner_confirmed"]["description"])
    )
    assert "action" in remove["inputSchema"]["properties"]
    assert "reject a fact pending" in remove["description"]


def test_f5_tool_set_and_annotations(stack: tuple[TestClient, AuthRuntime, Any]) -> None:
    """RG-59: the consolidated F5 tools only; old names are removed, not aliased."""
    client, _, _ = stack
    listed = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=MCP_HEADERS
    )
    tools = {t["name"]: t for t in listed.json()["result"]["tools"]}
    for old in OLD_F5_TOOLS:
        assert old not in tools, old
    expected = {
        # name: (readOnly, destructive, idempotent)
        "get_profile": (True, None, True),
        "add_facts": (False, False, False),
        "set_contribution_policy": (False, False, False),
        "update_fact": (False, False, False),
        "remove_fact": (False, True, False),
    }
    for name, (read_only, destructive, idempotent) in expected.items():
        hints = tools[name]["annotations"]
        assert hints["readOnlyHint"] is read_only, name
        assert hints.get("destructiveHint") is destructive, name
        assert hints["idempotentHint"] is idempotent, name
        assert hints["openWorldHint"] is False, name
    schema = tools["get_profile"]["inputSchema"]
    assert set(schema["properties"]) == {"handle"} and not schema.get("required")
    # No old name survives anywhere an agent reads: descriptions, titles and the whole input
    # schema (field descriptions of nested models such as FactInput included).
    for name in expected:
        shipped = json.dumps(
            [tools[name]["description"], tools[name].get("title"), tools[name]["inputSchema"]]
        )
        for old in OLD_F5_TOOLS:
            assert old not in shipped, (name, old)


def test_update_fact_per_action_validation(stack: tuple[TestClient, AuthRuntime, Any]) -> None:
    """Missing or invalid per-action params are invalid_request errors naming the action and
    the parameter, raised before any service call (nothing changes)."""
    client, _, _ = stack
    ok(
        mcp_call(
            client, "set_contribution_policy", {"expected_version": None, "owner_confirmed": True}
        )
    )
    (added,) = ok(mcp_call(client, "add_facts", {"facts": [FACT]}))["results"]
    fid = added["claim_id"]
    cases = [
        (
            {"action": "reject"},
            "invalid_request: Unknown action 'reject' for update_fact; expected one of: "
            "set_audience, approve.",
        ),
        ({"action": "set_audience"}, "invalid_request: Action 'set_audience' requires audience."),
        (
            {"action": "set_audience", "audience": "everyone"},
            "invalid_request: Action 'set_audience' requires audience to be one of: public, "
            "connections, trusted_circle, only_me.",
        ),
        (
            {"action": "approve", "audience": "only_me"},
            "invalid_request: Action 'approve' does not accept audience.",
        ),
        (
            {"action": "approve", "audience": "only_me", "owner_confirmed": True},
            "invalid_request: Action 'approve' does not accept audience.",
        ),
    ]
    for args, message in cases:
        # The SDK prefixes "Error executing tool update_fact: "; the message is ours.
        assert tool_error(mcp_call(client, "update_fact", {"fact_id": fid, **args})).endswith(
            ": " + message
        )
    long = tool_error(mcp_call(client, "update_fact", {"fact_id": fid, "action": "x" * 200}))
    assert "x" * 40 + "..." in long and "x" * 41 not in long
    # Nothing changed.
    facts = client.get("/api/v1/profile").json()["facts"]
    assert [(f["id"], f["status"], f["audience"]) for f in facts] == [
        (fid, "active", added["audience"])
    ]


def test_bearer_owner_controls(
    stack: tuple[TestClient, AuthRuntime, Any], factory: sessionmaker[Session]
) -> None:
    client, runtime, _ = stack
    # First approval: refused with the changes list, then allowed with the flag.
    first = client.put("/api/v1/profile/policy", json={"expected_version": None})
    assert first.status_code == 403
    body = first.json()
    assert body["detail"] == CONFIRM and len(body["changes"]) == 1
    assert "first time" in body["changes"][0]
    assert client.get("/api/v1/profile/policy").json()["policy"] is None
    v1 = client.put(
        "/api/v1/profile/policy",
        json={"expected_version": None, "require_review_all": True, "owner_confirmed": True},
    )
    assert v1.status_code == 200, v1.text
    assert v1.json()["policy"]["version"] == 1

    # Widening refused, narrowing fine; stale version is 409; invalid is 400; unknown 422.
    widen = client.put(
        "/api/v1/profile/policy", json={"expected_version": 1, "require_review_all": False}
    )
    assert widen.status_code == 403
    assert widen.json() == {
        "detail": CONFIRM,
        "changes": ["Stop requiring your review of every assistant contribution"],
    }
    narrow = client.put(
        "/api/v1/profile/policy", json={"expected_version": 1, "share_provisional": False}
    )
    assert narrow.status_code == 200 and narrow.json()["policy"]["version"] == 2
    stale = client.put("/api/v1/profile/policy", json={"expected_version": 1})
    assert stale.status_code == 409 and stale.json()["detail"] == "policy_version_conflict"
    bad = client.put(
        "/api/v1/profile/policy",
        json={
            "expected_version": 2,
            "category_settings": {"size": {"audience": "public"}},
            "owner_confirmed": True,
        },
    )
    assert bad.status_code == 400
    assert client.put("/api/v1/profile/policy", json={"nope": 1}).status_code == 422

    # Pending facts (review required): approve needs the flag, reject does not.
    added = client.post(
        "/api/v1/profile/facts",
        json={"facts": [FACT, {**FACT, "statement": "Owns a kettle"}]},
    ).json()["results"]
    a, b = (r["claim_id"] for r in added)
    assert {r["outcome"] for r in added} == {"pending_review"}
    refused = client.post(f"/api/v1/profile/facts/{a}/approve")
    assert refused.status_code == 403 and refused.json()["detail"] == CONFIRM
    assert "Approve the pending" in refused.json()["changes"][0]
    refused = client.post(f"/api/v1/profile/facts/{a}/approve", json={"owner_confirmed": False})
    assert refused.status_code == 403
    approved = client.post(
        f"/api/v1/profile/facts/{a}/approve",
        json={
            "owner_confirmed": True,
            "expected_updated_at": refused.json()["expected_updated_at"],
        },
    )
    assert approved.status_code == 200 and approved.json()["status"] == "active"
    rejected = client.post(f"/api/v1/profile/facts/{b}/reject")
    assert rejected.status_code == 200 and rejected.json()["status"] == "removed"
    assert client.post(f"/api/v1/profile/facts/{b}/reject").status_code == 409

    # Audience: narrow freely, widen only with the flag.
    assert (
        client.patch(f"/api/v1/profile/facts/{a}/audience", json={"audience": "only_me"}).json()[
            "audience"
        ]
        == "only_me"
    )
    widen = client.patch(f"/api/v1/profile/facts/{a}/audience", json={"audience": "connections"})
    assert widen.status_code == 403 and widen.json()["detail"] == CONFIRM
    assert widen.json()["changes"][0].endswith("from Only me to Connections")
    widened = client.patch(
        f"/api/v1/profile/facts/{a}/audience",
        json={
            "audience": "connections",
            "owner_confirmed": True,
            "expected_updated_at": widen.json()["expected_updated_at"],
        },
    )
    assert widened.status_code == 200 and widened.json()["audience"] == "connections"

    with factory() as s:
        rows = s.execute(select(ClaimEvent).where(ClaimEvent.claim_id == a)).scalars().all()
    by_action = {(r.action, r.acceptance) for r in rows if r.actor_delegation_id is not None}
    assert ("approved", "explicit") in by_action
    assert ("audience_changed", "explicit") in by_action

    # The browser routes are unchanged: no flag (and no such field) there.
    with ui_client(client, runtime) as ui:
        assert (
            ui.put(
                "/api/ui/profile/policy",
                json={"expected_version": 2, "owner_confirmed": True},
                headers=ORIGIN,
            ).status_code
            == 422
        )
        v3 = ui.put(
            "/api/ui/profile/policy",
            json={"expected_version": 2, "require_review_all": False},
            headers=ORIGIN,
        )
        assert v3.status_code == 200 and v3.json()["policy"]["version"] == 3
        assert (
            ui.patch(
                f"/api/ui/profile/facts/{a}", json={"audience": "only_me"}, headers=ORIGIN
            ).json()["audience"]
            == "only_me"
        )
        assert (
            ui.patch(
                f"/api/ui/profile/facts/{a}", json={"audience": "connections"}, headers=ORIGIN
            ).status_code
            == 200
        )


def test_mcp_owner_controls(stack: tuple[TestClient, AuthRuntime, Any]) -> None:
    client, _, _ = stack
    text = tool_error(mcp_call(client, "set_contribution_policy", {"expected_version": None}))
    assert f"{CONFIRM}:" in text
    assert "- Approve the contribution policy for the first time" in text
    v1 = ok(
        mcp_call(
            client,
            "set_contribution_policy",
            {"expected_version": None, "require_review_all": True, "owner_confirmed": True},
        )
    )
    assert v1["policy"]["version"] == 1 and v1["policy"]["require_review_all"] is True
    assert {"categories", "audiences", "provisional_caveat"} <= set(v1)  # policy read shape

    # Mixed write: the widening is listed; nothing is written until confirmed.
    mixed = {
        "expected_version": 1,
        "enrichment_enabled": False,
        "public_opt_in": True,
    }
    text = tool_error(mcp_call(client, "set_contribution_policy", mixed))
    assert "- Opt in to Public" in text and "contributions" not in text.split("\n", 1)[1]
    v2 = ok(mcp_call(client, "set_contribution_policy", {**mixed, "owner_confirmed": True}))
    assert v2["policy"]["public_opt_in"] is True and v2["policy"]["enrichment_enabled"] is False
    text = tool_error(
        mcp_call(
            client,
            "set_contribution_policy",
            {"expected_version": 2, "public_opt_in": False, "enrichment_enabled": True},
        )
    )
    assert text.endswith("- Turn assistant contributions back on")
    narrowed = ok(
        mcp_call(client, "set_contribution_policy", {"expected_version": 2, "public_opt_in": False})
    )
    assert narrowed["policy"]["version"] == 3 and narrowed["policy"]["public_opt_in"] is False
    conflict = tool_error(
        mcp_call(client, "set_contribution_policy", {"expected_version": 1, "public_opt_in": False})
    )
    assert "get_profile" in conflict
    invalid = tool_error(
        mcp_call(
            client,
            "set_contribution_policy",
            {"expected_version": 3, "category_settings": {"health": {"audience": "only_me"}}},
        )
    )
    assert invalid.endswith("category_settings.*: unknown category")  # key not echoed
    assert "health" not in invalid


def _events(factory: sessionmaker[Session], claim_id: str) -> list[tuple[str, str | None]]:
    with factory() as s:
        rows = s.execute(
            select(ClaimEvent)
            .where(ClaimEvent.claim_id == UUID(claim_id))
            .order_by(ClaimEvent.occurred_at, ClaimEvent.action)
        ).scalars()
        return [(r.action, r.acceptance) for r in rows]


def test_mcp_review_and_audience(
    stack: tuple[TestClient, AuthRuntime, Any], factory: sessionmaker[Session]
) -> None:
    client, runtime, settings = stack
    ok(
        mcp_call(
            client,
            "set_contribution_policy",
            {"expected_version": None, "require_review_all": True, "owner_confirmed": True},
        )
    )
    with ui_client(client, runtime) as ui:
        old = ui.post("/api/ui/profile/facts", json={"facts": [FACT]}, headers=ORIGIN)
        old_id = old.json()["results"][0]["claim_id"]
    (pending,) = ok(mcp_call(client, "add_facts", {"facts": [{**FACT, "supersedes_id": old_id}]}))[
        "results"
    ]
    pid = pending["claim_id"]

    # Approving always needs the owner's confirmation.
    approve = {"fact_id": pid, "action": "approve"}
    text = tool_error(mcp_call(client, "update_fact", approve))
    assert CONFIRM in text and old_id in text
    fact = ok(
        mcp_call(
            client,
            "update_fact",
            {
                **approve,
                "owner_confirmed": True,
                "expected_updated_at": preview_revision(text),
            },
        )
    )
    assert fact["status"] == "active" and fact["supersedes_id"] == old_id
    statuses = {
        f["id"]: f["status"]
        for f in client.get("/api/v1/profile", params={"status": ["active", "superseded"]}).json()[
            "facts"
        ]
    }
    assert statuses == {pid: "active", old_id: "superseded"}
    assert "changed" in tool_error(
        mcp_call(client, "update_fact", {**approve, "owner_confirmed": True})
    )  # not idempotent: a repeat approve is a conflict

    # set_audience: narrowing is free; widening is refused until the owner confirms.
    narrow = {"fact_id": pid, "action": "set_audience", "audience": "only_me"}
    assert ok(mcp_call(client, "update_fact", narrow))["audience"] == "only_me"
    widen = {"fact_id": pid, "action": "set_audience", "audience": "trusted_circle"}
    text = tool_error(mcp_call(client, "update_fact", widen))
    assert CONFIRM in text and "from Only me to Trusted Circle" in text
    assert client.get("/api/v1/profile").json()["facts"][0]["audience"] == "only_me"
    widened = ok(
        mcp_call(
            client,
            "update_fact",
            {
                **widen,
                "owner_confirmed": True,
                "expected_updated_at": preview_revision(text),
            },
        )
    )
    assert widened["audience"] == "trusted_circle"

    # Another user's assistant: the fact does not exist for them.
    owner_auth = client.headers["Authorization"]
    client.headers["Authorization"] = f"Bearer {TestAuthKit.token(settings, sub='user_bob')}"
    for name, args in (
        ("update_fact", {**approve, "owner_confirmed": True}),
        ("update_fact", {**narrow, "owner_confirmed": True}),
        ("remove_fact", {"fact_id": pid, "owner_confirmed": True}),
    ):
        assert tool_error(mcp_call(client, name, args)).endswith("Fact not found")
    for path in (f"/api/v1/profile/facts/{pid}/approve", f"/api/v1/profile/facts/{pid}/reject"):
        assert client.post(path, json={"owner_confirmed": True}).status_code == 404
    assert (
        client.patch(
            f"/api/v1/profile/facts/{pid}/audience",
            json={"audience": "only_me", "owner_confirmed": True},
        ).status_code
        == 404
    )
    # Bob's own policy write touches only Bob's profile.
    bob = ok(
        mcp_call(
            client, "set_contribution_policy", {"expected_version": None, "owner_confirmed": True}
        )
    )
    assert bob["policy"]["version"] == 1 and bob["policy"]["require_review_all"] is False
    client.headers["Authorization"] = owner_auth
    assert _events(factory, pid)[-1][0] == "audience_changed"  # Bob changed nothing


def test_confirmed_widening_requires_the_previewed_fact_revision(
    stack: tuple[TestClient, AuthRuntime, Any],
) -> None:
    client, _, _ = stack
    ok(
        mcp_call(
            client,
            "set_contribution_policy",
            {
                "expected_version": None,
                "owner_confirmed": True,
            },
        )
    )
    added = _add_mcp(client, {**FACT, "audience": "trusted_circle"})
    fact_id = added["claim_id"]
    args = {"fact_id": fact_id, "action": "set_audience", "audience": "connections"}
    preview = tool_error(mcp_call(client, "update_fact", args))
    assert FACT["statement"] in preview and "from Trusted Circle to Connections" in preview
    old_revision = preview_revision(preview)

    url = f"/api/v1/profile/facts/{fact_id}/audience"
    assert client.patch(url, json={"audience": "only_me"}).status_code == 200
    stale_args = {**args, "owner_confirmed": True, "expected_updated_at": old_revision}
    fresh = tool_error(mcp_call(client, "update_fact", stale_args))
    assert CONFIRM in fresh and "from Only me to Connections" in fresh
    fresh_revision = preview_revision(fresh)
    assert fresh_revision != old_revision
    assert client.get("/api/v1/profile").json()["facts"][0]["audience"] == "only_me"
    out = ok(mcp_call(client, "update_fact", {**stale_args, "expected_updated_at": fresh_revision}))
    assert out["audience"] == "connections"


@pytest.mark.parametrize(
    ("initial", "changed"), [("connections", "only_me"), ("only_me", "connections")]
)
def test_confirmed_approval_requires_the_previewed_pending_audience(
    stack: tuple[TestClient, AuthRuntime, Any],
    initial: str,
    changed: str,
) -> None:
    client, _, _ = stack
    ok(
        mcp_call(
            client,
            "set_contribution_policy",
            {
                "expected_version": None,
                "owner_confirmed": True,
                "require_review_all": True,
            },
        )
    )
    added = _add_mcp(client, {**FACT, "audience": initial})
    fact_id = added["claim_id"]
    url = f"/api/v1/profile/facts/{fact_id}"
    preview = client.post(f"{url}/approve")
    assert preview.status_code == 403
    old_revision = preview.json()["expected_updated_at"]
    audience = client.patch(f"{url}/audience", json={"audience": changed})
    if changed == "connections":
        assert audience.status_code == 403
        audience = client.patch(
            f"{url}/audience",
            json={
                "audience": changed,
                "owner_confirmed": True,
                "expected_updated_at": audience.json()["expected_updated_at"],
            },
        )
    assert audience.status_code == 200 and audience.json()["status"] == "pending_review"

    for params in ({"expected_updated_at": old_revision}, {}):
        refused = client.post(f"{url}/approve", json={"owner_confirmed": True, **params})
        assert refused.status_code == 403 and refused.json()["detail"] == CONFIRM
        assert refused.json()["expected_updated_at"] != old_revision
        label = {"connections": "Connections", "only_me": "Only me"}[changed]
        assert f"visible to {label}" in refused.json()["changes"][0]

    rejected_before_approval = client.get("/api/v1/profile").json()["facts"][0]
    assert rejected_before_approval["status"] == "pending_review"
    approved = client.post(
        f"{url}/approve",
        json={
            "owner_confirmed": True,
            "expected_updated_at": refused.json()["expected_updated_at"],
        },
    )
    assert approved.status_code == 200 and approved.json()["status"] == "active"
    for args in ({"action": "reject"}, {"owner_confirmed": True}):
        assert "fact_not_pending" in tool_error(
            mcp_call(client, "remove_fact", {"fact_id": fact_id, **args})
        )
    for args in ({}, {"owner_confirmed": False}):
        assert "action_required" in tool_error(
            mcp_call(client, "remove_fact", {"fact_id": fact_id, **args})
        )
    assert client.get("/api/v1/profile").json()["facts"][0]["status"] == "active"


def test_remove_fact_rejects_pending_and_removes_live(
    stack: tuple[TestClient, AuthRuntime, Any], factory: sessionmaker[Session]
) -> None:
    """The explicit action distinguishes rejecting a pending fact and removing a live fact."""
    client, runtime, _ = stack
    ok(
        mcp_call(
            client,
            "set_contribution_policy",
            {"expected_version": None, "require_review_all": True, "owner_confirmed": True},
        )
    )
    with ui_client(client, runtime) as ui:
        live = ui.post("/api/ui/profile/facts", json={"facts": [FACT]}, headers=ORIGIN)
        live_id = live.json()["results"][0]["claim_id"]
    pending = [
        ok(mcp_call(client, "add_facts", {"facts": [{**FACT, "statement": s}]}))["results"][0]
        for s in ("Owns a kettle", "Owns a grinder")
    ]
    assert {p["outcome"] for p in pending} == {"pending_review"}
    auto_id, explicit_id = (p["claim_id"] for p in pending)

    assert "fact_not_removable" in tool_error(
        mcp_call(client, "remove_fact", {"fact_id": auto_id, "action": "remove"})
    )
    assert client.delete(f"/api/v1/profile/facts/{auto_id}").status_code == 409
    rejected = ok(mcp_call(client, "remove_fact", {"fact_id": auto_id, "action": "reject"}))
    assert rejected["status"] == "removed"
    assert _events(factory, auto_id)[-1] == ("rejected", "automatic")
    assert "removed" not in [a for a, _ in _events(factory, auto_id)]

    confirmed = ok(
        mcp_call(client, "remove_fact", {"fact_id": explicit_id, "owner_confirmed": True})
    )
    assert confirmed["status"] == "removed"
    assert _events(factory, explicit_id)[-1] == ("rejected", "explicit")

    # A stale reject attempt against a live fact leaves no event behind.
    before = _events(factory, live_id)
    for args in ({"action": "reject"}, {"owner_confirmed": True}):
        assert "fact_not_pending" in tool_error(
            mcp_call(client, "remove_fact", {"fact_id": live_id, **args})
        )
    assert _events(factory, live_id) == before
    removed = ok(mcp_call(client, "remove_fact", {"fact_id": live_id, "action": "remove"}))
    assert removed["status"] == "removed"
    after = _events(factory, live_id)
    assert after[: len(before)] == before and after[len(before) :] == [("removed", "automatic")]

    # Repeats are the existing conflict error; nothing new is recorded.
    for fid, action in ((auto_id, "reject"), (live_id, "remove")):
        count = len(_events(factory, fid))
        assert "changed" in tool_error(
            mcp_call(client, "remove_fact", {"fact_id": fid, "action": action})
        )
        assert len(_events(factory, fid)) == count

    # A pending replacement is rejected and its target stays live.
    with ui_client(client, runtime) as ui:
        target = ui.post(
            "/api/ui/profile/facts",
            json={"facts": [{**FACT, "statement": "Owns a mug"}]},
            headers=ORIGIN,
        ).json()["results"][0]["claim_id"]
    (repl,) = ok(
        mcp_call(
            client,
            "add_facts",
            {"facts": [{**FACT, "statement": "Owns two mugs", "supersedes_id": target}]},
        )
    )["results"]
    assert repl["outcome"] == "pending_review"
    ok(mcp_call(client, "remove_fact", {"fact_id": repl["claim_id"], "action": "reject"}))
    assert _events(factory, repl["claim_id"])[-1][0] == "rejected"
    live_ids = {f["id"] for f in client.get("/api/v1/profile").json()["facts"]}
    assert target in live_ids and repl["claim_id"] not in live_ids


def _add_mcp(client: TestClient, item: dict[str, Any]) -> dict[str, Any]:
    (result,) = ok(mcp_call(client, "add_facts", {"facts": [item]}))["results"]
    return result


def _add_bearer(client: TestClient, item: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/api/v1/profile/facts", json={"facts": [item]})
    assert response.status_code == 200, response.text
    (result,) = response.json()["results"]
    return result


@pytest.mark.parametrize("add", [_add_mcp, _add_bearer], ids=["mcp", "bearer"])
def test_agent_corrections_never_widen_silently(
    stack: tuple[TestClient, AuthRuntime, Any], add: Any
) -> None:
    """Review B1 through each transport: inherit when omitted (also cross-category), cap at
    the category default, hold an explicit wider audience, accept equal or narrower."""
    client, _, _ = stack
    ok(
        mcp_call(
            client, "set_contribution_policy", {"expected_version": None, "owner_confirmed": True}
        )
    )
    interest = {"category": "interest", "evidence_type": "explicit"}
    private = add(client, {**interest, "statement": "Loves tea", "audience": "only_me"})
    assert private["audience"] == "only_me"

    def status_of(fact_id: str) -> str:
        facts = client.get(
            "/api/v1/profile", params={"status": ["active", "superseded", "pending_review"]}
        ).json()["facts"]
        return next(f["status"] for f in facts if f["id"] == fact_id)

    same_cat = add(
        client, {**interest, "statement": "Loves sencha", "supersedes_id": private["claim_id"]}
    )
    assert (same_cat["outcome"], same_cat["audience"]) == ("accepted", "only_me")
    assert status_of(private["claim_id"]) == "superseded"

    cross = add(
        client,
        {
            "category": "style",
            "statement": "Minimalist",
            "evidence_type": "explicit",
            "supersedes_id": same_cat["claim_id"],
        },
    )
    assert (cross["outcome"], cross["audience"]) == ("accepted", "only_me")

    wider = add(
        client,
        {
            "category": "style",
            "statement": "Scandinavian",
            "evidence_type": "explicit",
            "audience": "trusted_circle",
            "supersedes_id": cross["claim_id"],
        },
    )
    assert (wider["outcome"], wider["audience"]) == ("pending_review", "trusted_circle")
    assert status_of(cross["claim_id"]) == "active"
    refused = client.post(f"/api/v1/profile/facts/{wider['claim_id']}/approve")
    assert refused.status_code == 403 and refused.json()["detail"] == CONFIRM
    client.post(f"/api/v1/profile/facts/{wider['claim_id']}/reject")
    assert status_of(cross["claim_id"]) == "active"

    wide = add(client, {**interest, "statement": "Loves coffee"})
    assert wide["audience"] == "connections"
    capped = add(
        client,
        {
            "category": "size",
            "statement": "Wears M",
            "evidence_type": "explicit",
            "details": {"item": "shirt", "size": "M"},
            "supersedes_id": wide["claim_id"],
        },
    )
    assert (capped["outcome"], capped["audience"]) == ("accepted", "trusted_circle")
    assert status_of(wide["claim_id"]) == "superseded"

    narrower = add(
        client,
        {
            "category": "size",
            "statement": "Wears L",
            "evidence_type": "explicit",
            "details": {"item": "shirt", "size": "L"},
            "audience": "only_me",
            "supersedes_id": capped["claim_id"],
        },
    )
    assert (narrower["outcome"], narrower["audience"]) == ("accepted", "only_me")
    assert status_of(capped["claim_id"]) == "superseded"
