"""F6 review round 1: lookup-limited ``set_handle`` (B1), row write before revoke hooks (O1),
unblock Tx2 race (O2), target-inserted blocks outside the requester's quota (O3) and
``resolve_handle`` recording only misses (O6)."""

import anyio
import pytest
from authz_fakes import FakeFga
from conn_helpers import add_person, seed_fga
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_connections_service_db import _approved_pair, rows, tier_of

from revel.auth import authz
from revel.auth.authz import ViewerTier, blocked_tuple
from revel.db.models import ConnectionRequest
from revel.services import connections as svc

pytestmark = pytest.mark.anyio


async def test_set_handle_with_a_handle_is_lookup_limited(
    session: Session, sessions, fake_fga: FakeFga, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1: "unavailable" reveals occupancy (even of undiscoverable accounts), so it is limited."""
    sam = add_person(session, "sam")
    add_person(session, "hidden", discoverable=False)
    monkeypatch.setattr(svc, "LOOKUP_LIMIT", 2)
    with pytest.raises(svc.HandleUnavailable):
        await svc.set_handle(sam.ui, handle="hidden_demo", sessions=sessions)
    await svc.set_handle(sam.ui, handle="sam_free_1", sessions=sessions)
    outcomes = []
    for handle in ("hidden_demo", "sam_free_2", "bad!"):
        with pytest.raises(svc.RateLimited) as exc:
            await svc.set_handle(sam.ui, handle=handle, sessions=sessions)
        outcomes.append(exc.value.args)
    assert len(set(outcomes)) == 1
    # Changing only discoverability or the avatar is not a lookup.
    info = await svc.set_handle(
        sam.ui, discoverable=False, avatar_url="https://example.test/s.png", sessions=sessions
    )
    assert (info.handle, info.discoverable) == ("sam_free_1", False)


async def test_resolve_handle_counts_only_not_found(
    session: Session, sessions, fake_fga: FakeFga, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O6: connected reads are not throttled; misses spend quota; over the limit all refuse."""
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    add_person(session, "hidden", discoverable=False)
    monkeypatch.setattr(svc, "LOOKUP_LIMIT", 2)
    for _ in range(5):
        resolved = await svc.resolve_handle(session, sam.ui, handle="alex_demo")
        assert resolved.user_id == alex.user.id
    misses = []
    for handle in ("nobody_here", "hidden_demo"):
        with pytest.raises(svc.TargetNotFound) as exc:
            await svc.resolve_handle(session, sam.ui, handle=handle)
        misses.append(exc.value.args)
    assert misses[0] == misses[1]
    limited = []
    for handle in ("alex_demo", "nobody_here", "hidden_demo"):
        with pytest.raises(svc.RateLimited) as exc:
            await svc.resolve_handle(session, sam.ui, handle=handle)
        limited.append(exc.value.args)
    assert len(set(limited)) == 1
    assert rows(session) == []


async def test_target_inserted_blocks_do_not_use_the_blocked_users_quota(
    session: Session, sessions, fake_fga: FakeFga, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O3: only rows the requester created count towards REQUEST_LIMIT."""
    sam = add_person(session, "sam")
    blockers = [add_person(session, f"b{i}") for i in range(3)]
    add_person(session, "alex")
    monkeypatch.setattr(svc, "REQUEST_LIMIT", 2)
    for b in blockers:
        # Sam's request (moved out of the window), declined, then blocked: the block inserts a
        # new blocked row naming Sam as requester, inside the window.
        req = await svc.lookup_and_request(sam.ui, handle=b.handle, sessions=sessions)
        await svc.decline(b.ui, req.request.id, sessions=sessions)
        session.execute(
            text(
                "UPDATE connection_requests SET created_at = now() - interval '2 hours' "
                "WHERE status = 'declined'"
            )
        )
        session.commit()
        await svc.block(b.ui, req.request.id, sessions=sessions)
    assert sum(r.status == "blocked" for r in rows(session)) == 3
    assert (await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)).created

    # A request the requester created that was blocked later still counts.
    sam2 = add_person(session, "sam2")
    t1 = add_person(session, "t1")
    add_person(session, "t2")
    r1 = await svc.lookup_and_request(sam2.ui, handle="t1_demo", sessions=sessions)
    await svc.block(t1.ui, r1.request.id, sessions=sessions)
    await svc.lookup_and_request(sam2.ui, handle="t2_demo", sessions=sessions)
    with pytest.raises(svc.RateLimited):
        await svc.lookup_and_request(sam2.ui, handle="alex_demo", sessions=sessions)


HOOKS = {
    "revoke": "on_connection_revoked",
    "block": "on_blocked",
    "block_insert": "on_blocked",
    "tier": "on_connection_tier_changing",
}


@pytest.mark.parametrize("action", sorted(HOOKS))
async def test_row_write_precedes_revoke_hook(
    session: Session,
    sessions,
    engine,
    fake_fga: FakeFga,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """O1: when the revoke-class hook runs, the changing transaction has already written the
    row (it sees the new status, and holds ROW EXCLUSIVE on connection_requests)."""
    _sam, alex, rid = await _approved_pair(session, sessions, fake_fga)
    if action == "block_insert":
        await svc.revoke(alex.ui, rid, sessions=sessions)  # no live row: block INSERTs
    expected = {"revoke": "revoked", "tier": "approved"}.get(action, "blocked")
    real_hook = getattr(authz, HOOKS[action])
    real_update = svc._update_row
    real_profile_id = svc._profile_id
    events: list[str] = []
    seen_in_tx: list[str] = []

    def update_row(session_, request_id, **values):
        events.append("update")
        return real_update(session_, request_id, **values)

    def profile_id(session_, user_id):
        # Runs in the changing transaction right before the hook: record what it sees.
        statuses = session_.execute(
            select(ConnectionRequest.status).where(ConnectionRequest.target_user_id == alex.user.id)
        ).scalars()
        seen_in_tx.append(",".join(sorted(statuses)))
        return real_profile_id(session_, user_id)

    async def hook(**kwargs):
        events.append("hook")
        with engine.connect() as other:  # a separate session inspecting our transaction
            held = other.execute(
                text(
                    "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                    "WHERE c.relname = 'connection_requests' AND l.mode = 'RowExclusiveLock' "
                    "AND l.granted AND l.pid <> pg_backend_pid()"
                )
            ).scalar_one()
        assert held >= 1, "revoke-class hook ran before the row write"
        return await real_hook(**kwargs)

    monkeypatch.setattr(svc, "_update_row", update_row)
    monkeypatch.setattr(svc, "_profile_id", profile_id)
    monkeypatch.setattr(authz, HOOKS[action], hook)
    if action == "revoke":
        await svc.revoke(alex.ui, rid, sessions=sessions)
    elif action == "tier":
        await svc.change_tier(alex.ui, rid, tier="connections", sessions=sessions)
    else:
        await svc.block(alex.ui, rid, sessions=sessions)

    assert events.count("hook") == 1
    if action != "block_insert":  # the insert path writes with INSERT, not _update_row
        assert events.index("update") < events.index("hook")
    if action != "tier":  # tier change reads the profile before its UPDATE
        assert expected in seen_in_tx[-1].split(",")
    assert any(r.status == expected for r in rows(session))


async def test_unblock_tx2_does_not_undo_a_reblock(
    session: Session, sessions, fake_fga: FakeFga, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O2: unblock, then a re-block commits before unblock's Tx2: the blocked tuple stays."""
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    seed_fga(fake_fga, sam, alex)
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    await svc.block(alex.ui, req.request.id, sessions=sessions)
    real_unblock = svc._unblock_sync
    raced: list[bool] = []

    async def reblock() -> None:
        await svc.block(alex.ui, req.request.id, sessions=sessions)

    def reblock_first(factory, requester, target):
        if not raced:
            raced.append(True)
            anyio.from_thread.run(reblock)
        return real_unblock(factory, requester, target)

    monkeypatch.setattr(svc, "_unblock_sync", reblock_first)
    await svc.unblock(alex.ui, req.request.id, sessions=sessions)
    assert raced
    assert blocked_tuple(alex.profile.id, sam.user.id) in fake_fga.tuples
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.none
    assert sorted(r.status for r in rows(session)) == ["blocked", "revoked"]
    with pytest.raises(svc.TargetNotFound):
        await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
