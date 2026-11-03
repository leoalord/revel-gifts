"""#15 follow-up: an agent granting access (approve, upgrade to Trusted Circle) needs the owner's
explicit ``owner_confirmed``; Revel records ``owner_confirmed_at`` (F3 migration 0005).

Real PostgreSQL with F4's in-memory OpenFGA fake. ``owner_confirmed_at`` is read with SQL so the
tests depend only on F3's column, not on a model attribute.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from authz_fakes import FakeFga
from conn_helpers import Person, add_person, seed_fga
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_connections_adapters_db import Acting, mcp_server  # noqa: F401 (fixture)
from test_connections_service_db import rows, tier_of

from revel.api.connections import error_response
from revel.auth.authz import ViewerTier
from revel.services import connections as svc

pytestmark = pytest.mark.anyio


def confirmed_at(session: Session, request_id: UUID) -> datetime | None:
    session.expire_all()
    return session.execute(
        text("SELECT owner_confirmed_at FROM connection_requests WHERE id = :id"),
        {"id": request_id},
    ).scalar_one()


def responded_at(session: Session, request_id: UUID) -> datetime | None:
    return session.execute(
        text("SELECT responded_at FROM connection_requests WHERE id = :id"), {"id": request_id}
    ).scalar_one()


async def _pending(
    session: Session, sessions: Any, fake_fga: FakeFga
) -> tuple[Person, Person, UUID]:
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    seed_fga(fake_fga, sam, alex)
    req = await svc.lookup_and_request(sam.agent, handle="alex_demo", sessions=sessions)
    return sam, alex, req.request.id


def _grant_tuples(fake_fga: FakeFga) -> set[Any]:
    return {t for t in fake_fga.tuples if t.relation in ("connections", "trusted_circle")}


async def test_agent_approve_without_confirmation_is_refused_before_any_write(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, alex, rid = await _pending(session, sessions, fake_fga)
    before = set(fake_fga.tuples)
    for tier, label in (("connections", "Connections"), ("trusted_circle", "Trusted Circle")):
        with pytest.raises(svc.OwnerConfirmationRequired) as exc:
            await svc.approve(alex.agent, rid, tier=tier, sessions=sessions)
        assert exc.value.changes == [f"Let Sam (@sam_demo) see facts you share with {label}"]
        assert isinstance(exc.value, svc.NotAllowed)
    (row,) = rows(session)
    assert row.status == "pending" and row.granted_tier is None
    assert row.responder_delegation_id is None
    assert confirmed_at(session, rid) is None
    assert fake_fga.tuples == before
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.none

    # owner_confirmed=False is the same as omitting it.
    with pytest.raises(svc.OwnerConfirmationRequired):
        await svc.approve(
            alex.agent, rid, tier="connections", owner_confirmed=False, sessions=sessions
        )

    response = error_response(svc.OwnerConfirmationRequired(["x"]))
    assert response.status_code == 403
    assert response.body == (
        b'{"detail":"owner_confirmation_required","code":"owner_confirmation_required",'
        b'"changes":["x"]}'
    )


async def test_agent_approve_with_confirmation_is_approved_and_recorded(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, alex, rid = await _pending(session, sessions, fake_fga)
    result = await svc.approve(
        alex.agent, rid, tier="trusted_circle", owner_confirmed=True, sessions=sessions
    )
    assert result == svc.ResponseResult(request_id=rid, status="approved", tier="trusted_circle")
    (row,) = rows(session)
    assert row.responder_delegation_id == alex.delegation.id
    stamp = confirmed_at(session, rid)
    assert stamp is not None and stamp == responded_at(session, rid)
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.trusted_circle


async def test_ui_approve_needs_no_flag_and_is_recorded(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, alex, rid = await _pending(session, sessions, fake_fga)
    await svc.approve(alex.ui, rid, tier="connections", sessions=sessions)
    (row,) = rows(session)
    assert row.status == "approved" and row.responder_delegation_id is None
    assert confirmed_at(session, rid) is not None
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.connections


async def test_agent_upgrade_needs_confirmation_and_downgrade_does_not(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, alex, rid = await _pending(session, sessions, fake_fga)
    await svc.approve(alex.ui, rid, tier="connections", sessions=sessions)
    first = confirmed_at(session, rid)
    grants = _grant_tuples(fake_fga)

    with pytest.raises(svc.OwnerConfirmationRequired) as exc:
        await svc.change_tier(alex.agent, rid, tier="trusted_circle", sessions=sessions)
    assert exc.value.changes == ["Let Sam (@sam_demo) see facts you share with Trusted Circle"]
    (row,) = rows(session)
    assert row.granted_tier == "connections"
    assert confirmed_at(session, rid) == first
    assert _grant_tuples(fake_fga) == grants
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.connections

    # An unchanged tier widens nothing.
    await svc.change_tier(alex.agent, rid, tier="connections", sessions=sessions)
    assert confirmed_at(session, rid) == first

    await svc.change_tier(
        alex.agent, rid, tier="trusted_circle", owner_confirmed=True, sessions=sessions
    )
    upgraded = confirmed_at(session, rid)
    assert upgraded is not None and first is not None and upgraded > first
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.trusted_circle

    # Downgrade: no flag, the confirmation time is kept.
    await svc.change_tier(alex.agent, rid, tier="connections", sessions=sessions)
    assert rows(session)[0].granted_tier == "connections"
    assert confirmed_at(session, rid) == upgraded
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.connections

    # The browser owner upgrades without a flag and is recorded.
    await svc.change_tier(alex.ui, rid, tier="trusted_circle", sessions=sessions)
    ui_upgraded = confirmed_at(session, rid)
    assert ui_upgraded is not None and ui_upgraded > upgraded


async def test_narrowing_actions_need_no_flag_and_block_clears_the_record(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, alex, rid = await _pending(session, sessions, fake_fga)
    await svc.approve(alex.agent, rid, tier="connections", owner_confirmed=True, sessions=sessions)
    stamp = confirmed_at(session, rid)
    await svc.revoke(alex.agent, rid, sessions=sessions)
    assert confirmed_at(session, rid) == stamp  # history kept on revoke

    again = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    await svc.approve(
        alex.agent, again.request.id, tier="connections", owner_confirmed=True, sessions=sessions
    )
    assert confirmed_at(session, again.request.id) is not None
    blocked = await svc.block(alex.agent, again.request.id, sessions=sessions)
    assert blocked.status == "blocked"
    assert confirmed_at(session, again.request.id) is None  # F3 0005 CHECK
    await svc.unblock(alex.agent, again.request.id, sessions=sessions)
    assert confirmed_at(session, again.request.id) is None

    third = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    declined = await svc.decline(alex.agent, third.request.id, sessions=sessions)
    assert declined.status == "declined"
    assert confirmed_at(session, third.request.id) is None

    fourth = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    cancelled = await svc.cancel_request(sam.agent, fourth.request.id, sessions=sessions)
    assert cancelled.status == "cancelled"


async def test_blocking_a_confirmed_approved_row_clears_the_record(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    """approved -> blocked must clear owner_confirmed_at in the same UPDATE (F3's CHECK
    ck_connection_requests_owner_confirmed_at_matches_status), so the block succeeds."""
    sam, alex, rid = await _pending(session, sessions, fake_fga)
    await svc.approve(alex.ui, rid, tier="trusted_circle", sessions=sessions)
    assert confirmed_at(session, rid) is not None
    result = await svc.block(alex.ui, rid, sessions=sessions)
    assert result == svc.ResponseResult(request_id=rid, status="blocked", tier=None)
    (row,) = rows(session)
    assert row.status == "blocked" and row.owner_confirmed_at is None
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.none


async def test_not_found_and_invalid_state_win_over_confirmation(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, alex, rid = await _pending(session, sessions, fake_fga)
    eve = add_person(session, "eve")
    for actor in (sam.agent, eve.agent):
        with pytest.raises(svc.RequestNotFound):
            await svc.approve(actor, rid, tier="trusted_circle", sessions=sessions)
        with pytest.raises(svc.RequestNotFound):
            await svc.change_tier(actor, rid, tier="trusted_circle", sessions=sessions)
    for fn in (svc.approve, svc.change_tier):
        with pytest.raises(svc.RequestNotFound):
            await fn(alex.agent, uuid4(), tier="trusted_circle", sessions=sessions)
    # Upgrading a pending request (not approved yet) is an invalid state, not a confirmation.
    with pytest.raises(svc.InvalidState):
        await svc.change_tier(alex.agent, rid, tier="trusted_circle", sessions=sessions)
    await svc.decline(alex.ui, rid, sessions=sessions)
    with pytest.raises(svc.InvalidState):
        await svc.approve(alex.agent, rid, tier="connections", sessions=sessions)
    assert [r.status for r in rows(session)] == ["declined"]
    assert _grant_tuples(fake_fga) == set()


async def test_block_succeeds_when_reverse_cancel_fails(
    session: Session, sessions, fake_fga: FakeFga, monkeypatch: pytest.MonkeyPatch
) -> None:
    sam, alex, rid = await _pending(session, sessions, fake_fga)
    reverse = await svc.lookup_and_request(alex.ui, handle="sam_demo", sessions=sessions)

    def boom(*_args: Any) -> None:
        raise RuntimeError("unexpected failure after the block committed")

    monkeypatch.setattr(svc, "_cancel_reverse_pending", boom)
    result = await svc.block(alex.ui, rid, sessions=sessions)
    assert result.status == "blocked"
    statuses = {r.id: r.status for r in rows(session)}
    assert statuses[rid] == "blocked"
    assert statuses[reverse.request.id] == "pending"  # left for reconcile/the owner, not an error
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.none


async def test_mcp_tools_quote_the_changes_and_take_the_flag(
    mcp_server: tuple[Any, Acting],  # noqa: F811 (the imported fixture)
    session: Session,
    fake_fga: FakeFga,
) -> None:
    server, acting = mcp_server
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    seed_fga(fake_fga, sam, alex)
    acting.principal = sam.agent
    req = await server.call_tool("request_connection", {"handle": "alex_demo"})
    rid = req.structured_content["request"]["id"]

    acting.principal = alex.agent

    async def error_text(name: str, args: dict[str, Any]) -> str:
        try:
            result = await server.call_tool(name, args)
        except ToolError as exc:
            return str(exc)
        assert result.is_error
        return result.content[0].text

    approve = {"request_id": rid, "action": "approve", "tier": "connections"}
    message = await error_text("respond_to_connection_request", approve)
    assert "owner_confirmation_required: this widens access. Ask the owner" in message
    assert "- Let Sam (@sam_demo) see facts you share with Connections" in message
    assert rows(session)[0].status == "pending"

    done = await server.call_tool(
        "respond_to_connection_request", {**approve, "owner_confirmed": True}
    )
    assert done.structured_content["status"] == "approved"

    upgrade = {"request_id": rid, "tier": "trusted_circle"}
    message = await error_text("change_connection_tier", upgrade)
    assert "- Let Sam (@sam_demo) see facts you share with Trusted Circle" in message
    assert rows(session)[0].granted_tier == "connections"
    done = await server.call_tool("change_connection_tier", {**upgrade, "owner_confirmed": True})
    assert done.structured_content["tier"] == "trusted_circle"
    done = await server.call_tool(
        "change_connection_tier", {"request_id": rid, "tier": "connections"}
    )
    assert done.structured_content["tier"] == "connections"

    tools = {t.name: t for t in await server.list_tools()}
    for name in ("respond_to_connection_request", "change_connection_tier"):
        schema = tools[name].input_schema["properties"]["owner_confirmed"]
        assert schema["default"] is False
        assert schema["description"] == svc.OWNER_CONFIRMED_DESCRIPTION
        assert "owner_confirmed" in tools[name].description
