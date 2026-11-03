"""Lease races, repeated user-contact reports and concurrent declines (#20 review follow-ups
O1, O2, O4) against real PostgreSQL and OpenFGA."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

import anyio
import pytest
from questions_support import agent_principal, requires_db
from sqlalchemy.orm import Session, sessionmaker
from test_leases_answers_db import (
    T0,
    Clock,
    ask,
    assignment,
    deps,
    event_types,
    question,
)

from revel.services import question_leases
from revel.services.question_leases import (
    HUMAN_RESPONSE_WINDOW,
    MAX_RENEWALS,
    ClaimRequest,
    DeclineRequest,
    HumanContactRequired,
    LeaseVersionRequest,
    OutreachCooldown,
    RenewalLimitReached,
    StaleLease,
    UserContactRequest,
    claim_assignment,
    decline_assignment,
    release_assignment,
    renew_assignment,
    report_user_contact,
)

pytestmark = [pytest.mark.anyio, requires_db]


# --- O1: two operations race on the same lease_version ------------------------------------------


@pytest.mark.parametrize("second", ["renew", "report_user_contact"])
async def test_same_lease_version_race_has_exactly_one_winner(
    factory: sessionmaker[Session],
    fga: Any,
    world_builder: Any,
    monkeypatch: pytest.MonkeyPatch,
    second: Any,
) -> None:
    """Both operations pass the read-side version check (a barrier holds each until the other
    has read too), so ONLY the ``UPDATE ... WHERE lease_version = :v`` guard can pick the
    winner: without it both would succeed (both leave the lease held, so no status guard
    decides either; verified by removing the guard)."""
    world = await world_builder()
    _, by_delegation = await ask(factory, world)
    d1, a1 = agent_principal(world.owner_d1), by_delegation[world.owner_d1.id]
    lease = await claim_assignment(d1, a1, ClaimRequest(human_contact=True), deps=deps(factory))
    ops = {
        "renew": (renew_assignment, LeaseVersionRequest(lease_version=lease.lease_version)),
        "report_user_contact": (
            report_user_contact,
            UserContactRequest(lease_version=lease.lease_version, delivery_confirmed=True),
        ),
    }

    barrier = threading.Barrier(2, timeout=10)
    original = question_leases.held_lease

    def held_then_wait(*args: Any, **kwargs: Any) -> Any:
        row = original(*args, **kwargs)
        barrier.wait()
        return row

    monkeypatch.setattr(question_leases, "held_lease", held_then_wait)
    outcomes: list[Any] = []

    async def run(name: str) -> None:
        op, request = ops[name]
        try:
            outcomes.append(await op(d1, a1, request, deps=deps(factory)))
        except StaleLease as exc:
            outcomes.append(exc)

    async with anyio.create_task_group() as tg:
        tg.start_soon(run, "renew")
        tg.start_soon(run, second)

    winners = [o for o in outcomes if not isinstance(o, Exception)]
    losers = [o for o in outcomes if isinstance(o, StaleLease)]
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0].code == "stale_lease"
    assert assignment(factory, a1).lease_version == lease.lease_version + 1


# --- O2: repeated report_user_contact -----------------------------------------------------------


async def test_repeated_contact_report_does_not_extend_window_and_is_limited(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    clock = Clock(T0)
    question_id, by_delegation = await ask(factory, world)
    d1, a1 = agent_principal(world.owner_d1), by_delegation[world.owner_d1.id]
    lease = await claim_assignment(
        d1, a1, ClaimRequest(human_contact=True), deps=deps(factory, clock)
    )
    first = await report_user_contact(
        d1,
        a1,
        UserContactRequest(lease_version=lease.lease_version, delivery_confirmed=False),
        deps=deps(factory, clock),
    )
    window_end = first.lease_expires_at
    assert window_end == T0 + HUMAN_RESPONSE_WINDOW
    version = first.lease_version
    for _ in range(MAX_RENEWALS):
        clock.advance(timedelta(minutes=30))
        again = await report_user_contact(
            d1,
            a1,
            UserContactRequest(lease_version=version, delivery_confirmed=True),
            deps=deps(factory, clock),
        )
        # Re-confirmation: same window, same contact time, delivery updated.
        assert again.status == "waiting_for_user"
        assert again.lease_expires_at == window_end
        assert again.prior_user_contacts == 1
        version = again.lease_version
    row = assignment(factory, a1)
    assert row.user_contacted_at == T0 and row.lease_expires_at == window_end
    assert row.contact_delivery_confirmed is True
    with pytest.raises(RenewalLimitReached):
        await report_user_contact(
            d1,
            a1,
            UserContactRequest(lease_version=version, delivery_confirmed=True),
            deps=deps(factory, clock),
        )
    # Re-reports and renewals share the limit.
    with pytest.raises(RenewalLimitReached):
        await renew_assignment(
            d1, a1, LeaseVersionRequest(lease_version=version), deps=deps(factory, clock)
        )
    assert event_types(factory, question_id).count("user_contact_reported") == 1 + MAX_RENEWALS
    # The window still ends when it did: the lease lapses on schedule.
    clock.now = window_end
    with pytest.raises(question_leases.LeaseExpired):
        await report_user_contact(
            d1,
            a1,
            UserContactRequest(lease_version=version, delivery_confirmed=True),
            deps=deps(factory, clock),
        )


async def test_outreach_cooldown_applies_to_the_same_assistant(
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
    # A re-confirmation an hour later is not new outreach and does not move the cooldown.
    clock.advance(timedelta(hours=1))
    waiting = await report_user_contact(
        d1,
        a1,
        UserContactRequest(lease_version=waiting.lease_version, delivery_confirmed=True),
        deps=deps(factory, clock),
    )
    released = await release_assignment(
        d1, a1, LeaseVersionRequest(lease_version=waiting.lease_version), deps=deps(factory, clock)
    )
    # d1 itself may not start new outreach within the cooldown of its own contact...
    with pytest.raises(OutreachCooldown) as exc:
        await claim_assignment(d1, a1, ClaimRequest(human_contact=True), deps=deps(factory, clock))
    assert exc.value.retry_after_seconds == 23 * 3600  # from the first contact, not the re-report
    relet = await claim_assignment(d1, a1, ClaimRequest(), deps=deps(factory, clock))
    with pytest.raises(OutreachCooldown):  # nor upgrade its plain claim
        await claim_assignment(
            d1,
            a1,
            ClaimRequest(human_contact=True, lease_version=relet.lease_version),
            deps=deps(factory, clock),
        )
    assert assignment(factory, a1).human_contact is False
    with pytest.raises(HumanContactRequired):
        await report_user_contact(
            d1,
            a1,
            UserContactRequest(lease_version=relet.lease_version, delivery_confirmed=True),
            deps=deps(factory, clock),
        )
    assert released.status == "released"
    # ...and neither may another assistant; after the cooldown either may.
    with pytest.raises(OutreachCooldown):
        await claim_assignment(d2, a2, ClaimRequest(human_contact=True), deps=deps(factory, clock))
    clock.now = T0 + timedelta(hours=24)
    lease2 = await claim_assignment(
        d2, a2, ClaimRequest(human_contact=True), deps=deps(factory, clock)
    )
    assert lease2.prior_user_contacts == 1  # re-confirmations are not counted as contacts


# --- O4: concurrent last declines close the question ---------------------------------------------


async def test_concurrent_last_declines_close_the_question(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    for _ in range(3):
        world = await world_builder()
        question_id, by_delegation = await ask(factory, world)

        async def decline(delegation: Any, assignment_id: Any) -> None:
            await decline_assignment(
                agent_principal(delegation), assignment_id, DeclineRequest(), deps=deps(factory)
            )

        async with anyio.create_task_group() as tg:
            for delegation in (world.owner_d1, world.owner_d2):
                tg.start_soon(decline, delegation, by_delegation[delegation.id])
        q = question(factory, question_id)
        assert q.status == "declined" and q.closed_at is not None
        assert event_types(factory, question_id).count("declined") == 3  # 2 assignments + question
        # Fresh world for the next round.
        with factory() as s:
            from sqlalchemy import text

            s.execute(
                text(
                    "TRUNCATE answer_evidence, answers, question_events, assignments, questions, "
                    "connection_requests, handles, claim_events, claims, contribution_policies, "
                    "profiles, agent_delegations, agents, users CASCADE"
                )
            )
            s.commit()
