"""Leases, evidence-aware answers and answer retrieval (#20 / RG-39) against real PostgreSQL and
OpenFGA (or F4's fake when ``OPENFGA_TEST_API_URL`` is unreachable). Time is controlled through
``QuestionDeps.now``; no sleeping. See conftest for requirements."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import anyio
import pytest
from questions_support import (
    PURPOSE,
    REQUESTED,
    World,
    agent_principal,
    requires_db,
)
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from revel.auth.authz import public_tuples, tier_tuple
from revel.db.enums import Audience, ConnectionTier
from revel.db.models import (
    Answer,
    AnswerEvidence,
    Assignment,
    Claim,
    ContributionPolicy,
    Question,
    QuestionEvent,
)
from revel.services import answers, question_leases
from revel.services.answers import (
    InvalidEvidence,
    QuestionNotFound,
    SubmitAnswerRequest,
    cancel_question,
    get_answer,
    get_question,
    get_question_status,
    submit_answer,
)
from revel.services.question_leases import (
    HUMAN_RESPONSE_WINDOW,
    LEASE_DURATION,
    MAX_RENEWALS,
    AlreadyClaimed,
    AssignmentNotFound,
    ClaimRequest,
    DeclineRequest,
    HumanContactRequired,
    HumanContactTaken,
    LeaseExpired,
    LeaseVersionRequest,
    OutreachCooldown,
    QuestionClosed,
    QuestionExpired,
    RenewalLimitReached,
    StaleLease,
    UserContactRequest,
    claim_assignment,
    decline_assignment,
    release_assignment,
    renew_assignment,
    report_user_contact,
)
from revel.services.questions import (
    QuestionDeps,
    QuestionNotPermitted,
    QuestionServiceUnavailable,
    SubmitQuestionRequest,
    get_updates,
    submit_question,
)

pytestmark = [pytest.mark.anyio, requires_db]

T0 = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=1)


# --- Helpers ------------------------------------------------------------------------------------


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def deps(factory: sessionmaker[Session], clock: Clock | None = None, **kw: Any) -> QuestionDeps:
    return QuestionDeps(sessionmaker=factory, now=clock or (lambda: T0), **kw)


async def ask(
    factory: sessionmaker[Session], world: World, *, hours: float = 72
) -> tuple[UUID, dict[UUID, UUID]]:
    """Submit a question (fallback routing to both active owner assistants). Returns the
    question id and {respondent delegation id: assignment id}."""
    request = SubmitQuestionRequest.model_validate(
        {
            "handle": "chelsea",
            "requested_information": REQUESTED,
            "private_purpose": PURPOSE,
            "expires_in_hours": hours,
        }
    )
    result = await submit_question(agent_principal(world.requester_d), request, deps=deps(factory))
    with factory() as s:
        rows = s.execute(
            select(Assignment.respondent_delegation_id, Assignment.id).where(
                Assignment.question_id == result.question_id
            )
        ).all()
    return result.question_id, {row[0]: row[1] for row in rows}


def answer_request(version: int, *evidence: dict[str, Any], body: str = "Peonies") -> Any:
    return SubmitAnswerRequest.model_validate(
        {"lease_version": version, "body": body, "evidence": list(evidence)}
    )


def inline(category: str = "interest", audience: str = "connections") -> dict[str, Any]:
    return {
        "category": category,
        "proposed_audience": audience,
        "evidence_type": "explicit",
        "observed_month": "2026-09",
    }


def assignment(factory: sessionmaker[Session], assignment_id: UUID) -> Assignment:
    with factory() as s:
        row = s.get(Assignment, assignment_id)
        assert row is not None
        return row


def question(factory: sessionmaker[Session], question_id: UUID) -> Question:
    with factory() as s:
        row = s.get(Question, question_id)
        assert row is not None
        return row


def event_types(factory: sessionmaker[Session], question_id: UUID) -> list[str]:
    with factory() as s:
        return list(
            s.scalars(
                select(QuestionEvent.event_type)
                .where(QuestionEvent.question_id == question_id)
                .order_by(QuestionEvent.occurred_at, QuestionEvent.id)
            )
        )


def count(factory: sessionmaker[Session], model: Any, *where: Any) -> int:
    with factory() as s:
        return s.execute(select(func.count()).select_from(model).where(*where)).scalar_one()


def add_policy(factory: sessionmaker[Session], world: World, version: int, **values: Any) -> None:
    with factory() as s:
        s.add(
            ContributionPolicy(
                profile_id=world.profile.id,
                version=version,
                approved_by_user_id=world.owner.id,
                **values,
            )
        )
        s.commit()


async def answered_question(
    factory: sessionmaker[Session], world: World, *evidence: dict[str, Any], clock: Clock
) -> tuple[UUID, UUID]:
    """A question answered by the owner's first assistant with ``evidence``."""
    question_id, by_delegation = await ask(factory, world)
    assignment_id = by_delegation[world.owner_d1.id]
    d1 = agent_principal(world.owner_d1)
    lease = await claim_assignment(d1, assignment_id, ClaimRequest(), deps=deps(factory, clock))
    await submit_answer(
        d1, assignment_id, answer_request(lease.lease_version, *evidence), deps=deps(factory, clock)
    )
    return question_id, assignment_id


class CheckOutage:
    """Delegates to the real client except checks on ``relations``, which fail."""

    def __init__(self, inner: Any, relations: set[str] | None = None) -> None:
        self._inner = inner
        self._relations = relations
        self.configured = True

    async def check(self, *, user: str, relation: str, object: str) -> bool:
        if self._relations is None or relation in self._relations:
            raise ConnectionError("openfga down")
        return await self._inner.check(user=user, relation=relation, object=object)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# --- Claims and the human-contact lease ---------------------------------------------------------


