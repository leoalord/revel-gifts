"""F6 connection service against real PostgreSQL with F4's in-memory OpenFGA fake.

Covers task #15's acceptance: minimal identity and create-or-return; identical hidden-target
responses; duplicates (incl. concurrent) and rate limits; directional approval with tiers; tier
change, revocation and block applying on the next check; unauthorized responders; agents of the
target; disconnected delegations; Postgres/OpenFGA consistency after simulated failures
(rollback on revoke-class failures, reconcile restoring a failed post-commit grant, over-deny
after a failed commit); and per-pair serialization of a delayed grant against a revoke.
"""

import threading
from datetime import timedelta
from uuid import uuid4

import anyio
import pytest
from authz_fakes import FakeFga
from conn_helpers import Person, add_person, seed_fga
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from revel.auth import authz, fga_reconcile
from revel.auth.authz import (
    ViewerTier,
    blocked_tuple,
    owner_tuple,
    resolve_viewer_tier,
    tier_tuple,
)
from revel.db.enums import ConnectionTier
from revel.db.models import ConnectionRequest, Profile
from revel.services import connections as svc

pytestmark = pytest.mark.anyio


async def tier_of(fga: FakeFga, viewer, owner: Person) -> ViewerTier:
    assert owner.profile is not None
    return await resolve_viewer_tier(fga, viewer, owner.profile.id)


def rows(session: Session) -> list[ConnectionRequest]:
    session.expire_all()
    return list(session.scalars(select(ConnectionRequest).order_by(ConnectionRequest.created_at)))


async def reconcile(fga: FakeFga, factory: sessionmaker[Session]) -> None:
    await fga_reconcile.reconcile(fga, lambda: factory, window=timedelta(0))


# --- Handles ----------------------------------------------------------------------------------


