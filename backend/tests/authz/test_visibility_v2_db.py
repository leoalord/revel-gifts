# ruff: noqa: F401, F811
# (pytest fixtures engine/factory/session/_reset are imported from test_visibility_db and
# requested by parameter name.)
"""Model v2 reads by others, policy changes and v2 reconcile against real PostgreSQL.

Same requirements as ``test_visibility_db.py`` (``REVEL_TEST_DATABASE_URL``, a disposable
database; ``openfga`` tests also need ``OPENFGA_TEST_API_URL``), whose fixtures and builders
this module reuses. Run DB suites serially.
"""

import dataclasses
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import anyio
import anyio.from_thread
import pytest
from authz_fakes import FakeFga, Principal
from mcp.server.auth.provider import AccessToken
from sqlalchemy import text, update
from sqlalchemy.orm import Session, sessionmaker
from test_visibility_db import (
    DATABASE_URL,
    OPENFGA_TEST_API_URL,
    _old,
    _openfga_reachable,
    _reconcile,
    _reset,
    add_agent,
    add_claim,
    add_delegation,
    add_policy,
    add_profile,
    add_user,
    engine,
    factory,
    mcp,
    session,
    statements,
    ui,
)

from revel.auth import authz, fga_bootstrap, fga_reconcile
from revel.auth.agent_disconnect import disconnect_agent
from revel.auth.authz import (
    AuthorizationDenied,
    ViewerTier,
    blocked_tuple,
    configure_authz,
    delegation_tuples,
    owner_tuple,
    public_tuples,
    question_tuples,
    tier_tuple,
)
from revel.auth.fga import create_fga_client
from revel.auth.hooks import ModuleAuthzHooks
from revel.auth.identity import DelegationDisconnected, IdentityResolver
from revel.auth.repository import SqlAlchemyIdentityRepository
from revel.auth.visibility import SharedClaim, VisibleClaim, visible_claims
from revel.db.enums import Audience, ClaimStatus, ConnectionRequestStatus, ConnectionTier
from revel.db.models import AgentDelegation, Claim, ConnectionRequest, Profile, Question, User
from revel.settings import Settings

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not DATABASE_URL, reason="REVEL_TEST_DATABASE_URL not set"),
]

SHARED_FIELDS = {
    "id",
    "category",
    "statement",
    "details",
    "audience",
    "evidence_type",
    "confidence",
    "observed_at",
    "observed_precision",
    "provisional",
}


# --- Builders ---------------------------------------------------------------------------------