async def test_concurrent_human_contact_claims_have_exactly_one_winner(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    question_id, by_delegation = await ask(factory, world)
    outcomes: list[Any] = []

    async def claim(delegation: Any) -> None:
        try:
            outcomes.append(
                await claim_assignment(
                    agent_principal(delegation),
                    by_delegation[delegation.id],
                    ClaimRequest(human_contact=True),
                    deps=deps(factory),
                )
            )
        except (HumanContactTaken, AlreadyClaimed, StaleLease) as exc:
            outcomes.append(exc)

    async with anyio.create_task_group() as tg:
        for delegation in (world.owner_d1, world.owner_d2) * 3:
            tg.start_soon(claim, delegation)

    winners = [o for o in outcomes if not isinstance(o, Exception)]
    assert len(winners) == 1
    assert winners[0].human_contact is True and winners[0].lease_version == 1
    with factory() as s:
        held = s.scalars(
            select(Assignment).where(
                Assignment.question_id == question_id, Assignment.human_contact.is_(True)
            )
        ).all()
    assert len(held) == 1
    assert event_types(factory, question_id).count("claimed") == 1
    # The other assistant's attempts lost on the human-contact lease, not on anything else.
    assert any(isinstance(o, HumanContactTaken) for o in outcomes)


async def test_concurrent_claims_of_one_assignment_have_exactly_one_winner(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    question_id, by_delegation = await ask(factory, world)
    assignment_id = by_delegation[world.owner_d1.id]
    wins: list[int] = []
    losses: list[str] = []

    async def claim() -> None:
        try:
            lease = await claim_assignment(
                agent_principal(world.owner_d1), assignment_id, ClaimRequest(), deps=deps(factory)
            )
            wins.append(lease.lease_version)
        except (StaleLease, AlreadyClaimed) as exc:
            losses.append(exc.code)

    async with anyio.create_task_group() as tg:
        for _ in range(6):
            tg.start_soon(claim)

    assert wins == [1]
    assert len(losses) == 5
    row = assignment(factory, assignment_id)
    assert (row.status, row.lease_version) == ("claimed", 1)
    assert event_types(factory, question_id).count("claimed") == 1


async def test_second_human_contact_lease_is_rejected(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    _, by_delegation = await ask(factory, world)
    d1, d2 = agent_principal(world.owner_d1), agent_principal(world.owner_d2)
    a1, a2 = by_delegation[world.owner_d1.id], by_delegation[world.owner_d2.id]

    await claim_assignment(d1, a1, ClaimRequest(human_contact=True), deps=deps(factory))
    with pytest.raises(HumanContactTaken) as exc:
        await claim_assignment(d2, a2, ClaimRequest(human_contact=True), deps=deps(factory))
    assert exc.value.status_code == 409
    # d2 may still work the question without contacting the user...
    lease = await claim_assignment(d2, a2, ClaimRequest(), deps=deps(factory))
    assert lease.human_contact is False
    # ...but cannot report contacting them without a human-contact claim.
    with pytest.raises(HumanContactRequired):
        await report_user_contact(
            d2,
            a2,
            UserContactRequest(lease_version=lease.lease_version, delivery_confirmed=True),
            deps=deps(factory),
        )
    assert assignment(factory, a2).status == "claimed"


async def test_only_the_assignments_own_delegation_may_act(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    _, by_delegation = await ask(factory, world)
    a1 = by_delegation[world.owner_d1.id]
    for delegation in (world.owner_d2, world.requester_d, world.stranger_d):
        with pytest.raises(AssignmentNotFound):
            await claim_assignment(
                agent_principal(delegation), a1, ClaimRequest(), deps=deps(factory)
            )
    # respond is re-checked: the owner's assistant loses respond (tuples revoked) -> denied.
    from revel.auth.authz import delegation_tuples

    await fga.delete_tuples(delegation_tuples(world.owner_d1.user_id, world.owner_d1.id))
    with pytest.raises(QuestionNotPermitted):
        await claim_assignment(
            agent_principal(world.owner_d1), a1, ClaimRequest(), deps=deps(factory)
        )
    assert assignment(factory, a1).status == "available"


async def test_made_available_events_use_the_service_clock(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    # All question events share one clock, so the event log orders correctly against later
    # lease events (and the outreach cooldown that reads occurred_at) under clock skew.
    world = await world_builder()
    question_id, _ = await ask(factory, world)
    with factory() as s:
        stamps = s.scalars(
            select(QuestionEvent.occurred_at).where(
                QuestionEvent.question_id == question_id,
                QuestionEvent.event_type == "made_available",
            )
        ).all()
    assert stamps and set(stamps) == {T0}


# --- The human-contact path (MVP acceptance) ----------------------------------------------------


async def test_human_contact_path_waiting_for_user_then_answer_completes(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    clock = Clock(T0)
    question_id, by_delegation = await ask(factory, world)
    d1 = agent_principal(world.owner_d1)
    a1 = by_delegation[world.owner_d1.id]

    # Claim after ask()'s made_available events: event ids are random, so order by occurred_at.
    clock.advance(timedelta(seconds=1))
    lease = await claim_assignment(
        d1, a1, ClaimRequest(human_contact=True), deps=deps(factory, clock)
    )
    assert lease.prior_user_contacts == 0
    assert lease.lease_expires_at == clock.now + LEASE_DURATION
    clock.advance(timedelta(minutes=5))
    waiting = await report_user_contact(
        d1,
        a1,
        UserContactRequest(lease_version=lease.lease_version, delivery_confirmed=True),
        deps=deps(factory, clock),
    )
    assert waiting.status == "waiting_for_user"
    assert waiting.lease_expires_at == clock.now + HUMAN_RESPONSE_WINDOW
    row = assignment(factory, a1)
    assert row.status == "waiting_for_user"
    assert row.human_contact is True
    assert row.user_contacted_at == clock.now
    assert row.contact_delivery_confirmed is True
    with factory() as s:
        contact = s.scalars(
            select(QuestionEvent).where(
                QuestionEvent.question_id == question_id,
                QuestionEvent.event_type == "user_contact_reported",
            )
        ).one()
    assert contact.actor_delegation_id == world.owner_d1.id
    assert contact.details == {
        "via": "mcp",
        "delivery_confirmed": True,
        "prior_user_contacts": 0,
        "re_report": False,
    }

    # The inbox shows the waiting lease.
    items = (await get_updates(d1, deps=deps(factory, clock))).items
    assert [(i.status, i.lease_version) for i in items] == [("waiting_for_user", 2)]

    # The user answers hours later (inside the response window): the answer completes it.
    clock.advance(timedelta(hours=6))
    result = await submit_answer(
        d1, a1, answer_request(waiting.lease_version, inline()), deps=deps(factory, clock)
    )
    assert (result.question_status, result.assignment_status) == ("answered", "completed")
    assert result.lease_version == 3
    row = assignment(factory, a1)
    assert row.status == "completed" and row.completed_at == clock.now
    assert question(factory, question_id).status == "answered"
    assert [e for e in event_types(factory, question_id) if e != "retrieved"] == [
        "made_available",
        "made_available",
        "claimed",
        "user_contact_reported",
        "answered",
    ]


async def test_human_contact_full_acceptance_stale_completion_and_reassignment(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    """Claim -> waiting_for_user -> (no reply) lease expires -> reassigned to another assistant
    while the question is open -> it asks the user -> the user answers -> complete. The first
    assistant's stale completion is rejected throughout."""
    world = await world_builder()
    clock = Clock(T0)
    question_id, by_delegation = await ask(factory, world)
    d1, d2 = agent_principal(world.owner_d1), agent_principal(world.owner_d2)
    a1, a2 = by_delegation[world.owner_d1.id], by_delegation[world.owner_d2.id]

    lease = await claim_assignment(
        d1, a1, ClaimRequest(human_contact=True), deps=deps(factory, clock)
    )
    waiting = await report_user_contact(
        d1,
        a1,
        UserContactRequest(lease_version=lease.lease_version, delivery_confirmed=False),
        deps=deps(factory, clock),
    )
    # A stale completion (the pre-contact version) is rejected; nothing is written.
    with pytest.raises(StaleLease):
        await submit_answer(
            d1, a1, answer_request(lease.lease_version, inline()), deps=deps(factory, clock)
        )
    assert count(factory, Answer) == 0

    # While d1 waits, d2 cannot take the human-contact lease.
    clock.advance(timedelta(hours=1))
    with pytest.raises(HumanContactTaken):
        await claim_assignment(d2, a2, ClaimRequest(human_contact=True), deps=deps(factory, clock))

    # No reply within the response window: the lease lapses (question still open).
    clock.advance(HUMAN_RESPONSE_WINDOW)
    with pytest.raises(LeaseExpired):
        await submit_answer(
            d1, a1, answer_request(waiting.lease_version, inline()), deps=deps(factory, clock)
        )
    row = assignment(factory, a1)
    assert (row.status, row.lease_version, row.human_contact) == ("available", 3, False)
    assert row.user_contacted_at is not None  # the outreach record is kept
    with factory() as s:
        expired = s.scalars(
            select(QuestionEvent).where(
                QuestionEvent.question_id == question_id,
                QuestionEvent.event_type == "lease_expired",
            )
        ).one()
    assert expired.details["previous_status"] == "waiting_for_user"
    assert expired.details["status"] == "lease_expired"
    assert question(factory, question_id).status == "open"

    # Reassigned: d2 takes the human-contact lease (the cooldown has passed) and is told the
    # user was already asked once.
    lease2 = await claim_assignment(
        d2, a2, ClaimRequest(human_contact=True), deps=deps(factory, clock)
    )
    assert lease2.prior_user_contacts == 1
    waiting2 = await report_user_contact(
        d2,
        a2,
        UserContactRequest(lease_version=lease2.lease_version, delivery_confirmed=True),
        deps=deps(factory, clock),
    )
    result = await submit_answer(
        d2, a2, answer_request(waiting2.lease_version, inline()), deps=deps(factory, clock)
    )
    assert result.question_status == "answered"
    # d1's old version can never complete the answered question.
    with pytest.raises(QuestionClosed):
        await submit_answer(
            d1, a1, answer_request(waiting.lease_version, inline()), deps=deps(factory, clock)
        )
    assert count(factory, Answer, Answer.question_id == question_id) == 1
    status = await get_question_status(
        agent_principal(world.requester_d), question_id, deps=deps(factory, clock)
    )
    assert (status.status, status.answer_available) == ("answered", True)


# --- Stale and expired leases -------------------------------------------------------------------


async def test_stale_lease_version_completion_is_rejected(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    question_id, by_delegation = await ask(factory, world)
    d1, a1 = agent_principal(world.owner_d1), by_delegation[world.owner_d1.id]
    lease = await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory))
    renewed = await renew_assignment(
        d1, a1, LeaseVersionRequest(lease_version=lease.lease_version), deps=deps(factory)
    )
    assert renewed.lease_version == lease.lease_version + 1
    with pytest.raises(StaleLease) as exc:
        await submit_answer(
            d1, a1, answer_request(lease.lease_version, inline()), deps=deps(factory)
        )
    assert exc.value.code == "stale_lease" and exc.value.status_code == 409
    assert count(factory, Answer) == 0
    assert assignment(factory, a1).status == "claimed"
    assert question(factory, question_id).status == "open"
    # Releasing or renewing with the stale version is rejected too.
    for op, req in (
        (renew_assignment, LeaseVersionRequest(lease_version=lease.lease_version)),
        (release_assignment, LeaseVersionRequest(lease_version=lease.lease_version)),
    ):
        with pytest.raises(StaleLease):
            await op(d1, a1, req, deps=deps(factory))


async def test_expired_lease_reassigned_while_open_but_never_after_question_expiry(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    clock = Clock(T0)
    question_id, by_delegation = await ask(factory, world, hours=2)
    d1, d2 = agent_principal(world.owner_d1), agent_principal(world.owner_d2)
    a1, a2 = by_delegation[world.owner_d1.id], by_delegation[world.owner_d2.id]

    await claim_assignment(d1, a1, ClaimRequest(human_contact=True), deps=deps(factory, clock))
    clock.advance(LEASE_DURATION + timedelta(seconds=1))
    # Lazy expiry on read: d1's inbox shows the assignment available again, version bumped.
    items = (await get_updates(d1, deps=deps(factory, clock))).items
    assert [(i.assignment_id, i.status, i.lease_version) for i in items] == [(a1, "available", 2)]
    # The human-contact lease is free: d2 takes it.
    lease2 = await claim_assignment(
        d2, a2, ClaimRequest(human_contact=True), deps=deps(factory, clock)
    )
    assert lease2.status == "claimed"
    # d1 can claim again too (without human contact).
    lease1 = await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory, clock))
    assert lease1.lease_version == 3

    # Past the question deadline: leases lapse but are never revived and the question expires.
    clock.advance(timedelta(hours=3))
    with pytest.raises(QuestionExpired):
        await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory, clock))
    q = question(factory, question_id)
    assert q.status == "expired" and q.closed_at is not None
    for assignment_id in (a1, a2):
        assert assignment(factory, assignment_id).status == "claimed"  # not returned to the pool
    with pytest.raises(QuestionExpired):
        await renew_assignment(
            d2,
            a2,
            LeaseVersionRequest(lease_version=lease2.lease_version),
            deps=deps(factory, clock),
        )
    assert event_types(factory, question_id).count("lease_expired") == 1
    assert (await get_updates(d2, deps=deps(factory, clock))).items == []


async def test_late_submission_is_rejected(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    question_id, by_delegation = await ask(factory, world, hours=1)
    deadline = question(factory, question_id).expires_at
    clock = Clock(deadline - timedelta(minutes=5))
    d1, a1 = agent_principal(world.owner_d1), by_delegation[world.owner_d1.id]
    lease = await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory, clock))
    assert lease.lease_expires_at == deadline  # capped at the question deadline
    clock.now = deadline + timedelta(seconds=1)
    with pytest.raises(QuestionExpired) as exc:
        await submit_answer(
            d1, a1, answer_request(lease.lease_version, inline()), deps=deps(factory, clock)
        )
    assert exc.value.code == "question_expired"
    assert count(factory, Answer) == 0
    assert question(factory, question_id).status == "expired"
    assert assignment(factory, a1).status == "claimed"


@pytest.mark.parametrize("cross_question_deadline", [False, True], ids=["lease", "question"])
async def test_answer_rechecks_expiry_after_evidence_validation(
    cross_question_deadline, factory, fga, world_builder, monkeypatch
):
    world = await world_builder()
    question_id, ids = await ask(factory, world, hours=1)
    respondent = agent_principal(world.owner_d1)
    assignment_id = ids[world.owner_d1.id]
    clock = Clock(T0 + timedelta(minutes=55) if cross_question_deadline else T0)
    d = deps(factory, clock)
    lease = await claim_assignment(respondent, assignment_id, ClaimRequest(), deps=d)
    validate = answers._validate_evidence

    async def validate_then_advance(*args, **kwargs):
        result = await validate(*args, **kwargs)
        clock.advance(LEASE_DURATION)
        return result

    monkeypatch.setattr(answers, "_validate_evidence", validate_then_advance)
    with pytest.raises(QuestionExpired if cross_question_deadline else LeaseExpired):
        await submit_answer(
            respondent, assignment_id, answer_request(lease.lease_version, inline()), deps=d
        )
    assert "answered" not in event_types(factory, question_id)


async def test_contact_claim_rechecks_question_expiry_after_outreach_lock(
    factory, fga, world_builder, monkeypatch
):
    world = await world_builder()
    question_id, ids = await ask(factory, world, hours=1)
    respondent = agent_principal(world.owner_d1)
    assignment_id = ids[world.owner_d1.id]
    clock = Clock(T0 + timedelta(hours=1, seconds=-1))
    lock = question_leases._lock_outreach

    def lock_then_advance(*args):
        lock(*args)
        clock.advance(timedelta(seconds=2))

    monkeypatch.setattr(question_leases, "_lock_outreach", lock_then_advance)
    with pytest.raises(QuestionExpired):
        await claim_assignment(
            respondent, assignment_id, ClaimRequest(human_contact=True), deps=deps(factory, clock)
        )
    assert "claimed" not in event_types(factory, question_id)
    assert assignment(factory, assignment_id).status == "available"


@pytest.mark.parametrize("action", ["renew", "report", "upgrade"])
async def test_active_lease_write_rolls_back_if_old_lease_expires(
    action, factory, fga, world_builder, monkeypatch
):
    world = await world_builder()
    question_id, ids = await ask(factory, world)
    respondent = agent_principal(world.owner_d1)
    assignment_id = ids[world.owner_d1.id]
    clock = Clock(T0)
    d = deps(factory, clock)
    lease = await claim_assignment(
        respondent, assignment_id, ClaimRequest(human_contact=action == "report"), deps=d
    )
    clock.advance(LEASE_DURATION - timedelta(seconds=1))
    update = question_leases._versioned_update

    def update_then_advance(*args, **kwargs):
        result = update(*args, **kwargs)
        clock.advance(timedelta(seconds=2))
        return result

    monkeypatch.setattr(question_leases, "_versioned_update", update_then_advance)
    with pytest.raises(LeaseExpired):
        if action == "renew":
            await renew_assignment(
                respondent,
                assignment_id,
                LeaseVersionRequest(lease_version=lease.lease_version),
                deps=d,
            )
        elif action == "report":
            await report_user_contact(
                respondent,
                assignment_id,
                UserContactRequest(lease_version=lease.lease_version, delivery_confirmed=True),
                deps=d,
            )
        else:
            await claim_assignment(
                respondent,
                assignment_id,
                ClaimRequest(human_contact=True, lease_version=lease.lease_version),
                deps=d,
            )
    unchanged = assignment(factory, assignment_id)
    assert unchanged.status == "claimed"
    assert unchanged.lease_version == lease.lease_version
    assert unchanged.lease_expires_at == lease.lease_expires_at
    assert unchanged.human_contact == lease.human_contact
    events = event_types(factory, question_id)
    assert events.count("claimed") == 1
    assert "renewed" not in events and "user_contact_reported" not in events


@pytest.mark.parametrize("action", ["claim", "upgrade", "renew", "report"])
async def test_lease_write_rolls_back_when_another_assignment_answers(
    action, factory, fga, world_builder, monkeypatch
):
    world = await world_builder()
    question_id, ids = await ask(factory, world)
    first = agent_principal(world.owner_d1)
    second = agent_principal(world.owner_d2)
    first_id = ids[world.owner_d1.id]
    second_id = ids[world.owner_d2.id]
    d = deps(factory)
    first_lease = await claim_assignment(first, first_id, ClaimRequest(), deps=d)
    if action != "claim":
        await claim_assignment(
            second, second_id, ClaimRequest(human_contact=action == "report"), deps=d
        )
    before = assignment(factory, second_id)
    events_before = event_types(factory, question_id)
    update = question_leases._versioned_update

    async def answer():
        await submit_answer(
            first, first_id, answer_request(first_lease.lease_version, inline()), deps=d
        )

    def answer_then_update(session, row, *args, **kwargs):
        if row.id == second_id:
            anyio.from_thread.run(answer)
        return update(session, row, *args, **kwargs)

    monkeypatch.setattr(question_leases, "_versioned_update", answer_then_update)
    with pytest.raises(QuestionClosed):
        if action == "renew":
            await renew_assignment(
                second,
                second_id,
                LeaseVersionRequest(lease_version=before.lease_version),
                deps=d,
            )
        elif action == "report":
            await report_user_contact(
                second,
                second_id,
                UserContactRequest(lease_version=before.lease_version, delivery_confirmed=True),
                deps=d,
            )
        else:
            await claim_assignment(
                second,
                second_id,
                ClaimRequest(
                    human_contact=True,
                    lease_version=before.lease_version if action == "upgrade" else None,
                ),
                deps=d,
            )
    after = assignment(factory, second_id)
    assert (after.status, after.lease_version, after.lease_expires_at, after.human_contact) == (
        before.status,
        before.lease_version,
        before.lease_expires_at,
        before.human_contact,
    )
    assert question(factory, question_id).status == "answered"
    assert sorted(event_types(factory, question_id)) == sorted([*events_before, "answered"])


async def test_renew_on_expired_lease_is_rejected(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    clock = Clock(T0)
    question_id, by_delegation = await ask(factory, world)
    d1, a1 = agent_principal(world.owner_d1), by_delegation[world.owner_d1.id]
    lease = await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory, clock))
    clock.advance(LEASE_DURATION)  # lease_expires_at == now: expired
    with pytest.raises(LeaseExpired) as exc:
        await renew_assignment(
            d1,
            a1,
            LeaseVersionRequest(lease_version=lease.lease_version),
            deps=deps(factory, clock),
        )
    assert exc.value.code == "lease_expired"
    row = assignment(factory, a1)
    assert (row.status, row.lease_version) == ("available", 2)
    # The same (now stale) version stays rejected as expired, not silently renewed.
    with pytest.raises(LeaseExpired):
        await renew_assignment(
            d1,
            a1,
            LeaseVersionRequest(lease_version=lease.lease_version),
            deps=deps(factory, clock),
        )
    assert event_types(factory, question_id).count("renewed") == 0


async def test_renewal_limit(factory: sessionmaker[Session], fga: Any, world_builder: Any) -> None:
    world = await world_builder()
    clock = Clock(T0)
    _, by_delegation = await ask(factory, world)
    d1, a1 = agent_principal(world.owner_d1), by_delegation[world.owner_d1.id]
    version = (
        await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory, clock))
    ).lease_version
    for _ in range(MAX_RENEWALS):
        clock.advance(timedelta(minutes=1))
        version = (
            await renew_assignment(
                d1, a1, LeaseVersionRequest(lease_version=version), deps=deps(factory, clock)
            )
        ).lease_version
    with pytest.raises(RenewalLimitReached):
        await renew_assignment(
            d1, a1, LeaseVersionRequest(lease_version=version), deps=deps(factory, clock)
        )


