"""visible_claims / owner_claims / on_profile_created / reconcile against real PostgreSQL.

Needs ``REVEL_TEST_DATABASE_URL`` (a disposable database: it is reset to Alembic base and
upgraded to head once per module, and tables are truncated per test). Run DB suites serially.
The ``openfga`` tests here additionally need ``OPENFGA_TEST_API_URL``.

Fixtures keep ``contributor_user_id`` equal to the contributor delegation's own user (required
by F3's upcoming composite FKs); most facts are owner UI edits with no delegation.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from authz_fakes import FakeFga, Principal
from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.orm import Session, sessionmaker

from revel.auth import fga_bootstrap, fga_reconcile
from revel.auth.authz import (
    AuthorizationDenied,
    ViewerTier,
    acts_for_tuple,
    configure_authz,
    delegate_tuple,
    owner_tuple,
    reset_authz,
)
from revel.auth.fga import FgaConfigurationError, RelationTuple, create_fga_client
from revel.auth.visibility import (
    _visible_claims_for_tier,
    on_profile_created,
    owner_claims,
    visible_claims,
)
from revel.db.enums import Audience, ClaimStatus, DelegationStatus
from revel.db.models import Agent, AgentDelegation, Claim, ContributionPolicy, Profile, User
from revel.settings import Settings

DATABASE_URL = os.environ.get("REVEL_TEST_DATABASE_URL")
OPENFGA_TEST_API_URL = os.environ.get("OPENFGA_TEST_API_URL", "")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not DATABASE_URL, reason="REVEL_TEST_DATABASE_URL not set"),
]


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    from alembic import command
    from alembic.config import Config

    assert DATABASE_URL
    migrate = create_engine(DATABASE_URL, poolclass=pool.NullPool)
    for action, target in ((command.downgrade, "base"), (command.upgrade, "head")):
        config = Config(str(ALEMBIC_INI))
        with migrate.begin() as connection:
            config.attributes["connection"] = connection
            action(config, target)
    migrate.dispose()
    pooled = create_engine(DATABASE_URL, pool_size=5)
    try:
        yield pooled
    finally:
        pooled.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE claim_events, claims, contribution_policies, profiles, "
                "agent_delegations, agents, users CASCADE"
            )
        )
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as s:
        yield s


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_authz()
    yield
    reset_authz()


# --- Fixture builders -------------------------------------------------------------------------


def add_user(session: Session, name: str) -> User:
    user = User(workos_user_id=f"user_{name}_{uuid4().hex[:6]}")
    session.add(user)
    session.flush()
    return user


def add_agent(session: Session, client_id: str = "client_muse") -> Agent:
    agent = session.query(Agent).filter_by(oauth_client_id=client_id).one_or_none()
    if agent is None:
        agent = Agent(oauth_client_id=client_id)
        session.add(agent)
        session.flush()
    return agent


def add_delegation(
    session: Session, user: User, agent: Agent, *, active: bool = True
) -> AgentDelegation:
    delegation = AgentDelegation(
        user_id=user.id,
        agent_id=agent.id,
        status=(DelegationStatus.ACTIVE if active else DelegationStatus.DISCONNECTED).value,
        disconnected_at=None if active else datetime.now(UTC),
    )
    session.add(delegation)
    session.flush()
    return delegation


def add_profile(session: Session, user: User) -> Profile:
    profile = Profile(user_id=user.id)
    session.add(profile)
    session.flush()
    return profile


def add_policy(session: Session, profile: Profile, version: int, *, share: bool) -> None:
    session.add(
        ContributionPolicy(
            profile_id=profile.id,
            version=version,
            approved_by_user_id=profile.user_id,
            share_provisional=share,
        )
    )
    session.flush()


def add_claim(
    session: Session,
    profile: Profile,
    statement: str,
    *,
    status: ClaimStatus = ClaimStatus.ACTIVE,
    audience: Audience = Audience.ONLY_ME,
    delegation: AgentDelegation | None = None,
) -> Claim:
    claim = Claim(
        profile_id=profile.id,
        category="likes",
        statement=statement,
        audience=audience.value,
        evidence_type="explicit",
        status=status.value,
        # The contributor is the delegation's own user (survives F3's composite FKs).
        contributor_user_id=delegation.user_id if delegation else profile.user_id,
        contributor_delegation_id=delegation.id if delegation else None,
        contributed_via="mcp" if delegation else "ui",
    )
    session.add(claim)
    session.flush()
    return claim


def ui(user: User) -> Principal:
    return Principal(user_id=user.id, via="ui")


def mcp(delegation: AgentDelegation) -> Principal:
    return Principal(
        user_id=delegation.user_id,
        agent_id=delegation.agent_id,
        delegation_id=delegation.id,
        via="mcp",
    )


def statements(result: object) -> set[str]:
    claims = getattr(result, "claims", result)
    return {getattr(c, "claim", c).statement for c in claims}


@dataclass
class World:
    alex: User
    sam: User
    alex_d: AgentDelegation
    sam_d: AgentDelegation
    profile: Profile
    sam_profile: Profile


@pytest.fixture
def alex_world(session: Session, fake_fga: FakeFga) -> World:
    """Alex (profile, active delegation on the shared client) and Sam (same client)."""
    agent = add_agent(session)
    alex, sam = add_user(session, "alex"), add_user(session, "sam")
    alex_d, sam_d = add_delegation(session, alex, agent), add_delegation(session, sam, agent)
    profile, sam_profile = add_profile(session, alex), add_profile(session, sam)
    session.commit()
    fake_fga.tuples |= {
        owner_tuple(alex.id, profile.id),
        acts_for_tuple(alex.id, alex_d.id),
        delegate_tuple(alex.id, alex_d.id),
        owner_tuple(sam.id, sam_profile.id),
        acts_for_tuple(sam.id, sam_d.id),
        delegate_tuple(sam.id, sam_d.id),
    }
    return World(
        alex=alex, sam=sam, alex_d=alex_d, sam_d=sam_d, profile=profile, sam_profile=sam_profile
    )


# --- visible_claims ---------------------------------------------------------------------------


async def test_statuses_owner_view(session: Session, fake_fga: FakeFga, alex_world: World) -> None:
    profile, alex = alex_world.profile, alex_world.alex
    for status in ClaimStatus:
        add_claim(session, profile, status.value, status=status)
    session.commit()

    fake_fga.calls.clear()
    result = await visible_claims(session, ui(alex), profile.id, fga=fake_fga)
    assert fake_fga.calls == ["check"]  # one OpenFGA call
    assert result.tier is ViewerTier.only_me
    assert statements(result) == {"active", "provisional"}
    assert {c.claim.statement: c.provisional for c in result.claims} == {
        "active": False,
        "provisional": True,
    }
    assert result.has_provisional


async def test_owner_agent_view_and_configured_default_client(
    session: Session, fake_fga: FakeFga, alex_world: World
) -> None:
    profile = alex_world.profile
    add_claim(session, profile, "a")
    session.commit()
    configure_authz(fake_fga)
    result = await visible_claims(session, mcp(alex_world.alex_d), profile.id)
    assert statements(result) == {"a"}


async def test_share_provisional_false_at_latest_version(
    session: Session, fake_fga: FakeFga, alex_world: World
) -> None:
    profile, alex = alex_world.profile, alex_world.alex
    add_claim(session, profile, "firm", audience=Audience.CONNECTIONS)
    add_claim(session, profile, "maybe", status=ClaimStatus.PROVISIONAL, audience=Audience.PUBLIC)
    add_policy(session, profile, 1, share=True)
    add_policy(session, profile, 2, share=False)
    session.commit()

    # A non-owner tier (v2) does not see provisional facts under the latest policy...
    shared = await _visible_claims_for_tier(session, profile.id, ViewerTier.connections)
    assert statements(shared) == {"firm"}
    assert not shared.has_provisional
    # ...but the owner still does, with the caveat.
    owner = await visible_claims(session, ui(alex), profile.id, fga=fake_fga)
    assert statements(owner) == {"firm", "maybe"}
    assert owner.has_provisional

    add_policy(session, profile, 3, share=True)
    session.commit()
    shared = await _visible_claims_for_tier(session, profile.id, ViewerTier.connections)
    assert statements(shared) == {"firm", "maybe"}


async def test_no_policy_defaults_to_sharing_provisional(
    session: Session, alex_world: World
) -> None:
    profile = alex_world.profile
    add_claim(session, profile, "maybe", status=ClaimStatus.PROVISIONAL, audience=Audience.PUBLIC)
    session.commit()
    shared = await _visible_claims_for_tier(session, profile.id, ViewerTier.public)
    assert statements(shared) == {"maybe"}


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (ViewerTier.public, {"public"}),
        (ViewerTier.connections, {"public", "connections"}),
        (ViewerTier.trusted_circle, {"public", "connections", "trusted_circle"}),
        (ViewerTier.only_me, {"public", "connections", "trusted_circle", "only_me"}),
    ],
)
async def test_audience_rank_filtering(
    session: Session, alex_world: World, tier: ViewerTier, expected: set[str]
) -> None:
    profile = alex_world.profile
    for audience in Audience:
        add_claim(session, profile, audience.value, audience=audience)
        # Hidden statuses never leak at any tier.
        for status in (ClaimStatus.PENDING_REVIEW, ClaimStatus.SUPERSEDED, ClaimStatus.REMOVED):
            add_claim(
                session,
                profile,
                f"{audience.value}-{status.value}",
                audience=audience,
                status=status,
            )
    session.commit()
    result = await _visible_claims_for_tier(session, profile.id, tier)
    assert statements(result) == expected


async def test_tier_none_is_denied(session: Session, alex_world: World) -> None:
    with pytest.raises(AuthorizationDenied):
        await _visible_claims_for_tier(session, alex_world.profile.id, ViewerTier.none)


async def test_cross_user_same_client_gets_nothing(
    session: Session, fake_fga: FakeFga, alex_world: World
) -> None:
    profile = alex_world.profile
    add_claim(session, profile, "secret", audience=Audience.PUBLIC)
    session.commit()
    sams_muse = mcp(alex_world.sam_d)
    assert sams_muse.agent_id == mcp(alex_world.alex_d).agent_id
    with pytest.raises(AuthorizationDenied):
        await visible_claims(session, sams_muse, profile.id, fga=fake_fga)
    with pytest.raises(AuthorizationDenied):
        await owner_claims(session, sams_muse, profile.id, fga=fake_fga)
    with pytest.raises(AuthorizationDenied):
        await visible_claims(session, ui(alex_world.sam), profile.id, fga=fake_fga)


async def test_unconfigured_raises_on_both_paths(session: Session, alex_world: World) -> None:
    profile, alex = alex_world.profile, alex_world.alex
    for fga in (None, FakeFga(configured=False)):
        with pytest.raises(FgaConfigurationError):
            await visible_claims(session, ui(alex), profile.id, fga=fga)
        with pytest.raises(FgaConfigurationError):
            await owner_claims(session, ui(alex), profile.id, fga=fga)


# --- owner_claims -----------------------------------------------------------------------------


async def test_owner_claims_all_statuses_and_filter(
    session: Session, fake_fga: FakeFga, alex_world: World
) -> None:
    profile, alex = alex_world.profile, alex_world.alex
    for status in ClaimStatus:
        add_claim(session, profile, status.value, status=status, delegation=alex_world.alex_d)
    session.commit()
    everything = await owner_claims(session, ui(alex), profile.id, fga=fake_fga)
    assert statements(everything) == {s.value for s in ClaimStatus}
    by_agent = await owner_claims(
        session,
        mcp(alex_world.alex_d),
        profile.id,
        statuses=[ClaimStatus.PENDING_REVIEW],
        fga=fake_fga,
    )
    assert statements(by_agent) == {"pending_review"}


async def test_other_profiles_claims_never_leak(
    session: Session, fake_fga: FakeFga, alex_world: World
) -> None:
    """B2: Sam's claims (every status x audience) never appear on Alex's read paths."""
    for status in ClaimStatus:
        for audience in Audience:
            add_claim(
                session,
                alex_world.sam_profile,
                f"sam-{status.value}-{audience.value}",
                status=status,
                audience=audience,
            )
    add_claim(session, alex_world.profile, "alex-own", audience=Audience.PUBLIC)
    session.commit()
    profile_id = alex_world.profile.id
    for principal in (ui(alex_world.alex), mcp(alex_world.alex_d)):
        shared = await visible_claims(session, principal, profile_id, fga=fake_fga)
        assert statements(shared) == {"alex-own"}
        managed = await owner_claims(session, principal, profile_id, fga=fake_fga)
        assert statements(managed) == {"alex-own"}
        every = await owner_claims(
            session, principal, profile_id, statuses=list(ClaimStatus), fga=fake_fga
        )
        assert statements(every) == {"alex-own"}
    for tier in ViewerTier:
        if tier is not ViewerTier.none:
            result = await _visible_claims_for_tier(session, profile_id, tier)
            assert statements(result) == {"alex-own"}


# --- on_profile_created -----------------------------------------------------------------------


async def test_on_profile_created_writes_only_owner_and_agents_follow(
    session: Session, fake_fga: FakeFga
) -> None:
    agent, other_client = add_agent(session), add_agent(session, "client_other")
    alex = add_user(session, "alex")
    active = add_delegation(session, alex, agent)
    gone = add_delegation(session, alex, other_client, active=False)
    session.commit()
    # Identity hooks already wrote the delegation tuples for the active delegation only.
    fake_fga.tuples |= {acts_for_tuple(alex.id, active.id), delegate_tuple(alex.id, active.id)}
    configure_authz(fake_fga)

    # Same transaction as the (uncommitted) profile insert, via the caller's session.
    profile = add_profile(session, alex)
    result = await on_profile_created(session, profile_id=profile.id, user_id=alex.id)
    session.commit()
    assert result is not None
    assert result.written == (owner_tuple(alex.id, profile.id),)
    assert (await visible_claims(session, mcp(active), profile.id, fga=fake_fga)).tier is (
        ViewerTier.only_me
    )
    with pytest.raises(AuthorizationDenied):
        await visible_claims(session, mcp(gone), profile.id, fga=fake_fga)
    # Idempotent.
    before = set(fake_fga.tuples)
    await on_profile_created(session, profile_id=profile.id, user_id=alex.id)
    assert fake_fga.tuples == before


async def test_on_profile_created_unconfigured_and_failures(
    session: Session, fake_fga: FakeFga, caplog: pytest.LogCaptureFixture
) -> None:
    alex, bob = add_user(session, "alex"), add_user(session, "bob")
    profile = add_profile(session, alex)
    session.commit()
    assert await on_profile_created(session, profile_id=profile.id, user_id=alex.id) is None
    assert "not configured" in caplog.text

    configure_authz(fake_fga)
    with pytest.raises(ValueError):
        await on_profile_created(session, profile_id=profile.id, user_id=bob.id)
    fake_fga.error = RuntimeError("openfga unavailable")
    with pytest.raises(RuntimeError):
        await on_profile_created(session, profile_id=profile.id, user_id=alex.id)


# --- Reconcile --------------------------------------------------------------------------------

LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


def _old(fga: FakeFga, *tuples: RelationTuple) -> None:
    """Add tuples written long ago (older than any prune min-age)."""
    for t in tuples:
        fga.tuples.add(t)
        fga.written_at[t] = LONG_AGO


async def _reconcile(
    fga: FakeFga, factory: sessionmaker[Session], *, prune: bool
) -> fga_reconcile.ReconcileSummary:
    return await fga_reconcile.run(prune=prune, fga=fga, sessionmaker_provider=lambda: factory)


async def test_reconcile_idempotent_revokes_and_prunes(
    factory: sessionmaker[Session], session: Session, fake_fga: FakeFga
) -> None:
    agent, other_client = add_agent(session), add_agent(session, "client_other")
    alex, newbie = add_user(session, "alex"), add_user(session, "newbie")
    active = add_delegation(session, alex, agent)
    gone = add_delegation(session, alex, other_client, active=False)
    newbie_d = add_delegation(session, newbie, agent)  # no profile yet
    profile = add_profile(session, alex)
    session.commit()

    stray = uuid4()
    orphans = {acts_for_tuple(alex.id, stray), delegate_tuple(alex.id, stray)}
    _old(fake_fga, acts_for_tuple(alex.id, gone.id), delegate_tuple(alex.id, gone.id), *orphans)
    expected = {
        owner_tuple(alex.id, profile.id),
        acts_for_tuple(alex.id, active.id),
        delegate_tuple(alex.id, active.id),
        acts_for_tuple(newbie.id, newbie_d.id),
        delegate_tuple(newbie.id, newbie_d.id),
    }

    first = await _reconcile(fake_fga, factory, prune=False)
    assert first.revoked_delegations == 1
    assert fake_fga.tuples == expected | orphans
    assert await _reconcile(fake_fga, factory, prune=False) == first

    pruned = await _reconcile(fake_fga, factory, prune=True)
    assert pruned.pruned_tuples == 2
    assert fake_fga.tuples == expected
    assert (await _reconcile(fake_fga, factory, prune=True)).pruned_tuples == 0


async def test_prune_keeps_post_snapshot_delegation_and_fresh_orphans(
    factory: sessionmaker[Session],
    session: Session,
    fake_fga: FakeFga,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B3: a delegation created after the snapshot, and young orphans, survive --prune."""
    agent = add_agent(session)
    alex = add_user(session, "alex")
    session.commit()

    late_ids: list[UUID] = []
    real_snapshot = fga_reconcile._snapshot

    def snapshot_then_insert(provider: fga_reconcile.SessionFactory) -> fga_reconcile.Snapshot:
        snap = real_snapshot(provider)
        # After the snapshot an active delegation commits; its (old-looking) tuples exist.
        with factory() as other, other.begin():
            late = add_delegation(other, alex, agent)
            late_ids.append(late.id)
        _old(fake_fga, acts_for_tuple(alex.id, late.id), delegate_tuple(alex.id, late.id))
        return snap

    monkeypatch.setattr(fga_reconcile, "_snapshot", snapshot_then_insert)
    young_orphan, old_orphan = uuid4(), uuid4()
    await fake_fga.write_tuples([acts_for_tuple(alex.id, young_orphan)])  # written now
    _old(fake_fga, acts_for_tuple(alex.id, old_orphan), delegate_tuple(alex.id, old_orphan))

    result = await _reconcile(fake_fga, factory, prune=True)
    late = late_ids[0]
    assert acts_for_tuple(alex.id, late) in fake_fga.tuples
    assert delegate_tuple(alex.id, late) in fake_fga.tuples
    assert acts_for_tuple(alex.id, young_orphan) in fake_fga.tuples
    assert acts_for_tuple(alex.id, old_orphan) not in fake_fga.tuples
    assert delegate_tuple(alex.id, old_orphan) not in fake_fga.tuples
    assert result.pruned_tuples == 2
    assert result.kept_recent_or_active == 3


