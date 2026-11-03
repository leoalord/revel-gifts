"""HTTP routes and MCP tools for leases, answers and answer retrieval (#20), wired into test
apps. HTTP uses F4's in-memory OpenFGA fake (see ``test_questions_adapters_db``); the MCP tools
run against the ``fga`` fixture (real OpenFGA when reachable)."""

from __future__ import annotations

from typing import Any

import pytest
from authz_fakes import FakeFga
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from questions_support import PURPOSE, REQUESTED, World, agent_principal, requires_db
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_questions_adapters_db import fake_world, http_app, mcp_server, settings

from revel.db.models import Assignment, QuestionEvent
from revel.services.questions import QuestionDeps, configure_question_deps
from revel.settings import Settings

__all__ = ["fake_world", "settings"]

INLINE = {
    "category": "interest",
    "proposed_audience": "connections",
    "evidence_type": "explicit",
    "observed_month": "2026-09",
}


def assignment_for(factory: sessionmaker[Session], delegation_id: Any) -> str:
    with factory() as s:
        return str(
            s.scalars(
                select(Assignment.id).where(Assignment.respondent_delegation_id == delegation_id)
            ).one()
        )


@requires_db
def test_http_lease_answer_and_retrieval_flow(
    factory: sessionmaker[Session], settings: Settings, fake_world: tuple[World, FakeFga]
) -> None:
    world, _ = fake_world
    configure_question_deps(settings, QuestionDeps(sessionmaker=factory))
    with TestClient(http_app(settings, agent_principal(world.requester_d, via="api"))) as c:
        created = c.post(
            "/api/questions",
            json={
                "handle": "chelsea",
                "requested_information": REQUESTED,
                "private_purpose": PURPOSE,
            },
        )
    assert created.status_code == 201, created.text
    question_id = created.json()["question_id"]
    a1 = assignment_for(factory, world.owner_d1.id)
    a2 = assignment_for(factory, world.owner_d2.id)

    with TestClient(http_app(settings, agent_principal(world.owner_d1, via="api"))) as c:
        claim = c.post(f"/api/assignments/{a1}/claim", json={"human_contact": True})
        assert claim.status_code == 200, claim.text
        lease = claim.json()
        assert set(lease) == {
            "assignment_id",
            "question_id",
            "status",
            "lease_version",
            "lease_expires_at",
            "human_contact",
            "renewals_remaining",
            "prior_user_contacts",
        }
        contact = c.post(
            f"/api/assignments/{a1}/user-contact",
            json={"lease_version": lease["lease_version"], "delivery_confirmed": True},
        )
        assert contact.status_code == 200 and contact.json()["status"] == "waiting_for_user"
        # Stale completion -> 409 stale_lease.
        stale = c.post(
            f"/api/assignments/{a1}/answer",
            json={"lease_version": lease["lease_version"], "body": "Peonies", "evidence": [INLINE]},
        )
        assert stale.status_code == 409
        assert stale.json() == {
            "detail": {"code": "stale_lease", "message": stale.json()["detail"]["message"]}
        }
        bad = c.post(
            f"/api/assignments/{a1}/answer",
            json={
                "lease_version": contact.json()["lease_version"],
                "body": "Peonies",
                "evidence": [{**INLINE, "category": "address"}],
            },
        )
        assert bad.status_code == 422
        assert bad.json()["detail"]["code"] == "invalid_evidence"
        # Acting on someone else's assignment is an indistinct 404.
        assert c.post(f"/api/assignments/{a2}/claim").status_code == 404

    with TestClient(http_app(settings, agent_principal(world.owner_d2, via="api"))) as c:
        taken = c.post(f"/api/assignments/{a2}/claim", json={"human_contact": True})
        assert taken.status_code == 409 and taken.json()["detail"]["code"] == "human_contact_taken"
        declined = c.post(f"/api/assignments/{a2}/decline", json={"reason": "cannot_answer"})
        assert declined.status_code == 200 and declined.json()["status"] == "declined"

    with TestClient(http_app(settings, agent_principal(world.owner_d1, via="api"))) as c:
        done = c.post(
            f"/api/assignments/{a1}/answer",
            json={
                "lease_version": contact.json()["lease_version"],
                "body": "Peonies",
                "evidence": [INLINE],
            },
        )
        assert done.status_code == 201, done.text
        assert done.json()["question_status"] == "answered"
        # The respondent cannot read the requester's answer or status.
        assert c.get(f"/api/questions/{question_id}/answer").status_code == 404

    with TestClient(http_app(settings, agent_principal(world.requester_d, via="api"))) as c:
        status = c.get(f"/api/questions/{question_id}")
        assert status.status_code == 200
        assert status.json()["status"] == "answered"
        assert "Muse" not in status.text and "Claude" not in status.text
        answer = c.get(f"/api/questions/{question_id}/answer")
        assert answer.status_code == 200
        data = answer.json()
        assert data["result"] == "answer"
        assert data["answer"]["body"] == "Peonies"
        assert data["answer"]["respondent_agent"] == {"name": "Muse"}
        assert data["answer"]["evidence"][0]["category"] == "interest"
    with factory() as s:
        assert (
            len(
                s.scalars(
                    select(QuestionEvent).where(QuestionEvent.event_type == "answer_retrieved")
                ).all()
            )
            == 1
        )