async def test_outreach_cooldown_is_respected(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    clock = Clock(T0)
    _, by_delegation = await ask(factory, world)
    d1, d2 = agent_principal(world.owner_d1), agent_principal(world.owner_d2)
    a1, a2 = by_delegation[world.owner_d1.id], by_delegation[world.owner_d2.id]

    lease = await claim_assignment(
        d1, a1, ClaimRequest(human_contact=True), deps=deps(factory, clock)
    )
    waiting = await report_user_contact(
        d1,
        a1,
        UserContactRequest(lease_version=lease.lease_version, delivery_confirmed=True),
        deps=deps(factory, clock),
    )
    clock.advance(timedelta(hours=1))
    released = await release_assignment(
        d1, a1, LeaseVersionRequest(lease_version=waiting.lease_version), deps=deps(factory, clock)
    )
    assert released.status == "released" and released.human_contact is False

    # The human-contact lease is free, but d1 contacted the user an hour ago: d2 must wait.
    clock.advance(timedelta(hours=1))
    with pytest.raises(OutreachCooldown) as exc:
        await claim_assignment(d2, a2, ClaimRequest(human_contact=True), deps=deps(factory, clock))
    assert exc.value.retry_after_seconds == 22 * 3600
    lease2 = await claim_assignment(d2, a2, ClaimRequest(), deps=deps(factory, clock))
    with pytest.raises(OutreachCooldown):  # upgrading the plain claim is checked the same way
        await claim_assignment(
            d2,
            a2,
            ClaimRequest(human_contact=True, lease_version=lease2.lease_version),
            deps=deps(factory, clock),
        )
    with pytest.raises(HumanContactRequired):
        await report_user_contact(
            d2,
            a2,
            UserContactRequest(lease_version=lease2.lease_version, delivery_confirmed=True),
            deps=deps(factory, clock),
        )
    assert assignment(factory, a2).status == "claimed"
    # After the cooldown d2 may reach out.
    clock.advance(timedelta(hours=22))
    # (d2's earlier lease lapsed meanwhile; claiming expires it lazily and re-claims.)
    lease2 = await claim_assignment(
        d2, a2, ClaimRequest(human_contact=True), deps=deps(factory, clock)
    )
    assert lease2.prior_user_contacts == 1
    done = await report_user_contact(
        d2,
        a2,
        UserContactRequest(lease_version=lease2.lease_version, delivery_confirmed=True),
        deps=deps(factory, clock),
    )
    assert done.status == "waiting_for_user"


async def test_decline_by_every_respondent_declines_the_question(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    question_id, by_delegation = await ask(factory, world)
    d1, d2 = agent_principal(world.owner_d1), agent_principal(world.owner_d2)
    a1, a2 = by_delegation[world.owner_d1.id], by_delegation[world.owner_d2.id]
    lease = await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory))
    with pytest.raises(StaleLease):
        await decline_assignment(d1, a1, DeclineRequest(lease_version=0), deps=deps(factory))
    await decline_assignment(
        d1,
        a1,
        DeclineRequest(lease_version=lease.lease_version, reason="insufficient_context"),
        deps=deps(factory),
    )
    assert question(factory, question_id).status == "open"
    await decline_assignment(d2, a2, DeclineRequest(), deps=deps(factory))
    q = question(factory, question_id)
    assert q.status == "declined" and q.closed_at is not None
    status = await get_question_status(
        agent_principal(world.requester_d), question_id, deps=deps(factory)
    )
    assert status.status == "declined" and status.answer_available is False
    assert set(status.model_dump()) == {
        "question_id",
        "status",
        "expires_at",
        "created_at",
        "closed_at",
        "answer_available",
    }
    with pytest.raises(QuestionClosed):
        await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory))


