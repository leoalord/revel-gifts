"""F10 demo reset (RG-45 / Build Tasks #23) against real PostgreSQL with F4's FakeFga.

Covers: the dry run writes nothing; refusals (wrong target, missing --no-active-hold, missing
handle, non-demo user in scope); the clear (questions, claims incl. a supersede chain, extra
policy versions) leaving non-demo data untouched; users, agents, delegations (with
authorized_sid), delegation sessions, handles and profiles surviving; connections (pending
Sam -> Leo declined, approved Sam connections revoked, the Leo <-> Jordan baseline kept, a
missing baseline reported); the re-seed and its manifest; and the verify step.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from authz_fakes import FakeFga
from demo_helpers import Person, World, add_policy, connect
from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from revel.auth.authz import AuthorizationDenied, ViewerTier
from revel.auth.visibility import visible_claims
from revel.db.enums import ConnectionRequestStatus, ConnectionTier
from revel.db.models import (
    Agent,
    AgentDelegation,
    AgentDelegationSession,
    Answer,
    AnswerEvidence,
    Assignment,
    Claim,
    ClaimEvent,
    ConnectionRequest,
    ContributionPolicy,
    Handle,
    Profile,
    Question,
    QuestionEvent,
    User,
)
from revel.demo import reset
from revel.demo.seed import SEED_FACTS
from revel.services import connections
from revel.settings import Settings

pytestmark = pytest.mark.anyio

LATER = datetime.now(UTC) + timedelta(days=3)
ALL_MODELS = (
    User,
    Agent,
    AgentDelegation,
    AgentDelegationSession,
    Handle,
    Profile,
    ContributionPolicy,
    Claim,
    ClaimEvent,
    Question,
    Assignment,
    QuestionEvent,
    Answer,
    AnswerEvidence,
    ConnectionRequest,
)


# --- Builders ---------------------------------------------------------------------------------


def add_claim(session: Session, person: Person, **values: Any) -> UUID:
    assert person.profile is not None
    row = {
        "profile_id": person.profile.id,
        "category": "interest",
        "statement": "Synthetic fact",
        "audience": "connections",
        "evidence_type": "observed",
        "status": "active",
        "contributor_user_id": person.user.id,
        "contributed_via": "ui",
    }
    row.update(values)
    claim_id = session.execute(insert(Claim).values(**row).returning(Claim.id)).scalar_one()
    session.execute(
        insert(ClaimEvent).values(
            claim_id=claim_id,
            profile_id=person.profile.id,
            action="created",
            actor_user_id=person.user.id,
        )
    )
    return claim_id


def add_chain(session: Session, person: Person) -> list[UUID]:
    """Three facts, each superseding the previous one."""
    c1 = add_claim(session, person, status="superseded")
    c2 = add_claim(session, person, status="superseded", supersedes_id=c1)
    c3 = add_claim(session, person, supersedes_id=c2)
    return [c1, c2, c3]


def add_question(
    session: Session,
    about: Person,
    requester: Person,
    respondent: Person,
    *,
    cite: UUID | None = None,
) -> UUID:
    """An open question with an assignment, events, an answer and answer evidence."""
    assert about.profile is not None
    qid = session.execute(
        insert(Question)
        .values(
            profile_id=about.profile.id,
            requester_user_id=requester.user.id,
            requested_information="Synthetic question",
            expires_at=LATER,
        )
        .returning(Question.id)
    ).scalar_one()
    aid = session.execute(
        insert(Assignment)
        .values(question_id=qid, respondent_delegation_id=respondent.delegation.id)
        .returning(Assignment.id)
    ).scalar_one()
    session.execute(
        insert(QuestionEvent).values(
            question_id=qid, assignment_id=aid, event_type="made_available"
        )
    )
    session.execute(
        insert(QuestionEvent).values(
            question_id=qid, event_type="retrieved", actor_user_id=respondent.user.id
        )
    )
    ans = session.execute(
        insert(Answer)
        .values(
            question_id=qid,
            profile_id=about.profile.id,
            respondent_user_id=respondent.user.id,
            body="Synthetic answer",
            audience="connections",
        )
        .returning(Answer.id)
    ).scalar_one()
    evidence: dict[str, Any] = (
        {"claim_id": cite}
        if cite
        else {
            "category": "interest",
            "proposed_audience": "connections",
            "evidence_type": "observed",
            "observed_precision": "unknown",
        }
    )
    session.execute(
        insert(AnswerEvidence).values(answer_id=ans, profile_id=about.profile.id, **evidence)
    )
    return qid


def table_counts(session: Session) -> dict[str, int]:
    session.expire_all()
    return {
        m.__tablename__: session.execute(select(func.count()).select_from(m)).scalar_one()
        for m in ALL_MODELS
    }


def request_states(session: Session) -> dict[UUID, tuple[str, str | None]]:
    session.expire_all()
    return {r.id: (r.status, r.granted_tier) for r in session.scalars(select(ConnectionRequest))}


async def run(
    sessions: Any,
    fga: FakeFga,
    settings: Settings,
    *,
    apply: bool = False,
    hold: bool = True,
    target: str = "dev",
    ids: dict[str, UUID] | None = None,
) -> tuple[int, str]:
    out = io.StringIO()
    extra: dict[str, Any] = {} if ids is None else {"expected_user_ids": ids}
    code = await reset.run(
        target=target,
        apply=apply,
        no_active_hold=hold,
        settings=settings,
        sessions=sessions,
        fga=fga,
        out=out,
        **extra,
    )
    return code, out.getvalue()


def manifest_of(output: str) -> dict[str, Any]:
    start = output.index(reset.MANIFEST_BEGIN) + len(reset.MANIFEST_BEGIN)
    end = output.index(reset.MANIFEST_END)
    return json.loads(output[start:end])


def messy(session: Session, w: World) -> dict[str, Any]:
    """Leftovers of an earlier take, plus non-demo data that must survive."""
    chain = add_chain(session, w.leo)
    loose = add_claim(session, w.leo, statement="Owns synthetic item")
    q_leo = add_question(session, w.leo, w.sam, w.leo, cite=loose)
    q_by_jordan = add_question(session, w.leo, w.jordan, w.leo)
    add_policy(session, w.leo, 2)
    add_policy(session, w.leo, 3)
    # Non-demo data: Cal's own facts, policy and a question between Cal and nobody demo.
    cal_claims = add_chain(session, w.cal)
    add_policy(session, w.cal, 1)
    add_policy(session, w.cal, 2)
    q_cal = add_question(session, w.cal, w.cal, w.cal, cite=cal_claims[-1])
    session.commit()
    return {
        "chain": chain,
        "loose": loose,
        "questions": [q_leo, q_by_jordan],
        "cal_claims": cal_claims,
        "cal_question": q_cal,
    }


# --- Dry run ----------------------------------------------------------------------------------


async def test_dry_run_writes_nothing(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    messy(session, world)
    pending = await connect(sessions, world.sam, world.leo, None)
    before, states, tuples = table_counts(session), request_states(session), set(fga.tuples)
    writes = [c for c in fga.calls if c in ("write", "delete")]

    code, out = await run(sessions, fga, dev_settings)

    assert code == reset.EXIT_OK
    assert table_counts(session) == before
    assert request_states(session) == states
    assert fga.tuples == tuples
    assert [c for c in fga.calls if c in ("write", "delete")] == writes
    assert "DRY RUN" in out and "Scope check: PASS" in out
    assert f"decline {pending}" in out
    assert "questions: 2" in out and "claims: 4" in out and "contribution_policies: 2" in out
    # Counts and ids only: no fact/question/answer text, no display names.
    for secret in ("Synthetic", "Owns synthetic"):
        assert secret not in out


# --- Refusals ---------------------------------------------------------------------------------


async def test_refuses_wrong_target(
    session: Session, sessions: Any, fga: FakeFga, world: World
) -> None:
    before = table_counts(session)
    local = Settings(_env_file=None, app_env="test", public_base_url="http://localhost:8000")
    code, _ = await run(sessions, fga, local, apply=True)
    assert code == reset.EXIT_REFUSED
    code, _ = await run(sessions, fga, Settings(_env_file=None, public_base_url="https://x.test"))
    assert code == reset.EXIT_REFUSED
    assert table_counts(session) == before
    with pytest.raises(SystemExit):
        reset.main(["--target", "prod"])


async def test_refuses_apply_without_no_active_hold(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    messy(session, world)
    before = table_counts(session)
    code, out = await run(sessions, fga, dev_settings, apply=True, hold=False)
    assert code == reset.EXIT_REFUSED
    assert out == ""
    assert table_counts(session) == before


async def test_refuses_missing_handle(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    session.execute(
        update(Handle).where(Handle.user_id == world.jordan.user.id).values(handle="jordan_x")
    )
    session.commit()
    before = table_counts(session)
    for apply in (False, True):
        code, _ = await run(sessions, fga, dev_settings, apply=apply)
        assert code == reset.EXIT_REFUSED
    assert table_counts(session) == before


async def test_refuses_non_demo_user_in_scope(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    messy(session, world)
    # A question about Leo answered by non-demo Cal: the scope touches Cal.
    add_question(session, world.leo, world.sam, world.cal)
    session.commit()
    before, tuples = table_counts(session), set(fga.tuples)
    code, out = await run(sessions, fga, dev_settings, apply=True)
    assert code == reset.EXIT_REFUSED
    assert "Scope check: FAIL" in out
    assert "assignments_to_non_demo_respondent: 1" in out
    assert table_counts(session) == before
    assert fga.tuples == tuples


async def test_refuses_question_by_demo_user_about_non_demo_profile(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    add_question(session, world.cal, world.sam, world.cal)
    session.commit()
    before = table_counts(session)
    code, out = await run(sessions, fga, dev_settings, apply=True)
    assert code == reset.EXIT_REFUSED
    assert "questions_about_non_demo_profile: 1" in out
    assert table_counts(session) == before


# --- Apply: clear, survival, re-seed, verify --------------------------------------------------


async def test_apply_clears_demo_data_and_keeps_everything_else(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    made = messy(session, world)
    with session.begin():
        delegations = {
            (d.id, d.user_id, d.agent_id, d.status, d.authorized_sid)
            for d in session.scalars(select(AgentDelegation))
        }
        delegation_sessions = {
            (s.delegation_id, s.sid) for s in session.scalars(select(AgentDelegationSession))
        }
        users = {(u.id, u.workos_user_id) for u in session.scalars(select(User))}
        handles = {(h.user_id, h.handle) for h in session.scalars(select(Handle))}
        profs = {(p.id, p.user_id) for p in session.scalars(select(Profile))}
        agents = {a.id for a in session.scalars(select(Agent))}

    code, out = await run(sessions, fga, dev_settings, apply=True)
    assert code == reset.EXIT_OK, out
    session.expire_all()

    # Demo questions and all their children are gone; Cal's question is untouched.
    leo_profile = world.leo.profile.id
    assert session.scalar(select(func.count()).select_from(Question)) == 1
    assert session.scalar(select(Question.id)) == made["cal_question"]
    assert session.scalar(select(func.count()).select_from(Assignment)) == 1
    assert session.scalar(select(func.count()).select_from(Answer)) == 1
    assert session.scalar(select(func.count()).select_from(AnswerEvidence)) == 1
    assert session.scalar(select(func.count()).select_from(QuestionEvent)) == 2

    # Old claims (incl. the supersede chain) gone; exactly the 6 seeds remain on Leo's profile.
    leo_claims = list(session.scalars(select(Claim).where(Claim.profile_id == leo_profile)))
    old = set(made["chain"]) | {made["loose"]}
    assert not old & {c.id for c in leo_claims}
    assert len(leo_claims) == len(SEED_FACTS) == 6
    cal_claims = set(
        session.scalars(select(Claim.id).where(Claim.profile_id == world.cal.profile.id))
    )
    assert cal_claims == set(made["cal_claims"])
    assert (
        session.scalar(
            select(func.count())
            .select_from(ClaimEvent)
            .where(ClaimEvent.profile_id == world.cal.profile.id)
        )
        == 3
    )

    # Policies: Leo keeps only v1; Cal keeps both versions.
    versions = dict.fromkeys((leo_profile, world.cal.profile.id), None)
    for pid in versions:
        versions[pid] = sorted(
            session.scalars(
                select(ContributionPolicy.version).where(ContributionPolicy.profile_id == pid)
            )
        )
    assert versions == {leo_profile: [1], world.cal.profile.id: [1, 2]}

    # Users, agents, delegations (incl. authorized_sid), delegation sessions, handles, profiles.
    assert {
        (d.id, d.user_id, d.agent_id, d.status, d.authorized_sid)
        for d in session.scalars(select(AgentDelegation))
    } == delegations
    assert {
        (s.delegation_id, s.sid) for s in session.scalars(select(AgentDelegationSession))
    } == delegation_sessions
    assert all(sid for *_, sid in delegations)
    assert {(u.id, u.workos_user_id) for u in session.scalars(select(User))} == users
    assert {(h.user_id, h.handle) for h in session.scalars(select(Handle))} == handles
    assert {(p.id, p.user_id) for p in session.scalars(select(Profile))} == profs
    assert {a.id for a in session.scalars(select(Agent))} == agents
    assert "agent_delegation_sessions: 3 -> 3" in out


async def test_apply_reseeds_prepared_facts_and_prints_manifest(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    messy(session, world)
    code, out = await run(sessions, fga, dev_settings, apply=True)
    assert code == reset.EXIT_OK, out

    manifest = manifest_of(out)
    facts = {f["seed_key"]: f for f in manifest["facts"]}
    assert set(facts) == {
        "birdwatching",
        "compact_gear",
        "candles",
        "ring_size",
        "documentaries",
        "shoe_size",
    }
    assert all(f["label"] == "prepared" for f in facts.values())
    assert facts["shoe_size"]["audience"] == "trusted_circle"
    assert {k for k, f in facts.items() if f["audience"] == "connections"} == set(facts) - {
        "shoe_size"
    }
    session.expire_all()
    claims = {
        c.id: c
        for c in session.scalars(select(Claim).where(Claim.profile_id == world.leo.profile.id))
    }
    assert {UUID(f["claim_id"]) for f in facts.values()} == set(claims)
    for f in facts.values():
        c = claims[UUID(f["claim_id"])]
        assert (c.audience, c.status, c.contributed_via) == (f["audience"], "active", "ui")
        assert c.contributor_user_id == world.leo.user.id
    assert not any("binocular" in c.statement.lower() for c in claims.values())
    assert not any("binocular" in s.statement.lower() for s in SEED_FACTS)
    # Fact text never reaches the output.
    for s in SEED_FACTS:
        assert s.statement not in out


async def test_verify_sam_denied_jordan_permitted(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    code, out = await run(sessions, fga, dev_settings, apply=True)
    assert code == reset.EXIT_OK, out
    assert "sam_demo read of recipient profile: denied (ok)" in out
    assert "jordan_demo read of recipient profile: permitted at trusted_circle" in out

    shoe = UUID(
        next(f for f in manifest_of(out)["facts"] if f["seed_key"] == "shoe_size")["claim_id"]
    )
    with pytest.raises(AuthorizationDenied):
        await visible_claims(session, world.sam.ui, world.leo.profile.id, fga=fga)
    seen = await visible_claims(session, world.jordan.ui, world.leo.profile.id, fga=fga)
    assert seen.tier is ViewerTier.trusted_circle
    assert shoe in {c.id for c in seen.claims}
    assert len(seen.claims) == 6


# --- Connections ------------------------------------------------------------------------------


async def test_connections_cleared_except_baseline(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    w = world
    baseline = session.scalar(
        select(ConnectionRequest.id).where(
            ConnectionRequest.requester_user_id == w.jordan.user.id,
            ConnectionRequest.target_user_id == w.leo.user.id,
        )
    )
    sam_to_leo = await connect(sessions, w.sam, w.leo, None)  # pending
    leo_to_sam = await connect(sessions, w.leo, w.sam, ConnectionTier.CONNECTIONS)  # approved
    sam_to_jordan = await connect(sessions, w.sam, w.jordan, ConnectionTier.TRUSTED_CIRCLE)
    jordan_to_sam = await connect(sessions, w.jordan, w.sam, None)
    await connections.block(w.sam.ui, jordan_to_sam, sessions=sessions)  # Sam blocks Jordan
    cal_to_leo = await connect(sessions, w.cal, w.leo, None)  # non-demo: left alone

    code, out = await run(sessions, fga, dev_settings, apply=True)
    assert code == reset.EXIT_OK, out

    states = request_states(session)
    assert states[sam_to_leo][0] == ConnectionRequestStatus.DECLINED.value
    assert states[leo_to_sam][0] == ConnectionRequestStatus.REVOKED.value
    assert states[sam_to_jordan][0] == ConnectionRequestStatus.REVOKED.value
    blocked_now = [
        r
        for r in session.scalars(
            select(ConnectionRequest).where(ConnectionRequest.status == "blocked")
        )
    ]
    assert blocked_now == []
    assert states[baseline] == ("approved", "trusted_circle")
    assert states[cal_to_leo][0] == "pending"
    assert "requests with a non-demo party (left alone): 1" in out
    assert "live_requests_among_demo: 5 -> 1" in out
    # Sam: no live request to or from Leo.
    live = ("pending", "approved", "blocked")
    assert not [
        rid
        for rid, (status, _) in states.items()
        if status in live and rid in (sam_to_leo, leo_to_sam)
    ]


async def test_missing_baseline_is_reported_and_fails(
    session: Session,
    factory: sessionmaker[Session],
    sessions: Any,
    fga: FakeFga,
    world: World,
    dev_settings: Settings,
) -> None:
    baseline = session.scalar(select(ConnectionRequest.id))
    await connections.revoke(world.leo.ui, baseline, sessions=sessions)
    messy(session, world)
    sam_to_leo = await connect(sessions, world.sam, world.leo, None)
    before, states, tuples = table_counts(session), request_states(session), set(fga.tuples)

    # Both the dry run and --apply refuse (exit 2) BEFORE any destructive phase.
    code, out = await run(sessions, fga, dev_settings)
    assert code == reset.EXIT_REFUSED
    assert "ERROR: baseline missing" in out and "Connections check: FAIL" in out
    code, out = await run(sessions, fga, dev_settings, apply=True)
    assert code == reset.EXIT_REFUSED
    assert "ERROR: baseline missing" in out
    assert "Phase A" not in out
    assert table_counts(session) == before
    assert request_states(session) == states
    assert states[sam_to_leo][0] == "pending"
    assert fga.tuples == tuples
    # It is reported, never created.
    with factory() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(ConnectionRequest)
                .where(ConnectionRequest.status == "approved")
            )
            == 0
        )


async def test_wrong_tier_baseline_refuses_before_phase_a(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    baseline = session.scalar(select(ConnectionRequest.id))
    await connections.change_tier(
        world.leo.ui, baseline, tier=ConnectionTier.CONNECTIONS, sessions=sessions
    )
    messy(session, world)
    before, states = table_counts(session), request_states(session)
    for apply in (False, True):
        code, out = await run(sessions, fga, dev_settings, apply=apply)
        assert code == reset.EXIT_REFUSED
        assert "approved at connections, not trusted_circle" in out
    assert table_counts(session) == before
    assert request_states(session) == states
    assert states[baseline] == ("approved", "connections")


# --- Pinned user ids --------------------------------------------------------------------------


async def test_refuses_handle_resolving_to_unexpected_user(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    messy(session, world)
    before = table_counts(session)
    ids = {p.handle: p.user.id for p in (world.leo, world.sam, world.jordan)}
    ids["sam_demo"] = world.cal.user.id  # sam_demo now names someone else
    for apply in (False, True):
        code, out = await run(sessions, fga, dev_settings, apply=apply, ids=ids)
        assert code == reset.EXIT_REFUSED
        assert out == ""
    assert table_counts(session) == before
    # The recipient pinned to the shipped dev id does not match this world's Leo either.
    ids = {p.handle: p.user.id for p in (world.leo, world.sam, world.jordan)}
    ids["leo_walker"] = UUID("cce306e8-2ac1-4a9b-bedd-4d0f634b0f0a")
    code, _ = await run(sessions, fga, dev_settings, apply=True, ids=ids)
    assert code == reset.EXIT_REFUSED
    assert table_counts(session) == before


def test_pinned_dev_user_ids() -> None:
    from revel.demo import seed

    # Read the module source constants (the world fixture is not active here).
    assert {h: str(u) for h, u in seed.DEMO_USER_IDS.items()} == {
        "leo_walker": "cce306e8-2ac1-4a9b-bedd-4d0f634b0f0a",
        "sam_demo": "3a39e3cf-0f4c-4b99-8cf4-78abc66dcab2",
        "jordan_demo": "b2f16f94-e4a4-461b-b299-289e1cf0be15",
    }
    assert set(seed.DEMO_USER_IDS) == set(seed.DEMO_HANDLES)


# --- Adversarial scope cases ------------------------------------------------------------------


async def assert_refused_unchanged(
    session: Session, sessions: Any, fga: FakeFga, settings: Settings, violation: str
) -> None:
    before, states, tuples = table_counts(session), request_states(session), set(fga.tuples)
    code, out = await run(sessions, fga, settings, apply=True)
    assert code == reset.EXIT_REFUSED
    assert "Scope check: FAIL" in out
    assert f"{violation}: 1" in out
    assert "Phase A" not in out
    assert table_counts(session) == before
    assert request_states(session) == states
    assert fga.tuples == tuples


async def test_refuses_non_demo_answer_citing_leo_claim(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    made = messy(session, world)
    add_question(session, world.leo, world.sam, world.cal, cite=made["loose"])
    session.commit()
    await assert_refused_unchanged(
        session, sessions, fga, dev_settings, "answers_by_non_demo_respondent"
    )


async def test_refuses_non_demo_requester_about_leo(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    messy(session, world)
    add_question(session, world.leo, world.cal, world.leo)
    session.commit()
    await assert_refused_unchanged(
        session, sessions, fga, dev_settings, "questions_by_non_demo_requester"
    )


async def test_refuses_non_demo_approver_of_leo_policy_version(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    messy(session, world)  # Leo has v1-v3
    session.add(
        ContributionPolicy(
            profile_id=world.leo.profile.id,
            version=4,
            approved_by_user_id=world.cal.user.id,
            approved_at=datetime.now(UTC),
        )
    )
    session.commit()
    await assert_refused_unchanged(
        session, sessions, fga, dev_settings, "policies_approved_by_non_demo_user"
    )


async def test_question_inserted_between_plan_and_phase_a_refuses(
    session: Session,
    factory: sessionmaker[Session],
    sessions: Any,
    fga: FakeFga,
    world: World,
    dev_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messy(session, world)
    original = reset.plan_connections

    async def plan_then_insert(*args: Any, **kwargs: Any) -> reset.ConnPlan:
        plan = await original(*args, **kwargs)
        # Runs after the read-only plan and before Phase A: a new demo question appears.
        with factory() as s:
            add_question(s, world.leo, world.jordan, world.leo)
            s.commit()
        return plan

    monkeypatch.setattr(reset, "plan_connections", plan_then_insert)
    states, tuples = request_states(session), set(fga.tuples)
    code, out = await run(sessions, fga, dev_settings, apply=True)
    assert code == reset.EXIT_REFUSED
    assert "Phase A: deleted" not in out
    # Nothing deleted: the 2 planned demo questions + the new one + Cal's are all still there.
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Question)) == 4
    assert session.scalar(select(func.count()).select_from(Claim)) == 7
    assert request_states(session) == states
    assert fga.tuples == tuples


async def test_apply_twice_is_idempotent(
    session: Session, sessions: Any, fga: FakeFga, world: World, dev_settings: Settings
) -> None:
    messy(session, world)
    await connect(sessions, world.sam, world.leo, None)
    code1, out1 = await run(sessions, fga, dev_settings, apply=True)
    assert code1 == reset.EXIT_OK, out1
    code2, out2 = await run(sessions, fga, dev_settings, apply=True)
    assert code2 == reset.EXIT_OK, out2
    assert "decline: 0" in out2 and "revoke: 0" in out2
    first = {f["claim_id"] for f in manifest_of(out1)["facts"]}
    second = {f["claim_id"] for f in manifest_of(out2)["facts"]}
    assert len(second) == 6 and not first & second
    session.expire_all()
    leo_claims = set(
        session.scalars(select(Claim.id).where(Claim.profile_id == world.leo.profile.id))
    )
    assert {UUID(c) for c in second} == leo_claims
    assert "live_requests_among_demo: 1 -> 1" in out2


# --- Database errors --------------------------------------------------------------------------


async def test_lock_timeout_in_phase_a_exits_cleanly_and_rolls_back(
    session: Session,
    factory: sessionmaker[Session],
    sessions: Any,
    fga: FakeFga,
    world: World,
    dev_settings: Settings,
) -> None:
    made = messy(session, world)
    before = table_counts(session)
    # Another transaction holds a row lock on a planned claim: Phase A's delete waits past
    # its 3s lock_timeout and fails with OperationalError.
    with factory() as locker, locker.begin():
        locker.execute(select(Claim.id).where(Claim.id == made["loose"]).with_for_update())
        code, out = await run(sessions, fga, dev_settings, apply=True)
    assert code == reset.EXIT_FAILED
    assert "ERROR: phase A (clear) failed (OperationalError)" in out
    assert "Traceback" not in out and "Synthetic" not in out
    assert table_counts(session) == before  # rolled back as a whole


async def test_integrity_error_in_phase_c_exits_cleanly(
    session: Session,
    sessions: Any,
    fga: FakeFga,
    world: World,
    dev_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import IntegrityError

    async def failing_reseed(*args: Any, **kwargs: Any) -> Any:
        raise IntegrityError("INSERT", {}, Exception("synthetic"))

    monkeypatch.setattr(reset, "reseed", failing_reseed)
    code, out = await run(sessions, fga, dev_settings, apply=True)
    assert code == reset.EXIT_FAILED
    assert "ERROR: phase C (re-seed) failed (IntegrityError)" in out
    assert reset.MANIFEST_BEGIN not in out


async def test_unconfigured_openfga_refuses_apply(
    session: Session, sessions: Any, world: World, dev_settings: Settings
) -> None:
    messy(session, world)
    before = table_counts(session)
    code, _ = await run(sessions, FakeFga(configured=False), dev_settings, apply=True)
    assert code == reset.EXIT_REFUSED
    assert table_counts(session) == before