@requires_db
def test_http_outreach_cooldown_has_retry_after(
    factory: sessionmaker[Session], settings: Settings, fake_world: tuple[World, FakeFga]
) -> None:
    world, _ = fake_world
    configure_question_deps(settings, QuestionDeps(sessionmaker=factory))
    with TestClient(http_app(settings, agent_principal(world.requester_d, via="api"))) as c:
        assert (
            c.post(
                "/api/questions",
                json={"handle": "chelsea", "requested_information": REQUESTED},
            ).status_code
            == 201
        )
    a1 = assignment_for(factory, world.owner_d1.id)
    a2 = assignment_for(factory, world.owner_d2.id)
    with TestClient(http_app(settings, agent_principal(world.owner_d1, via="api"))) as c:
        lease = c.post(f"/api/assignments/{a1}/claim", json={"human_contact": True}).json()
        waiting = c.post(
            f"/api/assignments/{a1}/user-contact",
            json={"lease_version": lease["lease_version"], "delivery_confirmed": False},
        ).json()
        released = c.post(
            f"/api/assignments/{a1}/release", json={"lease_version": waiting["lease_version"]}
        )
        assert released.status_code == 200 and released.json()["status"] == "released"
    with TestClient(http_app(settings, agent_principal(world.owner_d2, via="api"))) as c:
        refused = c.post(f"/api/assignments/{a2}/claim", json={"human_contact": True})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "outreach_cooldown"
    assert int(refused.headers["Retry-After"]) > 0


@pytest.mark.anyio
@requires_db
async def test_mcp_lease_answer_and_retrieval_tools(
    factory: sessionmaker[Session],
    settings: Settings,
    fga: Any,
    world_builder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await world_builder()
    configure_question_deps(settings, QuestionDeps(sessionmaker=factory))
    asker = mcp_server(settings, monkeypatch, agent_principal(world.requester_d))
    created = await asker.call_tool(
        "submit_question", {"handle": "chelsea", "requested_information": REQUESTED}
    )
    question_id = created.structured_content["question_id"]  # type: ignore[index]
    pending = await asker.call_tool("get_question", {"question_id": question_id})
    assert pending.structured_content["result"] == "not_answered"  # type: ignore[index]

    a1 = assignment_for(factory, world.owner_d1.id)
    responder = mcp_server(settings, monkeypatch, agent_principal(world.owner_d1))
    lease = (
        await responder.call_tool("update_assignment", {"assignment_id": a1, "action": "claim"})
    ).structured_content
    assert lease is not None and lease["status"] == "claimed"
    renewed = (
        await responder.call_tool(
            "update_assignment",
            {"assignment_id": a1, "action": "renew", "lease_version": lease["lease_version"]},
        )
    ).structured_content
    assert renewed is not None
    with pytest.raises(ToolError, match=r": stale_lease: "):
        await responder.call_tool(
            "submit_answer",
            {
                "assignment_id": a1,
                "lease_version": lease["lease_version"],
                "body": "Peonies",
                "evidence": [INLINE],
            },
        )
    with pytest.raises(ToolError, match=r": invalid_request: "):
        await responder.call_tool(
            "submit_answer",
            {
                "assignment_id": a1,
                "lease_version": renewed["lease_version"],
                "body": "Peonies",
                "evidence": [],
            },
        )
    answered = (
        await responder.call_tool(
            "submit_answer",
            {
                "assignment_id": a1,
                "lease_version": renewed["lease_version"],
                "body": "Peonies",
                "evidence": [{"claim_id": str(world.connections_claim)}, INLINE],
            },
        )
    ).structured_content
    assert answered is not None and answered["audience"] == "connections"
    with pytest.raises(ToolError, match=r": not_found: "):
        await responder.call_tool("get_question", {"question_id": question_id})

    # mcp_server patches the module-level principal resolver: rebuild the requester's server.
    asker = mcp_server(settings, monkeypatch, agent_principal(world.requester_d))
    status = (
        await asker.call_tool("get_question", {"question_id": question_id})
    ).structured_content
    assert status is not None and status["status"] == "answered"
    released = (
        await asker.call_tool("get_question", {"question_id": question_id})
    ).structured_content
    assert released is not None and released["result"] == "answer"
    assert [e["kind"] for e in released["answer"]["evidence"]] == ["claim", "inline"]


@pytest.mark.anyio
@requires_db
async def test_mcp_update_assignment_other_actions(
    factory: sessionmaker[Session],
    settings: Settings,
    fga: Any,
    world_builder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await world_builder()
    configure_question_deps(settings, QuestionDeps(sessionmaker=factory))
    asker = mcp_server(settings, monkeypatch, agent_principal(world.requester_d))
    await asker.call_tool(
        "submit_question", {"handle": "chelsea", "requested_information": REQUESTED}
    )
    a1 = assignment_for(factory, world.owner_d1.id)
    a2 = assignment_for(factory, world.owner_d2.id)
    one = mcp_server(settings, monkeypatch, agent_principal(world.owner_d1))
    lease = (
        await one.call_tool(
            "update_assignment", {"assignment_id": a1, "action": "claim", "human_contact": True}
        )
    ).structured_content
    assert lease is not None and lease["human_contact"] is True
    waiting = (
        await one.call_tool(
            "update_assignment",
            {
                "assignment_id": a1,
                "action": "report_user_contact",
                "lease_version": lease["lease_version"],
                "delivery_confirmed": True,
            },
        )
    ).structured_content
    assert waiting is not None and waiting["status"] == "waiting_for_user"
    released = (
        await one.call_tool(
            "update_assignment",
            {"assignment_id": a1, "action": "release", "lease_version": waiting["lease_version"]},
        )
    ).structured_content
    assert released is not None and released["status"] == "released"
    two = mcp_server(settings, monkeypatch, agent_principal(world.owner_d2))
    declined = (
        await two.call_tool(
            "update_assignment",
            {"assignment_id": a2, "action": "decline", "reason": "not_appropriate"},
        )
    ).structured_content
    assert declined is not None and declined["status"] == "declined"