# --- Evidence validation ------------------------------------------------------------------------


async def test_evidence_validation(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    question_id, by_delegation = await ask(factory, world)
    d1, a1 = agent_principal(world.owner_d1), by_delegation[world.owner_d1.id]
    lease = await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory))
    with factory() as s:
        other_profile_claim = Claim(
            profile_id=s.execute(
                select(Claim.profile_id).where(Claim.id == world.connections_claim)
            ).scalar_one(),
            category="interest",
            statement="placeholder",
            audience="connections",
            evidence_type="explicit",
            status="active",
            contributor_user_id=world.owner.id,
            contributed_via="ui",
        )
        # Re-home it to the requester's profile: a claim from ANOTHER profile.
        from revel.db.models import Profile

        other_profile_claim.profile_id = s.execute(
            select(Profile.id).where(Profile.user_id == world.requester.id)
        ).scalar_one()
        other_profile_claim.contributor_user_id = world.requester.id
        removed = Claim(
            profile_id=world.profile.id,
            category="interest",
            statement="Removed fact",
            audience="connections",
            evidence_type="explicit",
            status="removed",
            contributor_user_id=world.owner.id,
            contributed_via="ui",
        )
        s.add_all([other_profile_claim, removed])
        s.commit()
        other_id, removed_id = other_profile_claim.id, removed.id

    cases: list[tuple[dict[str, Any], str]] = [
        (inline(category="flowers"), "unknown_category"),
        (inline(category="address"), "forbidden_category"),
        (inline(category="health"), "unknown_category"),
        (inline(audience="public"), "audience_too_open"),  # interest default: connections
        (inline(category="size", audience="connections"), "audience_too_open"),  # trusted
        ({"claim_id": str(other_id)}, "claim_not_usable"),
        ({"claim_id": str(removed_id)}, "claim_not_usable"),
        ({"claim_id": "00000000-0000-0000-0000-000000000000"}, "claim_not_usable"),
    ]
    for item, reason in cases:
        with pytest.raises(InvalidEvidence) as exc:
            await submit_answer(
                d1, a1, answer_request(lease.lease_version, item), deps=deps(factory)
            )
        assert exc.value.status_code == 422 and exc.value.code == "invalid_evidence"
        assert reason in exc.value.message
        assert str(other_id) not in exc.value.message and str(removed_id) not in exc.value.message
    # Mixed: every problem reported by index, nothing written.
    with pytest.raises(InvalidEvidence) as exc:
        await submit_answer(
            d1,
            a1,
            answer_request(
                lease.lease_version, {"claim_id": str(world.connections_claim)}, inline("flowers")
            ),
            deps=deps(factory),
        )
    assert exc.value.message == "evidence[1]: unknown_category"
    with pytest.raises(ValueError):
        answer_request(lease.lease_version)  # at least one evidence item
    with pytest.raises(ValueError):
        SubmitAnswerRequest.model_validate(
            {
                "lease_version": 1,
                "body": "x",
                "evidence": [{**inline(), "quote": "she said so"}],  # no extra fields
            }
        )
    assert count(factory, Answer) == 0 and count(factory, AnswerEvidence) == 0
    assert assignment(factory, a1).status == "claimed"
    assert question(factory, question_id).status == "open"

    # Once the owner opts into Public, a Public interest label is within policy.
    add_policy(
        factory,
        world,
        1,
        public_opt_in=True,
        category_settings={"interest": {"audience": "public"}},
    )
    result = await submit_answer(
        d1, a1, answer_request(lease.lease_version, inline(audience="public")), deps=deps(factory)
    )
    assert result.audience == "public"
    # A finished answer can still be released if its evidence remains Public after revocation.
    # This does not authorize a new question or further respondent actions.
    await fga.write_tuples(public_tuples(world.profile.id))
    await _revoke_requester(factory, world, fga)
    outcome = await get_question(
        agent_principal(world.requester_d), question_id, deps=deps(factory)
    )
    assert outcome.result == "answer"
    assert outcome.answer is not None and outcome.answer.audience == "public"


