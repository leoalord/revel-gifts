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
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

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