async def test_reconcile_revokes_delegation_disconnected_after_snapshot(
    factory: sessionmaker[Session],
    session: Session,
    fake_fga: FakeFga,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = add_agent(session)
    alex = add_user(session, "alex")
    d = add_delegation(session, alex, agent)
    session.commit()
    real_snapshot = fga_reconcile._snapshot

    def snapshot_then_disconnect(provider: fga_reconcile.SessionFactory) -> fga_reconcile.Snapshot:
        snap = real_snapshot(provider)
        with factory() as other, other.begin():
            row = other.get(AgentDelegation, d.id)
            assert row is not None
            row.status = DelegationStatus.DISCONNECTED.value
            row.disconnected_at = datetime.now(UTC)
        return snap

    monkeypatch.setattr(fga_reconcile, "_snapshot", snapshot_then_disconnect)
    result = await _reconcile(fake_fga, factory, prune=False)
    assert result.revoked_after_recheck == 1
    assert fake_fga.tuples == set()


async def test_reconcile_requires_configuration(factory: sessionmaker[Session]) -> None:
    with pytest.raises(FgaConfigurationError):
        await _reconcile(FakeFga(configured=False), factory, prune=False)


# --- Real OpenFGA -----------------------------------------------------------------------------


def _openfga_reachable() -> bool:
    if not OPENFGA_TEST_API_URL:
        return False
    try:
        return httpx.get(f"{OPENFGA_TEST_API_URL}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.openfga
async def test_reconcile_then_visible_claims_on_real_openfga(
    factory: sessionmaker[Session], session: Session
) -> None:
    if not _openfga_reachable():
        pytest.skip("OPENFGA_TEST_API_URL is not set or not reachable")
    base = Settings(_env_file=None, openfga_api_url=OPENFGA_TEST_API_URL)
    boot = await fga_bootstrap.bootstrap(base, store_name=f"revel-test-{uuid4()}")
    settings = base.model_copy(
        update={
            "openfga_store_id": boot.store_id,
            "openfga_authorization_model_id": boot.authorization_model_id,
        }
    )
    fga = create_fga_client(settings)
    try:
        agent = add_agent(session)
        alex, sam = add_user(session, "alex"), add_user(session, "sam")
        alex_d, sam_d = add_delegation(session, alex, agent), add_delegation(session, sam, agent)
        profile = add_profile(session, alex)
        add_profile(session, sam)
        add_claim(session, profile, "likes tea", audience=Audience.PUBLIC)
        add_claim(session, profile, "draft", status=ClaimStatus.PENDING_REVIEW)
        session.commit()

        await fga_reconcile.run(prune=True, fga=fga, sessionmaker_provider=lambda: factory)
        result = await visible_claims(session, mcp(alex_d), profile.id, fga=fga)
        assert statements(result) == {"likes tea"}
        with pytest.raises(AuthorizationDenied):
            await visible_claims(session, mcp(sam_d), profile.id, fga=fga)

        # Disconnect in Postgres, reconcile: alex's agent loses access.
        session.get(AgentDelegation, alex_d.id).status = DelegationStatus.DISCONNECTED.value
        session.get(AgentDelegation, alex_d.id).disconnected_at = datetime.now(UTC)
        session.commit()
        await fga_reconcile.run(prune=False, fga=fga, sessionmaker_provider=lambda: factory)
        with pytest.raises(AuthorizationDenied):
            await visible_claims(session, mcp(alex_d), profile.id, fga=fga)
        remaining: list[RelationTuple] = await fga.read_tuples(
            relation="acts_for", object=f"agent:{alex_d.id}"
        )
        assert remaining == []
    finally:
        await fga.sdk.delete_store()
        await fga.close()