def add_request(
    session: Session,
    requester: User,
    target: User,
    status: ConnectionRequestStatus,
    tier: ConnectionTier | None = None,
) -> ConnectionRequest:
    row = ConnectionRequest(
        requester_user_id=requester.id,
        target_user_id=target.id,
        status=status.value,
        granted_tier=tier.value if tier else None,
        responded_at=None if status is ConnectionRequestStatus.PENDING else datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def add_question(session: Session, profile: Profile, requester: User) -> Question:
    question = Question(
        profile_id=profile.id,
        requester_user_id=requester.id,
        requested_information="synthetic: favourite tea?",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(question)
    session.flush()
    return question


def backdate(session: Session, model: type, ids: list, *, column: str = "updated_at") -> None:
    """Move rows out of the in-flight window (raw UPDATE: the ORM would refresh updated_at)."""
    table = model.__table__.name
    session.execute(
        text(f"UPDATE {table} SET {column} = now() - interval '1 hour' WHERE id = ANY(:ids)"),
        {"ids": ids},
    )


@dataclasses.dataclass
class People:
    alex: User
    tia: User
    cal: User
    sam: User
    profile: Profile
    tia_profile: Profile
    delegations: dict[str, AgentDelegation]


@pytest.fixture
def people(session: Session) -> People:
    """Alex owns the profile; Tia, Cal and Sam each have a profile and a connected agent."""
    agent = add_agent(session)
    users = {name: add_user(session, name) for name in ("alex", "tia", "cal", "sam")}
    delegations = {name: add_delegation(session, u, agent) for name, u in users.items()}
    profiles = {name: add_profile(session, u) for name, u in users.items()}
    session.commit()
    return People(
        alex=users["alex"],
        tia=users["tia"],
        cal=users["cal"],
        sam=users["sam"],
        profile=profiles["alex"],
        tia_profile=profiles["tia"],
        delegations=delegations,
    )


def seed_connected(fga: FakeFga, people: People) -> None:
    for d in people.delegations.values():
        fga.tuples |= set(delegation_tuples(d.user_id, d.id))
    fga.tuples.add(owner_tuple(people.alex.id, people.profile.id))


def seed_claims(session: Session, people: People) -> None:
    for audience in Audience:
        add_claim(session, people.profile, audience.value, audience=audience)
    add_claim(
        session,
        people.profile,
        "maybe-public",
        status=ClaimStatus.PROVISIONAL,
        audience=Audience.PUBLIC,
        delegation=people.delegations["alex"],
    )
    add_claim(session, people.profile, "draft", status=ClaimStatus.PENDING_REVIEW)


# --- Reads by others --------------------------------------------------------------------------


async def test_other_people_get_permitted_audiences_as_projections(
    session: Session, fake_fga: FakeFga, people: People
) -> None:
    seed_connected(fake_fga, people)
    seed_claims(session, people)
    add_policy(session, people.profile, 1, share=True, public=True)
    session.commit()
    configure_authz(fake_fga)
    pid = people.profile.id
    await authz.on_connection_granted(
        profile_id=pid, viewer_user_id=people.tia.id, tier="trusted_circle"
    )
    await authz.on_connection_granted(
        profile_id=pid, viewer_user_id=people.cal.id, tier="connections"
    )
    await authz.on_public_opt_in_changed(profile_id=pid, enabled=True)

    expected = {
        "tia": (ViewerTier.trusted_circle, {"public", "connections", "trusted_circle"}),
        "cal": (ViewerTier.connections, {"public", "connections"}),
        "sam": (ViewerTier.public, {"public"}),
    }
    for name, (tier, audiences) in expected.items():
        user = getattr(people, name)
        for principal in (ui(user), mcp(people.delegations[name])):
            result = await visible_claims(session, principal, pid)
            assert result.tier is tier
            # Only Me and non-live facts never reach another person.
            assert statements(result) == audiences | {"maybe-public"}
            assert all(type(c) is SharedClaim for c in result.claims)
            caveat = {c.statement: c.provisional for c in result.claims}
            assert caveat["maybe-public"] is True and caveat["public"] is False

    shared = next(c for c in (await visible_claims(session, ui(people.cal), pid)).claims)
    assert {f.name for f in dataclasses.fields(shared)} == SHARED_FIELDS
    for private in ("contributor_user_id", "contributor_delegation_id", "contributed_via"):
        assert not hasattr(shared, private)
    with pytest.raises(dataclasses.FrozenInstanceError):
        shared.statement = "changed"  # type: ignore[misc]

    # The owner path is unchanged: ORM claims, Only Me included.
    owner = await visible_claims(session, ui(people.alex), pid)
    assert owner.tier is ViewerTier.only_me
    assert all(type(c) is VisibleClaim and isinstance(c.claim, Claim) for c in owner.claims)
    assert "only_me" in statements(owner)


async def test_policy_tightening_and_public_withdrawal_apply_on_next_read(
    session: Session, fake_fga: FakeFga, people: People
) -> None:
    seed_connected(fake_fga, people)
    seed_claims(session, people)
    add_policy(session, people.profile, 1, share=True, public=True)
    session.commit()
    configure_authz(fake_fga)
    pid = people.profile.id
    await authz.on_connection_granted(
        profile_id=pid, viewer_user_id=people.cal.id, tier="connections"
    )
    await authz.on_public_opt_in_changed(profile_id=pid, enabled=True)
    sam, cal = mcp(people.delegations["sam"]), mcp(people.delegations["cal"])
    assert statements(await visible_claims(session, sam, pid)) == {"public", "maybe-public"}
    assert (await visible_claims(session, sam, pid)).has_provisional

    # Stop sharing provisional facts: gone for others (no caveat needed), kept for the owner.
    add_policy(session, people.profile, 2, share=False, public=True)
    session.commit()
    assert statements(await visible_claims(session, sam, pid)) == {"public"}
    assert statements(await visible_claims(session, cal, pid)) == {"public", "connections"}
    assert not (await visible_claims(session, cal, pid)).has_provisional
    assert "maybe-public" in statements(await visible_claims(session, ui(people.alex), pid))

    # Withdraw Public in the policy: Public-only viewers see nothing even before the tuples go
    # (defence in depth); Connections still see Public-audience facts.
    add_policy(session, people.profile, 3, share=False, public=False)
    session.commit()
    withdrawn = await visible_claims(session, sam, pid)
    assert withdrawn.tier is ViewerTier.public and withdrawn.claims == ()
    assert statements(await visible_claims(session, cal, pid)) == {"public", "connections"}
    await authz.on_public_opt_in_changed(profile_id=pid, enabled=False)
    with pytest.raises(AuthorizationDenied):
        await visible_claims(session, sam, pid)


async def test_block_and_revocation_deny_reads(
    session: Session, fake_fga: FakeFga, people: People
) -> None:
    seed_connected(fake_fga, people)
    seed_claims(session, people)
    add_policy(session, people.profile, 1, public=True)
    session.commit()
    configure_authz(fake_fga)
    pid = people.profile.id
    for name in ("tia", "cal"):
        await authz.on_connection_granted(
            profile_id=pid, viewer_user_id=getattr(people, name).id, tier="trusted_circle"
        )
    await authz.on_public_opt_in_changed(profile_id=pid, enabled=True)

    await authz.on_blocked(profile_id=pid, blocked_user_id=people.tia.id)
    await authz.on_connection_revoked(profile_id=pid, viewer_user_id=people.cal.id)
    for principal in (ui(people.tia), mcp(people.delegations["tia"])):
        with pytest.raises(AuthorizationDenied):
            await visible_claims(session, principal, pid)
    # Revoked Cal falls back to Public.
    cal = await visible_claims(session, mcp(people.delegations["cal"]), pid)
    assert cal.tier is ViewerTier.public


# --- Reconcile v2 -----------------------------------------------------------------------------


async def test_reconcile_v2_from_requests_policies_and_questions(
    factory: sessionmaker[Session], session: Session, fake_fga: FakeFga, people: People
) -> None:
    dan = add_user(session, "dan")
    a, t, c, s = people.alex, people.tia, people.cal, people.sam
    pid = people.profile.id
    add_request(session, t, a, ConnectionRequestStatus.APPROVED, ConnectionTier.TRUSTED_CIRCLE)
    add_request(session, c, a, ConnectionRequestStatus.APPROVED, ConnectionTier.CONNECTIONS)
    add_request(session, s, a, ConnectionRequestStatus.BLOCKED)
    add_request(session, dan, a, ConnectionRequestStatus.REVOKED)
    add_request(session, a, t, ConnectionRequestStatus.PENDING)  # reverse direction
    # Tia had declined Cal earlier, then approved a new request (one live row per pair).
    add_request(session, c, t, ConnectionRequestStatus.DECLINED)
    add_request(session, c, t, ConnectionRequestStatus.APPROVED, ConnectionTier.CONNECTIONS)
    add_policy(session, people.profile, 1, public=True)
    add_policy(session, people.tia_profile, 1, public=True)
    add_policy(session, people.tia_profile, 2, public=False)  # withdrawn at the latest version
    question = add_question(session, people.profile, t)
    session.commit()
    # Stale state reconcile must repair: Sam's old tier, Dan's revoked tier, Tia's public.
    _old(
        fake_fga,
        tier_tuple(pid, s.id, ConnectionTier.TRUSTED_CIRCLE),
        tier_tuple(pid, dan.id, ConnectionTier.CONNECTIONS),
        tier_tuple(pid, t.id, ConnectionTier.CONNECTIONS),  # tier changed to trusted
        *public_tuples(people.tia_profile.id),
    )

    summary = await _reconcile(fake_fga, factory, prune=False)
    v2 = {t_ for t_ in fake_fga.tuples if t_.relation not in ("owner", "acts_for", "delegate")}
    v2 = {t_ for t_ in v2 if t_.relation != "connected_agent"}
    assert v2 == {
        tier_tuple(pid, t.id, ConnectionTier.TRUSTED_CIRCLE),
        tier_tuple(pid, c.id, ConnectionTier.CONNECTIONS),
        tier_tuple(people.tia_profile.id, c.id, ConnectionTier.CONNECTIONS),
        blocked_tuple(pid, s.id),
        *public_tuples(pid),
        *question_tuples(question.id, pid, t.id),
    }
    assert summary.blocked_pairs == 1 and summary.connection_tuples == 3
    assert summary.public_profiles == 1 and summary.question_tuples == 2
    assert await _reconcile(fake_fga, factory, prune=False) == summary

    # Reads follow: Sam blocked (even with public), Tia trusted via her agent.
    configure_authz(fake_fga)
    with pytest.raises(AuthorizationDenied):
        await visible_claims(session, mcp(people.delegations["sam"]), pid)
    tia = await visible_claims(session, mcp(people.delegations["tia"]), pid)
    assert tia.tier is ViewerTier.trusted_circle


async def test_reconcile_window_skips_recent_grants_but_always_revokes(
    factory: sessionmaker[Session], session: Session, fake_fga: FakeFga, people: People
) -> None:
    a, t, c, s = people.alex, people.tia, people.cal, people.sam
    pid = people.profile.id
    old_request = add_request(
        session, t, a, ConnectionRequestStatus.APPROVED, ConnectionTier.TRUSTED_CIRCLE
    )
    add_request(session, c, a, ConnectionRequestStatus.APPROVED, ConnectionTier.CONNECTIONS)
    add_request(session, s, a, ConnectionRequestStatus.REVOKED)
    add_policy(session, people.profile, 1, public=True)  # recent
    gone = people.delegations["cal"]
    session.execute(
        update(AgentDelegation)
        .where(AgentDelegation.id == gone.id)
        .values(status="disconnected", disconnected_at=datetime.now(UTC))
    )
    backdate(session, ConnectionRequest, [old_request.id])
    backdate(session, AgentDelegation, [people.delegations["tia"].id])
    session.commit()
    _old(
        fake_fga,
        *delegation_tuples(gone.user_id, gone.id),
        tier_tuple(pid, s.id, ConnectionTier.CONNECTIONS),
    )

    summary = await fga_reconcile.run(
        prune=False, fga=fake_fga, sessionmaker_provider=lambda: factory
    )  # default 2-minute window
    assert tier_tuple(pid, t.id, ConnectionTier.TRUSTED_CIRCLE) in fake_fga.tuples  # settled
    assert set(delegation_tuples(t.id, people.delegations["tia"].id)) <= fake_fga.tuples
    # Recent rows: their hooks just ran, so reconcile does not re-grant them...
    assert tier_tuple(pid, c.id, ConnectionTier.CONNECTIONS) not in fake_fga.tuples
    assert not set(public_tuples(pid)) & fake_fga.tuples
    assert not set(delegation_tuples(a.id, people.delegations["alex"].id)) & fake_fga.tuples
    # ...but revocations always apply, however recent.
    assert not set(delegation_tuples(gone.user_id, gone.id)) & fake_fga.tuples
    assert tier_tuple(pid, s.id, ConnectionTier.CONNECTIONS) not in fake_fga.tuples
    assert summary.skipped_in_flight == 4  # alex, sam delegations; cal request; alex policy


async def test_barrier_reapplies_changes_committed_after_the_snapshot(
    factory: sessionmaker[Session],
    session: Session,
    fake_fga: FakeFga,
    people: People,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, t, s = people.alex, people.tia, people.sam
    pid = people.profile.id
    approved = add_request(
        session, t, a, ConnectionRequestStatus.APPROVED, ConnectionTier.TRUSTED_CIRCLE
    )
    add_request(session, s, a, ConnectionRequestStatus.PENDING)
    add_policy(session, people.profile, 1, public=True)
    session.commit()
    real_snapshot = fga_reconcile._snapshot

    def snapshot_then_change(provider: fga_reconcile.SessionFactory) -> fga_reconcile.Snapshot:
        snap = real_snapshot(provider)
        # After the snapshot: Alex revokes Tia, blocks Sam and withdraws Public (committed).
        with factory() as other, other.begin():
            row = other.get(ConnectionRequest, approved.id)
            assert row is not None
            row.status, row.granted_tier = ConnectionRequestStatus.REVOKED.value, None
            pending = other.query(ConnectionRequest).filter_by(requester_user_id=s.id).one()
            pending.status, pending.responded_at = (
                ConnectionRequestStatus.BLOCKED.value,
                datetime.now(UTC),
            )
            add_policy(other, other.get(Profile, pid), 2, public=False)  # type: ignore[arg-type]
        return snap

    monkeypatch.setattr(fga_reconcile, "_snapshot", snapshot_then_change)
    summary = await _reconcile(fake_fga, factory, prune=False)
    assert summary.rechecked_changes == 3
    assert tier_tuple(pid, t.id, ConnectionTier.TRUSTED_CIRCLE) not in fake_fga.tuples
    assert blocked_tuple(pid, s.id) in fake_fga.tuples
    assert not set(public_tuples(pid)) & fake_fga.tuples


async def test_barrier_waits_for_an_in_flight_revocation(
    factory: sessionmaker[Session],
    session: Session,
    fake_fga: FakeFga,
    people: People,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The F2/F6 race: a revoke whose hook ran but whose commit lands after reconcile's grant."""
    a, t = people.alex, people.tia
    pid = people.profile.id
    approved = add_request(
        session, t, a, ConnectionRequestStatus.APPROVED, ConnectionTier.TRUSTED_CIRCLE
    )
    session.commit()
    real_snapshot = fga_reconcile._snapshot
    started, done = threading.Event(), threading.Event()

    def revoke_slowly() -> None:
        with factory() as other, other.begin():
            other.execute(
                update(ConnectionRequest)
                .where(ConnectionRequest.id == approved.id)
                .values(status="revoked", granted_tier=None)
            )
            # F6's revoke hook has deleted the tuple; the commit is still in flight.
            fake_fga.tuples.discard(tier_tuple(pid, t.id, ConnectionTier.TRUSTED_CIRCLE))
            started.set()
            threading.Event().wait(0.5)
        done.set()

    def snapshot_then_revoke(provider: fga_reconcile.SessionFactory) -> fga_reconcile.Snapshot:
        snap = real_snapshot(provider)
        threading.Thread(target=revoke_slowly, daemon=True).start()
        assert started.wait(5)
        return snap

    monkeypatch.setattr(fga_reconcile, "_snapshot", snapshot_then_revoke)
    summary = await _reconcile(fake_fga, factory, prune=False)
    assert done.is_set()  # the barrier waited for the in-flight commit
    assert summary.connection_tuples == 1  # granted from the stale snapshot...
    assert tier_tuple(pid, t.id, ConnectionTier.TRUSTED_CIRCLE) not in fake_fga.tuples  # ...undone


async def test_prune_covers_v2_relations(
    factory: sessionmaker[Session], session: Session, fake_fga: FakeFga, people: People
) -> None:
    a, t = people.alex, people.tia
    pid = people.profile.id
    add_request(session, t, a, ConnectionRequestStatus.APPROVED, ConnectionTier.CONNECTIONS)
    question = add_question(session, people.profile, t)
    session.commit()
    ghost_profile, ghost_user, ghost_question = uuid4(), uuid4(), uuid4()
    orphans = {
        tier_tuple(ghost_profile, t.id, ConnectionTier.TRUSTED_CIRCLE),
        blocked_tuple(ghost_profile, ghost_user),
        *public_tuples(ghost_profile),
        *question_tuples(ghost_question, pid, t.id),
    }
    _old(fake_fga, *orphans)
    young = tier_tuple(ghost_profile, ghost_user, ConnectionTier.CONNECTIONS)
    await fake_fga.write_tuples([young])

    summary = await _reconcile(fake_fga, factory, prune=True)
    assert not orphans & fake_fga.tuples
    assert young in fake_fga.tuples  # may be an uncommitted hook write
    assert tier_tuple(pid, t.id, ConnectionTier.CONNECTIONS) in fake_fga.tuples
    assert set(question_tuples(question.id, pid, t.id)) <= fake_fga.tuples
    assert summary.pruned_tuples == len(orphans)


# --- Barrier bounds and failure ---------------------------------------------------------------

WAITING_SHARE_LOCK = text(
    "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
    "WHERE c.relname = 'connection_requests' AND l.mode = 'ShareLock' AND NOT l.granted"
)


def hold_row_lock(
    factory: sessionmaker[Session], request_id: object, seconds: float, held: threading.Event
) -> threading.Thread:
    """An in-flight writer: an uncommitted UPDATE on connection_requests for ``seconds``."""

    def run() -> None:
        with factory() as other, other.begin():
            other.execute(
                text("UPDATE connection_requests SET updated_at = now() WHERE id = :id"),
                {"id": request_id},
            )
            held.set()
            threading.Event().wait(seconds)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert held.wait(5)
    return thread


def fast_barrier(monkeypatch: pytest.MonkeyPatch, *, lock: float, attempts: int) -> None:
    monkeypatch.setattr(fga_reconcile, "BARRIER_LOCK_TIMEOUT_SECONDS", lock)
    monkeypatch.setattr(fga_reconcile, "BARRIER_ATTEMPTS", attempts)
    monkeypatch.setattr(fga_reconcile, "_retry_delay", lambda: 0.2)


async def test_writer_queued_behind_a_waiting_barrier_proceeds_within_the_bound(
    factory: sessionmaker[Session],
    session: Session,
    fake_fga: FakeFga,
    people: People,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIFO lock queue: a writer arriving while the barrier waits is held at most one lock
    attempt (lock_timeout), not until the in-flight writer finishes; reconcile still succeeds
    by retrying."""
    a, t, c = people.alex, people.tia, people.cal
    busy = add_request(session, t, a, ConnectionRequestStatus.APPROVED, ConnectionTier.CONNECTIONS)
    other_row = add_request(session, c, a, ConnectionRequestStatus.PENDING)
    session.commit()
    lock_seconds = 1.0
    fast_barrier(monkeypatch, lock=lock_seconds, attempts=6)
    holder = hold_row_lock(factory, busy.id, 2.5, threading.Event())
    queued: dict[str, float] = {}

    def queued_writer() -> None:
        with factory() as probe:
            deadline = time.monotonic() + 5
            while probe.execute(WAITING_SHARE_LOCK).scalar_one() == 0:
                probe.rollback()
                assert time.monotonic() < deadline, "barrier never queued"
                threading.Event().wait(0.02)
            probe.rollback()
        started = time.monotonic()
        with factory() as writer, writer.begin():
            writer.execute(
                text("UPDATE connection_requests SET updated_at = now() WHERE id = :id"),
                {"id": other_row.id},
            )
        queued["elapsed"] = time.monotonic() - started

    writer = threading.Thread(target=queued_writer, daemon=True)
    writer.start()
    summary = await _reconcile(fake_fga, factory, prune=False)
    writer.join(10)
    holder.join(10)
    assert queued["elapsed"] < lock_seconds + 0.75, queued
    assert summary.connection_tuples == 1


async def test_barrier_failure_is_raised_and_skips_prune(
    factory: sessionmaker[Session],
    session: Session,
    fake_fga: FakeFga,
    people: People,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, t = people.alex, people.tia
    busy = add_request(session, t, a, ConnectionRequestStatus.APPROVED, ConnectionTier.CONNECTIONS)
    session.commit()
    orphan = tier_tuple(uuid4(), uuid4(), ConnectionTier.CONNECTIONS)
    _old(fake_fga, orphan)
    fast_barrier(monkeypatch, lock=0.3, attempts=2)
    holder = hold_row_lock(factory, busy.id, 3.0, threading.Event())
    started = time.monotonic()
    with pytest.raises(fga_reconcile.BarrierNotApplied, match="re-run reconcile"):
        await _reconcile(fake_fga, factory, prune=True)
    assert time.monotonic() - started < 2.5
    assert orphan in fake_fga.tuples  # --prune did not run after the failed barrier
    holder.join(10)


async def test_barrier_apply_is_bounded_and_releases_the_locks(
    factory: sessionmaker[Session],
    session: Session,
    fake_fga: FakeFga,
    people: People,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenFGA hanging while the locks are held: the apply bound rolls back (locks released at
    once), retries, then fails loudly."""
    a, t = people.alex, people.tia
    approved = add_request(
        session, t, a, ConnectionRequestStatus.APPROVED, ConnectionTier.CONNECTIONS
    )
    session.commit()
    fast_barrier(monkeypatch, lock=1.0, attempts=2)
    monkeypatch.setattr(fga_reconcile, "BARRIER_APPLY_SECONDS", 0.3)
    real_snapshot, real_barrier = fga_reconcile._snapshot, fga_reconcile._barrier

    def snapshot_then_revoke(provider: fga_reconcile.SessionFactory) -> fga_reconcile.Snapshot:
        snap = real_snapshot(provider)
        with factory() as other, other.begin():
            other.execute(
                update(ConnectionRequest)
                .where(ConnectionRequest.id == approved.id)
                .values(status="revoked", granted_tier=None)
            )
        return snap

    def slow_barrier(factory_: fga_reconcile.SessionFactory, apply):  # type: ignore[no-untyped-def]

        def hanging_apply(fresh: fga_reconcile.Snapshot) -> None:
            anyio.from_thread.run(_hang_fga, fake_fga)
            apply(fresh)

        return real_barrier(factory_, hanging_apply)

    monkeypatch.setattr(fga_reconcile, "_snapshot", snapshot_then_revoke)
    monkeypatch.setattr(fga_reconcile, "_barrier", slow_barrier)
    started = time.monotonic()
    with pytest.raises(fga_reconcile.BarrierNotApplied):
        await _reconcile(fake_fga, factory, prune=False)
    assert time.monotonic() - started < 3
    # The locks were released: a writer goes through immediately.
    began = time.monotonic()
    with factory() as writer, writer.begin():
        writer.execute(text("SET LOCAL lock_timeout = '500ms'"))
        writer.execute(text("UPDATE connection_requests SET updated_at = now()"))
    assert time.monotonic() - began < 0.5


async def _hang_fga(fga: FakeFga) -> None:
    """Make every later OpenFGA write/delete of this fake hang (until cancelled)."""

    async def hang(*_: object, **__: object) -> None:
        await anyio.sleep(30)

    fga.write_tuples = hang  # type: ignore[method-assign]
    fga.delete_tuples = hang  # type: ignore[method-assign]


def test_cli_exits_non_zero_when_the_barrier_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing_run(**_: object) -> fga_reconcile.ReconcileSummary:
        raise fga_reconcile.BarrierNotApplied(
            "barrier not applied after 4 attempts (OperationalError); re-run reconcile"
        )

    monkeypatch.setattr(fga_reconcile, "run", failing_run)
    assert fga_reconcile.main([]) == fga_reconcile.EXIT_BARRIER_NOT_APPLIED == 3
    err = capsys.readouterr().err
    assert "barrier not applied" in err and "re-run reconcile" in err


async def test_reconcile_removes_a_grant_left_by_a_rolled_back_transaction(
    factory: sessionmaker[Session], session: Session, fake_fga: FakeFga, people: People
) -> None:
    """Backstop for a caller that broke the post-commit rule: the grant hook ran, then the
    approval rolled back. The pair's committed state (declined) wins on the next reconcile."""
    a, t = people.alex, people.tia
    add_request(session, t, a, ConnectionRequestStatus.DECLINED)
    session.commit()
    configure_authz(fake_fga)
    with factory() as tx, tx.begin():
        tx.execute(
            update(ConnectionRequest)
            .where(ConnectionRequest.requester_user_id == t.id)
            .values(status="approved", granted_tier="trusted_circle")
        )
        await authz.on_connection_granted(
            profile_id=people.profile.id, viewer_user_id=t.id, tier="trusted_circle"
        )
        tx.rollback()
    leaked = tier_tuple(people.profile.id, t.id, ConnectionTier.TRUSTED_CIRCLE)
    assert leaked in fake_fga.tuples
    await _reconcile(fake_fga, factory, prune=False)
    assert leaked not in fake_fga.tuples


# --- Real OpenFGA end to end ------------------------------------------------------------------


@pytest.fixture
def real_fga_settings() -> Iterator[Settings]:
    if not _openfga_reachable():
        pytest.skip("OPENFGA_TEST_API_URL is not set or not reachable")
    import asyncio

    base = Settings(_env_file=None, openfga_api_url=OPENFGA_TEST_API_URL)
    boot = asyncio.run(fga_bootstrap.bootstrap(base, store_name=f"revel-test-{uuid4()}"))
    settings = base.model_copy(
        update={
            "openfga_store_id": boot.store_id,
            "openfga_authorization_model_id": boot.authorization_model_id,
        }
    )
    yield settings

    async def delete_store() -> None:
        client = create_fga_client(settings)
        try:
            await client.sdk.delete_store()
        finally:
            await client.close()

    asyncio.run(delete_store())


@pytest.mark.openfga
async def test_reconciled_shared_reads_and_sql_disconnect_on_real_openfga(
    factory: sessionmaker[Session], session: Session, people: People, real_fga_settings: Settings
) -> None:
    fga = create_fga_client(real_fga_settings)
    configure_authz(fga)
    try:
        a, t, s = people.alex, people.tia, people.sam
        pid = people.profile.id
        seed_claims(session, people)
        add_request(session, t, a, ConnectionRequestStatus.APPROVED, ConnectionTier.TRUSTED_CIRCLE)
        add_request(session, s, a, ConnectionRequestStatus.BLOCKED)
        add_policy(session, people.profile, 1, public=True)
        question = add_question(session, people.profile, t)
        session.commit()
        await fga_reconcile.run(
            prune=True, fga=fga, sessionmaker_provider=lambda: factory, window=timedelta(0)
        )

        tia_agent = mcp(people.delegations["tia"])
        trusted = await visible_claims(session, tia_agent, pid)
        assert trusted.tier is ViewerTier.trusted_circle
        assert statements(trusted) == {"public", "connections", "trusted_circle", "maybe-public"}
        public = await visible_claims(session, mcp(people.delegations["cal"]), pid)
        assert statements(public) == {"public", "maybe-public"}
        with pytest.raises(AuthorizationDenied):
            await visible_claims(session, mcp(people.delegations["sam"]), pid)

        # Disconnect Tia's agent through F2's SQL repository and resolver.
        resolver = IdentityResolver(
            SqlAlchemyIdentityRepository(lambda: factory), hooks=ModuleAuthzHooks()
        )
        await disconnect_agent(tia_agent, people.delegations["tia"].id, resolver=resolver)
        agent = f"agent:{people.delegations['tia'].id}"
        relations = [
            (r, f"profile:{pid}")
            for r in ("view_trusted", "view_connections", "view_public", "contribute")
        ]
        relations += [
            ("read_answer", f"question:{question.id}"),
            ("respond", f"question:{question.id}"),
        ]
        relations += [("contribute", f"profile:{people.tia_profile.id}")]
        relations += [("view_only_me", f"profile:{people.tia_profile.id}")]
        for relation, obj in relations:
            assert not await fga.check(user=agent, relation=relation, object=obj)
        with pytest.raises(AuthorizationDenied):
            await visible_claims(session, tia_agent, pid)
        # The still-valid old token: F2's upsert rejects the disconnected row; no tuples return,
        # including after another reconcile.
        old_token = AccessToken(
            token="opaque",
            client_id="client_muse",
            scopes=[],
            subject=t.workos_user_id,
            claims={"iss": "x"},
        )
        with pytest.raises(DelegationDisconnected):
            await resolver.resolve(old_token, "mcp")
        await fga_reconcile.run(
            prune=True, fga=fga, sessionmaker_provider=lambda: factory, window=timedelta(0)
        )
        stored = [x.tuple for x in await fga.read_stored_tuples()]
        assert [x for x in stored if agent in (x.user, x.object)] == []
        # Tia herself (UI) keeps her trusted tier.
        tia_ui = Principal(user_id=t.id, via="ui")
        assert (await visible_claims(session, tia_ui, pid)).tier is ViewerTier.trusted_circle
    finally:
        await fga.close()