async def test_answer_audience_is_the_most_restrictive_evidence(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    clock = Clock(T0)
    question_id, _ = await answered_question(
        factory,
        world,
        {"claim_id": str(world.connections_claim)},
        inline("size", "trusted_circle"),
        inline("interest", "connections"),
        clock=clock,
    )
    with factory() as s:
        answer = s.scalars(select(Answer).where(Answer.question_id == question_id)).one()
        evidence = s.scalars(
            select(AnswerEvidence).where(AnswerEvidence.answer_id == answer.id)
        ).all()
    assert answer.audience == "trusted_circle"
    assert len(evidence) == 3
    assert {e.observed_precision for e in evidence if e.claim_id is None} == {"month"}

    requester = agent_principal(world.requester_d)
    # A Connections-tier requester cannot receive a Trusted Circle answer.
    result = await get_answer(requester, question_id, deps=deps(factory, clock))
    assert result.result == "no_shareable_answer" and result.answer is None
    # Promoted to Trusted Circle: released.
    await fga.write_tuples(
        [tier_tuple(world.profile.id, world.requester.id, ConnectionTier.TRUSTED_CIRCLE)]
    )
    result = await get_answer(requester, question_id, deps=deps(factory, clock))
    assert result.result == "answer" and result.answer is not None
    assert result.answer.audience == "trusted_circle"


# --- Retrieval re-checks --------------------------------------------------------------------------


async def _remove_claim(factory: sessionmaker[Session], world: World, fga: Any) -> None:
    with factory() as s:
        s.execute(update(Claim).where(Claim.id == world.connections_claim).values(status="removed"))
        s.commit()


async def _narrow_claim(factory: sessionmaker[Session], world: World, fga: Any) -> None:
    with factory() as s:
        s.execute(
            update(Claim)
            .where(Claim.id == world.connections_claim)
            .values(audience=Audience.TRUSTED_CIRCLE.value)
        )
        s.commit()


async def _revoke_requester(factory: sessionmaker[Session], world: World, fga: Any) -> None:
    await fga.delete_tuples(
        [tier_tuple(world.profile.id, world.requester.id, ConnectionTier.CONNECTIONS)]
    )


@pytest.mark.parametrize("public_profile", [False, True])
async def test_cached_assignment_is_denied_after_requester_revocation(
    public_profile, factory, fga, world_builder
):
    world = await world_builder()
    if public_profile:
        await fga.write_tuples(public_tuples(world.profile.id))
    _, ids = await ask(factory, world)
    respondent = agent_principal(world.owner_d1)
    assignment_id = ids[world.owner_d1.id]
    d = deps(factory)
    assert [item.assignment_id for item in (await get_updates(respondent, deps=d)).items] == [
        assignment_id
    ]
    lease = await claim_assignment(respondent, assignment_id, ClaimRequest(), deps=d)
    await _revoke_requester(factory, world, fga)
    assert (await get_updates(respondent, deps=d)).items == []
    with pytest.raises(QuestionNotPermitted):
        await claim_assignment(
            respondent,
            assignment_id,
            ClaimRequest(human_contact=True, lease_version=lease.lease_version),
            deps=d,
        )
    with pytest.raises(QuestionNotPermitted):
        await submit_answer(
            respondent, assignment_id, answer_request(lease.lease_version, inline()), deps=d
        )


async def test_get_question_keeps_snapshot_when_answer_commits_during_read(
    factory, fga, world_builder, monkeypatch
):
    world = await world_builder()
    question_id, ids = await ask(factory, world)
    respondent = agent_principal(world.owner_d1)
    requester = agent_principal(world.requester_d)
    assignment_id = ids[world.owner_d1.id]
    d = deps(factory)
    lease = await claim_assignment(respondent, assignment_id, ClaimRequest(), deps=d)
    read_question = answers._own_question

    async def answer():
        await submit_answer(
            respondent, assignment_id, answer_request(lease.lease_version, inline()), deps=d
        )

    def read_then_answer(*args):
        result = read_question(*args)
        anyio.from_thread.run(answer)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(answers, "_own_question", read_then_answer)
        before = await get_question(requester, question_id, deps=d)
    assert (before.status, before.answer_available, before.result, before.answer) == (
        "open",
        False,
        "not_answered",
        None,
    )
    assert "answer_retrieved" not in event_types(factory, question_id)
    after = await get_question(requester, question_id, deps=d)
    assert (after.status, after.answer_available, after.result) == ("answered", True, "answer")
    assert after.answer is not None
    assert event_types(factory, question_id).count("answer_retrieved") == 1


async def _tighten_inline_policy(factory: sessionmaker[Session], world: World, fga: Any) -> None:
    add_policy(factory, world, 1, category_settings={"interest": {"audience": "trusted_circle"}})


@pytest.mark.parametrize(
    "change", [_remove_claim, _narrow_claim, _revoke_requester, _tighten_inline_policy]
)
async def test_retrieval_rechecks_remove_access(
    factory: sessionmaker[Session], fga: Any, world_builder: Any, change: Callable[..., Any]
) -> None:
    world = await world_builder()
    clock = Clock(T0)
    question_id, assignment_id = await answered_question(
        factory, world, {"claim_id": str(world.connections_claim)}, inline(), clock=clock
    )
    requester = agent_principal(world.requester_d)
    before = await get_answer(requester, question_id, deps=deps(factory, clock))
    assert before.result == "answer" and before.answer is not None
    assert before.answer.body == "Peonies"
    assert before.answer.audience == "connections"
    assert before.answer.respondent_agent is not None
    assert before.answer.respondent_agent.name == "Muse"
    labels = [e.model_dump() for e in before.answer.evidence]
    assert [(e["kind"], e["category"], e["audience"]) for e in labels] == [
        ("claim", "likes", "connections"),
        ("inline", "interest", "connections"),
    ]
    assert "Likes peonies" not in before.model_dump_json()  # labels only, no claim text

    await change(factory, world, fga)
    after = await get_answer(requester, question_id, deps=deps(factory, clock))
    assert after.model_dump() == {
        "question_id": question_id,
        "question_status": "answered",
        "result": "no_shareable_answer",
        "answer": None,
    }
    # Only the successful retrieval is recorded; nothing else changed.
    assert event_types(factory, question_id).count("answer_retrieved") == 1
    assert question(factory, question_id).status == "answered"
    assert assignment(factory, assignment_id).status == "completed"


async def test_inline_evidence_is_never_broadened(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    clock = Clock(T0)
    question_id, _ = await answered_question(
        factory, world, inline("interest", "trusted_circle"), clock=clock
    )
    requester = agent_principal(world.requester_d)
    assert (await get_answer(requester, question_id, deps=deps(factory, clock))).result == (
        "no_shareable_answer"
    )
    # The owner later opens Interests to Public: the answer keeps its Trusted Circle label.
    add_policy(
        factory,
        world,
        1,
        public_opt_in=True,
        category_settings={"interest": {"audience": "public"}},
    )
    result = await get_answer(requester, question_id, deps=deps(factory, clock))
    assert result.result == "no_shareable_answer"
    await fga.write_tuples(
        [tier_tuple(world.profile.id, world.requester.id, ConnectionTier.TRUSTED_CIRCLE)]
    )
    result = await get_answer(requester, question_id, deps=deps(factory, clock))
    assert result.answer is not None
    assert result.answer.audience == "trusted_circle"
    assert result.answer.evidence[0].audience == "trusted_circle"


async def test_answer_retrievable_after_deadline_without_completing_anything(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    clock = Clock(T0)
    requester = agent_principal(world.requester_d)
    question_id, by_delegation = await ask(factory, world, hours=2)
    d1, a1 = agent_principal(world.owner_d1), by_delegation[world.owner_d1.id]
    lease = await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory, clock))

    # Retrieval before an answer exists: not_answered, and it completes nothing.
    pending = await get_answer(requester, question_id, deps=deps(factory, clock))
    assert (pending.result, pending.question_status, pending.answer) == (
        "not_answered",
        "open",
        None,
    )
    assert assignment(factory, a1).status == "claimed"

    await submit_answer(
        d1, a1, answer_request(lease.lease_version, inline()), deps=deps(factory, clock)
    )
    with factory() as s:
        before = s.execute(
            select(Question.status, Question.closed_at, Question.updated_at).where(
                Question.id == question_id
            )
        ).one()
        lease_before = s.execute(
            select(Assignment.status, Assignment.lease_version).where(Assignment.id == a1)
        ).one()

    clock.advance(timedelta(days=3))  # well past the question deadline
    for _ in range(2):
        result = await get_answer(requester, question_id, deps=deps(factory, clock))
        assert result.result == "answer" and result.question_status == "answered"
    status = await get_question_status(requester, question_id, deps=deps(factory, clock))
    assert status.status == "answered"
    with factory() as s:
        assert (
            s.execute(
                select(Question.status, Question.closed_at, Question.updated_at).where(
                    Question.id == question_id
                )
            ).one()
            == before
        )
        assert (
            s.execute(
                select(Assignment.status, Assignment.lease_version).where(Assignment.id == a1)
            ).one()
            == lease_before
        )
        retrieved = s.scalars(
            select(QuestionEvent).where(
                QuestionEvent.question_id == question_id,
                QuestionEvent.event_type == "answer_retrieved",
            )
        ).all()
    assert len(retrieved) == 2
    assert {(e.actor_user_id, e.actor_delegation_id) for e in retrieved} == {
        (world.requester.id, world.requester_d.id)
    }
    assert event_types(factory, question_id).count("answered") == 1


async def test_only_the_requester_may_read_status_or_answer(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    clock = Clock(T0)
    question_id, _ = await answered_question(factory, world, inline(), clock=clock)
    for delegation in (world.owner_d1, world.stranger_d, world.public_d):
        with pytest.raises(QuestionNotFound):
            await get_answer(agent_principal(delegation), question_id, deps=deps(factory, clock))
        with pytest.raises(QuestionNotFound):
            await get_question_status(
                agent_principal(delegation), question_id, deps=deps(factory, clock)
            )
    assert event_types(factory, question_id).count("answer_retrieved") == 0


# --- Cancelling a question ---------------------------------------------------------------------


async def test_requester_cancels_and_respondents_can_no_longer_act(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    question_id, assignments = await ask(factory, world)
    holder = agent_principal(world.owner_d1)
    lease = await claim_assignment(
        holder, assignments[world.owner_d1.id], ClaimRequest(), deps=deps(factory)
    )

    requester = agent_principal(world.requester_d)
    result = await cancel_question(requester, question_id, deps=deps(factory))
    assert result.status == "cancelled" and result.closed_at == T0
    assert not result.answer_available
    assert question(factory, question_id).status == "cancelled"

    # Repeating changes nothing and records no second event.
    again = await cancel_question(requester, question_id, deps=deps(factory))
    assert again == result
    assert event_types(factory, question_id).count("cancelled") == 1

    with factory() as s:
        event = s.execute(
            select(QuestionEvent).where(
                QuestionEvent.question_id == question_id,
                QuestionEvent.event_type == "cancelled",
            )
        ).scalar_one()
    assert event.actor_delegation_id == world.requester_d.id

    # The inbox drops it, and neither the held lease nor another assistant can continue.
    assert (await get_updates(holder, deps=deps(factory))).items == []
    with pytest.raises(QuestionClosed):
        await submit_answer(
            holder,
            assignments[world.owner_d1.id],
            answer_request(lease.lease_version, inline()),
            deps=deps(factory),
        )
    with pytest.raises(QuestionClosed):
        await claim_assignment(
            agent_principal(world.owner_d2),
            assignments[world.owner_d2.id],
            ClaimRequest(),
            deps=deps(factory),
        )
    assert count(factory, Answer, Answer.question_id == question_id) == 0


async def test_only_the_requester_may_cancel(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    question_id, _ = await ask(factory, world)
    for delegation in (world.owner_d1, world.stranger_d, world.public_d):
        with pytest.raises(QuestionNotFound):
            await cancel_question(agent_principal(delegation), question_id, deps=deps(factory))
    assert question(factory, question_id).status == "open"
    assert "cancelled" not in event_types(factory, question_id)


async def test_answered_or_expired_questions_cannot_be_cancelled(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    requester = agent_principal(world.requester_d)
    clock = Clock(T0)
    answered, _ = await answered_question(factory, world, inline(), clock=clock)
    with pytest.raises(QuestionClosed):
        await cancel_question(requester, answered, deps=deps(factory, clock))
    assert question(factory, answered).status == "answered"

    overdue, _ = await ask(factory, world, hours=1)
    late = Clock(T0 + timedelta(hours=2))
    with pytest.raises(QuestionExpired):
        await cancel_question(requester, overdue, deps=deps(factory, late))
    assert question(factory, overdue).status == "expired"


async def test_cancel_ends_a_held_human_contact_lease(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    question_id, assignments = await ask(factory, world)
    holder = agent_principal(world.owner_d1)
    assignment_id = assignments[world.owner_d1.id]
    lease = await claim_assignment(
        holder, assignment_id, ClaimRequest(human_contact=True), deps=deps(factory)
    )
    waiting = await report_user_contact(
        holder,
        assignment_id,
        UserContactRequest(lease_version=lease.lease_version, delivery_confirmed=True),
        deps=deps(factory),
    )
    assert waiting.status == "waiting_for_user"

    await cancel_question(agent_principal(world.requester_d), question_id, deps=deps(factory))

    version = LeaseVersionRequest(lease_version=waiting.lease_version)
    for call in (
        lambda: renew_assignment(holder, assignment_id, version, deps=deps(factory)),
        lambda: release_assignment(holder, assignment_id, version, deps=deps(factory)),
        lambda: submit_answer(
            holder,
            assignment_id,
            answer_request(waiting.lease_version, inline()),
            deps=deps(factory),
        ),
    ):
        with pytest.raises(QuestionClosed):
            await call()
    # The lease lapsing later revives nothing, and no second assistant can reach the owner.
    later = Clock(T0 + timedelta(days=2))
    assert (await get_updates(holder, deps=deps(factory, later))).items == []
    with pytest.raises(QuestionClosed):
        await claim_assignment(
            agent_principal(world.owner_d2),
            assignments[world.owner_d2.id],
            ClaimRequest(human_contact=True),
            deps=deps(factory, later),
        )
    assert assignment(factory, assignment_id).status == "waiting_for_user"
    assert question(factory, question_id).status == "cancelled"


async def test_requester_may_cancel_after_losing_access(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    question_id, _ = await ask(factory, world)
    await _revoke_requester(factory, world, fga)
    result = await cancel_question(
        agent_principal(world.requester_d), question_id, deps=deps(factory)
    )
    assert result.status == "cancelled"


@pytest.mark.parametrize(
    ("winner", "outcome"),
    [("cancelled", None), ("expired", QuestionExpired), ("answered", QuestionClosed)],
)
async def test_cancel_reports_the_state_a_concurrent_write_left(
    factory: sessionmaker[Session],
    fga: Any,
    world_builder: Any,
    winner: str,
    outcome: type[Exception] | None,
) -> None:
    world = await world_builder()
    question_id, _ = await ask(factory, world)
    closed_at = T0 + timedelta(minutes=1)
    results: list[Any] = []

    async def cancel() -> None:
        try:
            results.append(
                await cancel_question(
                    agent_principal(world.requester_d), question_id, deps=deps(factory)
                )
            )
        except Exception as exc:
            results.append(exc)

    with factory() as blocker:
        blocker.execute(
            update(Question)
            .where(Question.id == question_id)
            .values(status=winner, closed_at=None if winner == "answered" else closed_at)
        )  # Holds the row lock, so the cancel reads it open and waits at its own UPDATE.
        async with anyio.create_task_group() as tg:
            tg.start_soon(cancel)
            await anyio.sleep(0.2)
            blocker.commit()

    assert len(results) == 1
    if outcome is None:
        assert results[0].status == "cancelled" and results[0].closed_at == closed_at
    else:
        assert isinstance(results[0], outcome)
    assert question(factory, question_id).status == winner
    assert "cancelled" not in event_types(factory, question_id)


# --- OpenFGA outages ----------------------------------------------------------------------------


async def test_fga_outage_maps_to_503_never_allow(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    clock = Clock(T0)
    question_id, assignment_id = await answered_question(factory, world, inline(), clock=clock)
    requester = agent_principal(world.requester_d)
    down = CheckOutage(fga)
    with pytest.raises(QuestionServiceUnavailable) as exc:
        await get_answer(requester, question_id, deps=deps(factory, clock, fga=down))
    assert exc.value.status_code == 503
    # Only the tier/visibility checks down (about and read_answer pass): still 503.
    partial = CheckOutage(fga, {"view_only_me", "view_trusted", "view_connections", "view_public"})
    with pytest.raises(QuestionServiceUnavailable):
        await get_answer(requester, question_id, deps=deps(factory, clock, fga=partial))
    assert event_types(factory, question_id).count("answer_retrieved") == 0

    # Lease operations re-check respond: an outage is 503, and nothing is claimed.
    _, by_delegation = await ask(factory, world)
    a1 = by_delegation[world.owner_d1.id]
    with pytest.raises(QuestionServiceUnavailable):
        await claim_assignment(
            agent_principal(world.owner_d1),
            a1,
            ClaimRequest(),
            deps=deps(factory, fga=CheckOutage(fga, {"respond"})),
        )
    assert assignment(factory, a1).status == "available"
    # The get_updates requester re-check is bounded the same way.
    with pytest.raises(QuestionServiceUnavailable):
        await get_updates(
            agent_principal(world.owner_d1),
            deps=deps(factory, fga=CheckOutage(fga, {"view_connections"})),
        )
    assert assignment_id is not None


# --- get_updates requester re-check -------------------------------------------------------------


async def test_get_updates_hides_items_whose_requester_can_no_longer_view(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    await ask(factory, world)
    d1 = agent_principal(world.owner_d1)
    assert len((await get_updates(d1, deps=deps(factory))).items) == 1
    await fga.delete_tuples(
        [tier_tuple(world.profile.id, world.requester.id, ConnectionTier.CONNECTIONS)]
    )
    assert (await get_updates(d1, deps=deps(factory))).items == []
    await fga.write_tuples(
        [tier_tuple(world.profile.id, world.requester.id, ConnectionTier.CONNECTIONS)]
    )
    assert len((await get_updates(d1, deps=deps(factory))).items) == 1


# --- Human contact requires a prior human-contact claim; in-place upgrade -----------------------


async def test_report_without_human_contact_lease_is_refused_and_changes_nothing(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    question_id, by_delegation = await ask(factory, world)
    d1, a1 = agent_principal(world.owner_d1), by_delegation[world.owner_d1.id]
    lease = await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory))
    before = event_types(factory, question_id)
    with pytest.raises(HumanContactRequired) as exc:
        await report_user_contact(
            d1,
            a1,
            UserContactRequest(lease_version=lease.lease_version, delivery_confirmed=True),
            deps=deps(factory),
        )
    assert (exc.value.status_code, exc.value.code) == (409, "human_contact_required")
    row = assignment(factory, a1)
    assert (row.status, row.lease_version, row.human_contact) == ("claimed", 1, False)
    assert row.user_contacted_at is None and row.contact_delivery_confirmed is None
    assert event_types(factory, question_id) == before


async def test_upgrade_claim_to_human_contact_in_place(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    clock = Clock(T0)
    question_id, by_delegation = await ask(factory, world)
    d1, a1 = agent_principal(world.owner_d1), by_delegation[world.owner_d1.id]
    lease = await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory, clock))
    # Without human_contact a held lease stays already_claimed; a stale version is refused.
    with pytest.raises(AlreadyClaimed):
        await claim_assignment(
            d1, a1, ClaimRequest(lease_version=lease.lease_version), deps=deps(factory, clock)
        )
    with pytest.raises(StaleLease):
        await claim_assignment(
            d1, a1, ClaimRequest(human_contact=True, lease_version=0), deps=deps(factory, clock)
        )
    assert assignment(factory, a1).human_contact is False

    clock.advance(timedelta(minutes=5))
    upgraded = await claim_assignment(
        d1,
        a1,
        ClaimRequest(human_contact=True, lease_version=lease.lease_version),
        deps=deps(factory, clock),
    )
    assert (upgraded.status, upgraded.human_contact) == ("claimed", True)
    assert upgraded.lease_version == lease.lease_version + 1
    assert upgraded.lease_expires_at == lease.lease_expires_at  # kept, not extended
    row = assignment(factory, a1)
    assert (row.human_contact, row.lease_expires_at, row.claimed_at) == (
        True,
        lease.lease_expires_at,
        T0,
    )
    with factory() as s:
        claimed = s.scalars(
            select(QuestionEvent)
            .where(QuestionEvent.question_id == question_id, QuestionEvent.event_type == "claimed")
            .order_by(QuestionEvent.lease_version)
        ).all()
    assert [e.details for e in claimed] == [
        {"via": "mcp", "human_contact": False},
        {"via": "mcp", "human_contact": True, "upgrade": True},
    ]
    # Now the user may be asked and the contact reported, then answered.
    waiting = await report_user_contact(
        d1,
        a1,
        UserContactRequest(lease_version=upgraded.lease_version, delivery_confirmed=True),
        deps=deps(factory, clock),
    )
    assert waiting.status == "waiting_for_user"
    result = await submit_answer(
        d1, a1, answer_request(waiting.lease_version, inline()), deps=deps(factory, clock)
    )
    assert result.question_status == "answered"


async def test_upgrade_blocked_by_another_agents_human_contact_lease(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    _, by_delegation = await ask(factory, world)
    d1, d2 = agent_principal(world.owner_d1), agent_principal(world.owner_d2)
    a1, a2 = by_delegation[world.owner_d1.id], by_delegation[world.owner_d2.id]
    await claim_assignment(d1, a1, ClaimRequest(human_contact=True), deps=deps(factory))
    lease2 = await claim_assignment(d2, a2, ClaimRequest(), deps=deps(factory))
    with pytest.raises(HumanContactTaken):
        await claim_assignment(
            d2,
            a2,
            ClaimRequest(human_contact=True, lease_version=lease2.lease_version),
            deps=deps(factory),
        )
    row = assignment(factory, a2)
    assert (row.human_contact, row.lease_version) == (False, lease2.lease_version)