async def test_set_handle_validates_normalizes_and_hides_conflicts(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    alex = add_person(session, "alex", handle=False)
    sam = add_person(session, "sam", handle=False)
    assert await svc.get_my_handle(alex.ui, sessions=sessions) is None
    with pytest.raises(svc.InvalidHandle):
        await svc.set_handle(alex.ui, handle="no", sessions=sessions)
    with pytest.raises(svc.InvalidHandle):
        await svc.set_handle(alex.ui, handle="bad-handle!", sessions=sessions)
    with pytest.raises(svc.InvalidHandle):  # the first call needs a handle
        await svc.set_handle(alex.ui, discoverable=False, sessions=sessions)
    with pytest.raises(svc.InvalidAvatarUrl):
        await svc.set_handle(
            alex.ui, handle="alex_demo", avatar_url="javascript:alert(1)", sessions=sessions
        )

    info = await svc.set_handle(alex.agent, handle=" @Alex_Demo ", sessions=sessions)
    assert info == svc.HandleInfo(handle="alex_demo", discoverable=True, avatar_url=None)
    with pytest.raises(svc.HandleUnavailable) as taken:
        await svc.set_handle(sam.ui, handle="ALEX_DEMO", sessions=sessions)
    assert str(taken.value) == "That handle is unavailable"

    info = await svc.set_handle(
        alex.ui, discoverable=False, avatar_url="https://example.test/a.png", sessions=sessions
    )
    assert info == svc.HandleInfo(
        handle="alex_demo", discoverable=False, avatar_url="https://example.test/a.png"
    )
    info = await svc.set_handle(alex.ui, handle="alex_new", sessions=sessions)
    assert info.avatar_url == "https://example.test/a.png" and not info.discoverable
    assert (await svc.set_handle(alex.ui, avatar_url=None, sessions=sessions)).avatar_url is None


# --- Lookup and request -----------------------------------------------------------------------


async def test_lookup_returns_minimal_identity_and_creates_or_returns_request(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    first = await svc.lookup_and_request(sam.agent, handle="Alex_Demo", sessions=sessions)
    assert first.created
    assert first.target.model_dump() == {
        "display_name": "Alex",
        "handle": "alex_demo",
        "avatar_url": "https://example.test/alex.png",
    }
    assert first.request.status == "pending"
    (row,) = rows(session)
    assert (row.requester_user_id, row.target_user_id) == (sam.user.id, alex.user.id)
    assert row.requester_delegation_id == sam.delegation.id  # acting assistant recorded

    again = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    assert not again.created and again.request == first.request
    assert len(rows(session)) == 1

    await svc.approve(alex.ui, first.request.id, tier="trusted_circle", sessions=sessions)
    approved = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    assert not approved.created and approved.request.status == "approved"
    assert "tier" not in approved.model_dump_json()  # the requester never sees the tier
    assert len(rows(session)) == 1


async def test_hidden_targets_are_identical(session: Session, sessions, fake_fga: FakeFga) -> None:
    sam = add_person(session, "sam")
    add_person(session, "hidden", discoverable=False)
    blocker = add_person(session, "blocker")
    req = await svc.lookup_and_request(sam.ui, handle="blocker_demo", sessions=sessions)
    await svc.block(blocker.ui, req.request.id, sessions=sessions)
    before = len(rows(session))

    async def attempt(handle: str) -> tuple[type, str, tuple]:
        with pytest.raises(svc.ConnectionsError) as exc:
            await svc.lookup_and_request(sam.ui, handle=handle, sessions=sessions)
        return type(exc.value), str(exc.value), exc.value.args

    outcomes = {
        "nonexistent": await attempt("nobody_here"),
        "undiscoverable": await attempt("hidden_demo"),
        "blocked": await attempt("blocker_demo"),
        "self": await attempt("sam_demo"),
        "malformed": await attempt("x"),
    }
    assert len(set(outcomes.values())) == 1, outcomes
    assert outcomes["nonexistent"][0] is svc.TargetNotFound
    assert len(rows(session)) == before  # nothing created

    # The shared resolver: the same single error for every hidden case; nothing created.
    resolved = []
    for handle in ("nobody_here", "hidden_demo", "blocker_demo"):
        with pytest.raises(svc.TargetNotFound) as exc:
            await svc.resolve_handle(session, sam.ui, handle=handle)
        resolved.append((type(exc.value), exc.value.args))
    assert len(set(resolved)) == 1
    assert len(rows(session)) == before


async def test_resolve_handle_connected_viewer_and_self(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    await svc.approve(alex.ui, req.request.id, tier="connections", sessions=sessions)
    await svc.set_handle(alex.ui, discoverable=False, sessions=sessions)

    target = await svc.resolve_handle(session, sam.agent, handle="ALEX_DEMO")
    assert target == svc.ResolvedTarget(
        user_id=alex.user.id,
        profile_id=alex.profile.id,
        display_name="Alex",
        handle="alex_demo",
        avatar_url="https://example.test/alex.png",
    )
    me = await svc.resolve_handle(session, sam.ui, handle="sam_demo")
    assert me.user_id == sam.user.id

    # A stranger cannot find the undiscoverable target; revocation removes the exception too.
    stranger = add_person(session, "stranger")
    with pytest.raises(svc.TargetNotFound):
        await svc.resolve_handle(session, stranger.ui, handle="alex_demo")
    await svc.revoke(alex.ui, req.request.id, sessions=sessions)
    with pytest.raises(svc.TargetNotFound):
        await svc.resolve_handle(session, sam.ui, handle="alex_demo")
    assert len(rows(session)) == 1


async def test_concurrent_duplicate_requests_create_one_row(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, _ = add_person(session, "sam"), add_person(session, "alex")
    results: list[svc.RequestResult] = []

    async def go() -> None:
        results.append(await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions))

    async with anyio.create_task_group() as tg:
        for _ in range(6):
            tg.start_soon(go)
    assert len({r.request.id for r in results}) == 1
    assert sum(r.created for r in results) == 1
    assert len(rows(session)) == 1


async def test_rate_limits(
    session: Session, sessions, fake_fga: FakeFga, monkeypatch: pytest.MonkeyPatch
) -> None:
    sam = add_person(session, "sam")
    for name in ("a1", "a2", "a3"):
        add_person(session, name)
    add_person(session, "hidden", discoverable=False)
    monkeypatch.setattr(svc, "REQUEST_LIMIT", 2)
    await svc.lookup_and_request(sam.ui, handle="a1_demo", sessions=sessions)
    r2 = await svc.lookup_and_request(sam.ui, handle="a2_demo", sessions=sessions)
    # A cancelled request still counts (the window counts created rows).
    await svc.cancel_request(sam.ui, r2.request.id, sessions=sessions)

    outcomes = []
    for handle in ("a3_demo", "nobody_here", "hidden_demo", "a1_demo"):
        with pytest.raises(svc.RateLimited) as exc:
            await svc.lookup_and_request(sam.ui, handle=handle, sessions=sessions)
        outcomes.append(exc.value.args)
    # Checked before resolution: existing, hidden and nonexistent targets look the same.
    assert len(set(outcomes)) == 1
    assert len(rows(session)) == 2

    # Outside the window the requester may ask again.
    session.execute(text("UPDATE connection_requests SET created_at = now() - interval '2 hours'"))
    session.commit()
    assert (await svc.lookup_and_request(sam.ui, handle="a3_demo", sessions=sessions)).created


async def test_lookup_limit_counts_failed_lookups(
    session: Session, sessions, fake_fga: FakeFga, monkeypatch: pytest.MonkeyPatch
) -> None:
    sam = add_person(session, "sam")
    add_person(session, "alex")
    monkeypatch.setattr(svc, "LOOKUP_LIMIT", 3)
    for handle in ("nope_1", "nope_2", "nope_3"):
        with pytest.raises(svc.TargetNotFound):
            await svc.lookup_and_request(sam.ui, handle=handle, sessions=sessions)
    with pytest.raises(svc.RateLimited):
        await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    with pytest.raises(svc.RateLimited):
        await svc.resolve_handle(session, sam.ui, handle="alex_demo")
    assert rows(session) == []


# --- Approval, tiers, revocation --------------------------------------------------------------


async def test_approval_is_directional_and_tiers_apply_on_next_check(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    seed_fga(fake_fga, sam, alex)
    req = await svc.lookup_and_request(sam.agent, handle="alex_demo", sessions=sessions)
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.none

    result = await svc.approve(alex.ui, req.request.id, tier="connections", sessions=sessions)
    assert result == svc.ResponseResult(
        request_id=req.request.id, status="approved", tier="connections"
    )
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.connections
    assert await tier_of(fake_fga, sam.agent, alex) is ViewerTier.connections  # Sam's agent
    assert await tier_of(fake_fga, alex.ui, sam) is ViewerTier.none  # directional
    assert await tier_of(fake_fga, alex.agent, sam) is ViewerTier.none

    await svc.change_tier(alex.ui, req.request.id, tier="trusted_circle", sessions=sessions)
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.trusted_circle
    await svc.change_tier(alex.agent, req.request.id, tier="connections", sessions=sessions)
    assert await tier_of(fake_fga, sam.agent, alex) is ViewerTier.connections
    assert tier_tuple(alex.profile.id, sam.user.id, ConnectionTier.TRUSTED_CIRCLE) not in (
        fake_fga.tuples
    )

    (row,) = rows(session)
    assert row.responder_delegation_id == alex.delegation.id  # last response via Alex's agent

    await svc.revoke(alex.ui, req.request.id, sessions=sessions)
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.none
    assert rows(session)[0].status == "revoked" and rows(session)[0].granted_tier is None
    with pytest.raises(svc.InvalidState):
        await svc.revoke(alex.ui, req.request.id, sessions=sessions)

    # Re-request after revocation creates a new row.
    again = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    assert again.created and again.request.id != req.request.id


async def test_decline_cancel_and_invalid_transitions(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    with pytest.raises(svc.InvalidState):
        await svc.change_tier(alex.ui, req.request.id, tier="connections", sessions=sessions)
    with pytest.raises(svc.InvalidTier):
        await svc.approve(alex.ui, req.request.id, tier="only_me", sessions=sessions)
    declined = await svc.decline(alex.agent, req.request.id, sessions=sessions)
    assert declined.status == "declined"
    with pytest.raises(svc.InvalidState):
        await svc.approve(alex.ui, req.request.id, tier="connections", sessions=sessions)

    again = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    assert again.created
    cancelled = await svc.cancel_request(sam.agent, again.request.id, sessions=sessions)
    assert cancelled.status == "cancelled"
    assert await svc.list_incoming(alex.ui, sessions=sessions) == []
    assert all(r.responded_at is not None for r in rows(session))


async def test_only_the_target_may_respond(session: Session, sessions, fake_fga: FakeFga) -> None:
    sam, alex, eve = (add_person(session, n) for n in ("sam", "alex", "eve"))
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    rid = req.request.id
    for actor in (sam.ui, sam.agent, eve.ui, eve.agent):
        for action in (svc.decline, svc.block, svc.revoke, svc.unblock):
            with pytest.raises(svc.RequestNotFound):
                await action(actor, rid, sessions=sessions)
        with pytest.raises(svc.RequestNotFound):
            await svc.approve(actor, rid, tier="trusted_circle", sessions=sessions)
    for actor in (alex.ui, eve.ui):  # only the requester cancels
        with pytest.raises(svc.RequestNotFound):
            await svc.cancel_request(actor, rid, sessions=sessions)
    with pytest.raises(svc.RequestNotFound):  # unknown id: same error
        await svc.decline(alex.ui, uuid4(), sessions=sessions)
    assert rows(session)[0].status == "pending"
    assert fake_fga.tuples == set()


async def test_disconnected_delegation_cannot_act(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam = add_person(session, "sam")
    alex = add_person(session, "alex", active=False)
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    with pytest.raises(svc.NotAllowed):
        await svc.approve(alex.agent, req.request.id, tier="connections", sessions=sessions)
    with pytest.raises(svc.NotAllowed):
        await svc.lookup_and_request(alex.agent, handle="sam_demo", sessions=sessions)
    with pytest.raises(svc.NotAllowed):
        await svc.list_incoming(alex.agent, sessions=sessions)
    assert rows(session)[0].status == "pending"


def profile_of(session: Session, person: Person):
    session.expire_all()
    return session.execute(
        select(Profile.id).where(Profile.user_id == person.user.id)
    ).scalar_one_or_none()


async def test_approval_creates_the_approvers_profile_and_grants(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam = add_person(session, "sam")
    alex = add_person(session, "alex", profile=False)
    seed_fga(fake_fga, sam, alex)
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    assert profile_of(session, alex) is None
    await svc.approve(
        alex.agent, req.request.id, tier="connections", owner_confirmed=True, sessions=sessions
    )
    pid = profile_of(session, alex)
    assert pid is not None
    assert owner_tuple(alex.user.id, pid) in fake_fga.tuples  # F5/F4's on_profile_created
    assert await resolve_viewer_tier(fake_fga, sam.ui, pid) is ViewerTier.connections
    assert await resolve_viewer_tier(fake_fga, alex.ui, pid) is ViewerTier.only_me


async def test_block_without_a_profile_writes_the_blocked_tuple_at_once(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam = add_person(session, "sam")
    alex = add_person(session, "alex", profile=False)
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    assert (await svc.block(alex.ui, req.request.id, sessions=sessions)).status == "blocked"
    pid = profile_of(session, alex)
    assert pid is not None
    assert blocked_tuple(pid, sam.user.id) in fake_fga.tuples
    with pytest.raises(svc.TargetNotFound):
        await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)


async def test_profile_creation_failure_rolls_back_approval(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam = add_person(session, "sam")
    alex = add_person(session, "alex", profile=False)
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    fake_fga.error = RuntimeError("synthetic OpenFGA outage")  # on_profile_created fails
    with pytest.raises(svc.ConnectionsUnavailable):
        await svc.approve(alex.ui, req.request.id, tier="connections", sessions=sessions)
    fake_fga.error = None
    assert rows(session)[0].status == "pending"
    assert profile_of(session, alex) is None  # rolled back with the approval


# --- Blocks -----------------------------------------------------------------------------------


async def test_block_hides_target_removes_access_and_unblock_restores_no_tier(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    seed_fga(fake_fga, sam, alex)
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    await svc.approve(alex.ui, req.request.id, tier="trusted_circle", sessions=sessions)
    # Alex also asked Sam (pending): blocking Sam cancels it.
    reverse = await svc.lookup_and_request(alex.ui, handle="sam_demo", sessions=sessions)

    blocked = await svc.block(alex.agent, req.request.id, sessions=sessions)
    assert blocked == svc.ResponseResult(request_id=req.request.id, status="blocked", tier=None)
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.none
    assert await tier_of(fake_fga, sam.agent, alex) is ViewerTier.none
    assert blocked_tuple(alex.profile.id, sam.user.id) in fake_fga.tuples
    by_id = {r.id: r for r in rows(session)}
    assert by_id[reverse.request.id].status == "cancelled"
    assert (await svc.block(alex.ui, req.request.id, sessions=sessions)).status == "blocked"

    # Sam sees Alex exactly like a nonexistent handle, and no trace of the request.
    with pytest.raises(svc.TargetNotFound):
        await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    assert await svc.list_outgoing(sam.ui, sessions=sessions) == []
    assert [b.request_id for b in await svc.list_blocked(alex.ui, sessions=sessions)] == [
        req.request.id
    ]

    unblocked = await svc.unblock(alex.ui, req.request.id, sessions=sessions)
    assert unblocked.status == "revoked"
    assert blocked_tuple(alex.profile.id, sam.user.id) not in fake_fga.tuples
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.none  # no tier restored
    again = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    assert again.created and again.request.status == "pending"


async def test_block_after_decline_inserts_blocked_row(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    await svc.decline(alex.ui, req.request.id, sessions=sessions)
    blocked = await svc.block(alex.ui, req.request.id, sessions=sessions)
    assert blocked.status == "blocked" and blocked.request_id != req.request.id
    statuses = sorted(r.status for r in rows(session))
    assert statuses == ["blocked", "declined"]
    with pytest.raises(svc.TargetNotFound):
        await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)


# --- Lists ------------------------------------------------------------------------------------


async def test_lists(session: Session, sessions, fake_fga: FakeFga) -> None:
    sam, alex, cal = (add_person(session, n) for n in ("sam", "alex", "cal"))
    r1 = await svc.lookup_and_request(sam.agent, handle="alex_demo", sessions=sessions)
    r2 = await svc.lookup_and_request(cal.ui, handle="alex_demo", sessions=sessions)

    incoming = await svc.list_incoming(alex.ui, sessions=sessions)
    by_handle = {i.requester.handle: i for i in incoming}
    assert by_handle["sam_demo"].requester.assistant == svc.AssistantInfo(
        name="Muse", client_id="client_muse"
    )
    assert by_handle["cal_demo"].requester.assistant is None  # asked from the UI
    assert by_handle["sam_demo"].requester.display_name == "Sam"

    await svc.approve(alex.ui, r1.request.id, tier="trusted_circle", sessions=sessions)
    await svc.decline(alex.ui, r2.request.id, sessions=sessions)
    conns = await svc.list_connections(alex.ui, sessions=sessions)
    assert [(c.request_id, c.tier, c.person.handle) for c in conns] == [
        (r1.request.id, "trusted_circle", "sam_demo")
    ]
    assert await svc.list_connections(sam.ui, sessions=sessions) == []  # directional
    out = await svc.list_outgoing(sam.ui, sessions=sessions)
    assert [(o.id, o.status, o.target.handle) for o in out] == [
        (r1.request.id, "approved", "alex_demo")
    ]
    assert await svc.list_outgoing(cal.ui, sessions=sessions) == []  # declined: not listed
    assert await svc.list_incoming(alex.ui, sessions=sessions) == []


# --- Consistency after simulated failures -----------------------------------------------------


class FailingFga(FakeFga):
    """Fails the listed operations (``write`` / ``delete``) while ``fail`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.fail: set[str] = set()

    def _call(self, name: str) -> None:
        super()._call(name)
        if name in self.fail:
            raise RuntimeError("synthetic OpenFGA outage")


@pytest.fixture
def failing_fga():
    fga = FailingFga()
    authz.configure_authz(fga)
    yield fga
    authz.reset_authz()


async def _approved_pair(session, sessions, fga, tier="trusted_circle"):
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    seed_fga(fga, sam, alex)
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    await svc.approve(alex.ui, req.request.id, tier=tier, sessions=sessions)
    return sam, alex, req.request.id


async def test_revoke_and_block_hook_failures_roll_back(
    session: Session, sessions, failing_fga: FailingFga
) -> None:
    sam, alex, rid = await _approved_pair(session, sessions, failing_fga)
    granted = tier_tuple(alex.profile.id, sam.user.id, ConnectionTier.TRUSTED_CIRCLE)
    assert granted in failing_fga.tuples

    failing_fga.fail = {"delete", "write"}
    for action in (svc.revoke, svc.block):
        with pytest.raises(svc.ConnectionsUnavailable):
            await action(alex.ui, rid, sessions=sessions)
        (row,) = rows(session)
        assert (row.status, row.granted_tier) == ("approved", "trusted_circle")
    with pytest.raises(svc.ConnectionsUnavailable):  # downgrade's pre-commit delete fails
        await svc.change_tier(alex.ui, rid, tier="connections", sessions=sessions)
    assert rows(session)[0].granted_tier == "trusted_circle"

    failing_fga.fail = set()
    assert granted in failing_fga.tuples  # consistent: approved in both
    await svc.revoke(alex.ui, rid, sessions=sessions)
    assert granted not in failing_fga.tuples


async def test_unconfigured_openfga_fails_closed_for_revocation(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    _sam, alex, rid = await _approved_pair(session, sessions, fake_fga)
    authz.reset_authz()
    with pytest.raises(svc.ConnectionsUnavailable):
        await svc.revoke(alex.ui, rid, sessions=sessions)
    assert rows(session)[0].status == "approved"


async def test_failed_post_commit_grant_is_restored_by_reconcile(
    session: Session, sessions, factory, failing_fga: FailingFga
) -> None:
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    seed_fga(failing_fga, sam, alex)
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    failing_fga.fail = {"write"}  # the pre-commit half only deletes; the grant writes
    result = await svc.approve(alex.ui, req.request.id, tier="connections", sessions=sessions)
    assert result.status == "approved"  # committed; the request still succeeds
    assert rows(session)[0].status == "approved"
    assert await tier_of(failing_fga, sam.ui, alex) is ViewerTier.none  # only denies

    failing_fga.fail = set()
    await reconcile(failing_fga, factory)
    assert await tier_of(failing_fga, sam.ui, alex) is ViewerTier.connections


async def test_commit_failure_after_revoke_hook_over_denies_until_reconcile(
    session: Session, sessions, factory, fake_fga: FakeFga, monkeypatch: pytest.MonkeyPatch
) -> None:
    sam, alex, rid = await _approved_pair(session, sessions, fake_fga)
    real = authz.on_connection_revoked

    async def revoke_then_fail(**kwargs):
        await real(**kwargs)
        raise RuntimeError("synthetic failure after the revoke hook, before commit")

    monkeypatch.setattr(authz, "on_connection_revoked", revoke_then_fail)
    with pytest.raises(svc.ConnectionsUnavailable):
        await svc.revoke(alex.ui, rid, sessions=sessions)
    assert rows(session)[0].status == "approved"  # rolled back
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.none  # over-denied, never leaks

    await reconcile(fake_fga, factory)
    assert await tier_of(fake_fga, sam.ui, alex) is ViewerTier.trusted_circle


async def test_never_revoked_in_postgres_but_granted_in_openfga(
    session: Session, sessions, factory, fake_fga: FakeFga
) -> None:
    """After every successful revoke-class call, OpenFGA grants nothing Postgres does not."""
    _sam, alex, rid = await _approved_pair(session, sessions, fake_fga)
    await svc.change_tier(alex.ui, rid, tier="connections", sessions=sessions)
    await svc.block(alex.ui, rid, sessions=sessions)
    before = set(fake_fga.tuples)
    await reconcile(fake_fga, factory)
    assert fake_fga.tuples == before  # reconcile finds nothing to change


# --- Serialization ----------------------------------------------------------------------------


class GatedFga(FakeFga):
    """Holds every ``write`` until ``release`` is set (a slow post-commit grant)."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = False
        self.entered = anyio.Event()
        self.release = anyio.Event()

    async def write_tuples(self, tuples) -> None:
        if self.gate:
            self.entered.set()
            await self.release.wait()
        await super().write_tuples(tuples)


async def test_delayed_grant_cannot_land_after_a_later_revoke(
    session: Session, sessions, factory
) -> None:
    fga = GatedFga()
    authz.configure_authz(fga)
    try:
        sam, alex = add_person(session, "sam"), add_person(session, "alex")
        seed_fga(fga, sam, alex)
        req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
        rid = req.request.id
        fga.gate = True
        revoked = threading.Event()

        async def approve() -> None:
            await svc.approve(alex.ui, rid, tier="trusted_circle", sessions=sessions)

        async def revoke() -> None:
            await svc.revoke(alex.agent, rid, sessions=sessions)
            revoked.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(approve)
            with anyio.fail_after(5):
                await fga.entered.wait()  # Tx1 committed approved; Tx2's grant is in flight
            assert rows(session)[0].status == "approved"
            tg.start_soon(revoke)
            await anyio.sleep(0.5)
            assert not revoked.is_set()  # the revoke waits for the pair lock
            fga.release.set()

        assert rows(session)[0].status == "revoked"
        assert await tier_of(fga, sam.ui, alex) is ViewerTier.none
        alex_profile = f"profile:{alex.profile.id}"
        assert not any(
            t.user == f"user:{sam.user.id}" for t in fga.tuples if t.object == alex_profile
        )
    finally:
        authz.reset_authz()


@pytest.mark.parametrize("race", ["revoke", "block", "downgrade"])
async def test_grant_rechecks_the_row_before_writing(
    session: Session, sessions, fake_fga: FakeFga, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    """Tx2 re-reads the row under the pair lock: a revoke, block or downgrade that committed
    between Tx1 and Tx2 is never overwritten by the stale grant."""
    sam, alex = add_person(session, "sam"), add_person(session, "alex")
    seed_fga(fake_fga, sam, alex)
    req = await svc.lookup_and_request(sam.ui, handle="alex_demo", sessions=sessions)
    rid = req.request.id
    real_grant = svc._grant_sync
    raced: list[bool] = []

    async def other_change() -> None:
        if race == "revoke":
            await svc.revoke(alex.ui, rid, sessions=sessions)
        elif race == "block":
            await svc.block(alex.ui, rid, sessions=sessions)
        else:
            await svc.change_tier(alex.ui, rid, tier="connections", sessions=sessions)

    def change_first(factory, requester, target, tier):
        if not raced:
            raced.append(True)
            anyio.from_thread.run(other_change)
        return real_grant(factory, requester, target, tier)

    monkeypatch.setattr(svc, "_grant_sync", change_first)
    await svc.approve(alex.ui, rid, tier="trusted_circle", sessions=sessions)
    expected = {"revoke": ViewerTier.none, "block": ViewerTier.none}.get(
        race, ViewerTier.connections
    )
    assert await tier_of(fake_fga, sam.ui, alex) is expected
    assert tier_tuple(alex.profile.id, sam.user.id, ConnectionTier.TRUSTED_CIRCLE) not in (
        fake_fga.tuples
    )


async def test_lock_timeout_fails_closed(
    session: Session, sessions, factory, fake_fga: FakeFga, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held pair lock (or the reconcile barrier) past LOCK_TIMEOUT_MS: retryable, no change."""
    _sam, alex, rid = await _approved_pair(session, sessions, fake_fga)
    monkeypatch.setattr(svc, "LOCK_TIMEOUT_MS", 200)
    with factory() as holder, holder.begin():
        holder.execute(text("LOCK TABLE connection_requests IN EXCLUSIVE MODE"))
        with pytest.raises(svc.ConnectionsUnavailable):
            await svc.revoke(alex.ui, rid, sessions=sessions)
        with pytest.raises(svc.ConnectionsUnavailable):
            await svc.lookup_and_request(alex.ui, handle="sam_demo", sessions=sessions)
    assert rows(session)[0].status == "approved"
    count = session.execute(select(func.count()).select_from(ConnectionRequest)).scalar_one()
    assert count == 1
