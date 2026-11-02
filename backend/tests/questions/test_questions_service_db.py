"""submit_question / get_updates against real PostgreSQL and OpenFGA (or F4's fake when
``OPENFGA_TEST_API_URL`` is unreachable). See conftest for requirements."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from questions_support import (
    PURPOSE,
    REQUESTED,
    FakeMuse,
    World,
    agent_principal,
    proposal,
    requires_db,
    set_policy,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from revel.auth.authz import can_respond, delegation_tuples, public_tuples, question_object
from revel.auth.identity import Principal
from revel.db.enums import DelegationStatus
from revel.db.models import AgentDelegation, Assignment, Question, QuestionEvent
from revel.services.questions import (
    MAX_DEADLINE,
    RATE_LIMIT_MAX_QUESTIONS,
    QuestionNotPermitted,
    QuestionRateLimited,
    SubmitQuestionRequest,
    UnsafeRequestedInformationError,
    get_updates,
    submit_question,
)

pytestmark = [pytest.mark.anyio, requires_db]

ITEM_FIELDS = {
    "type",
    "assignment_id",
    "question_id",
    "status",
    "lease_version",
    "minimized_question",
    "requester",
    "acting_agent",
    "about",
    "expires_at",
    "lease_expires_at",
}
SECRET_VALUES = [
    "30th birthday",
    "March 14",
    "$80",
    "peony bouquet",
    "ceramics class voucher",
    "pottery",
    "lake house",
    "escalation-secret-context",
]


def request(**overrides: Any) -> SubmitQuestionRequest:
    data: dict[str, Any] = {
        "handle": "chelsea",
        "requested_information": REQUESTED,
        "private_purpose": PURPOSE,
        "escalation_permitted": True,
        "escalation_context": {"urgency": "high", "notes": "escalation-secret-context"},
    }
    data.update(overrides)
    return SubmitQuestionRequest.model_validate(data)


def assignments(factory: sessionmaker[Session], question_id: Any) -> list[Assignment]:
    with factory() as s:
        return list(s.scalars(select(Assignment).where(Assignment.question_id == question_id)))


def events(factory: sessionmaker[Session], question_id: Any) -> list[QuestionEvent]:
    with factory() as s:
        return list(
            s.scalars(
                select(QuestionEvent)
                .where(QuestionEvent.question_id == question_id)
                .order_by(QuestionEvent.occurred_at)
            )
        )


# --- submit_question: happy path ---------------------------------------------------------------


async def test_submit_model_path_routes_to_owner_active_delegations(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world: World = await world_builder()
    fake = FakeMuse(content=proposal("Which flowers does Chelsea like most?", ["r1", "r2"]))
    deps = world.deps(factory, fake.client())

    result = await submit_question(agent_principal(world.requester_d), request(), deps=deps)

    assert result.status == "open"
    assert result.routing.routing_step == 2
    assert result.routing.respondent_count == 2
    assert result.routing.minimizer == "model"
    assert result.minimized_question == "Which flowers does Chelsea like most?"
    assert not result.deadline_clamped
    # Step 1: the requester's permitted profile context (Connections fact, never Only Me).
    assert result.profile_context.tier == "connections"
    assert {c.id for c in result.profile_context.claims} == {world.connections_claim}
    # No respondent identities reach the requester.
    dumped = result.model_dump_json()
    for d in (world.owner_d1, world.owner_d2):
        assert str(d.id) not in dumped

    rows = assignments(factory, result.question_id)
    assert {a.respondent_delegation_id for a in rows} == {world.owner_d1.id, world.owner_d2.id}
    assert all(a.status == "available" and a.lease_version == 0 for a in rows)
    evs = events(factory, result.question_id)
    assert [e.event_type for e in evs] == ["made_available", "made_available"]
    assert {e.assignment_id for e in evs} == {a.id for a in rows}
    assert all(e.details["routing_path"] == "model" for e in evs)
    assert all(e.actor_delegation_id == world.requester_d.id for e in evs)

    with factory() as s:
        q = s.get(Question, result.question_id)
        assert q is not None
        assert q.routing_step == 2
        assert q.requester_delegation_id == world.requester_d.id
        assert q.minimized_question == result.minimized_question
    # on_question_created wrote the question tuples: the owner's agents may respond.
    assert await can_respond(agent_principal(world.owner_d1), result.question_id)
    assert not await can_respond(agent_principal(world.requester_d), result.question_id)
    assert not await can_respond(agent_principal(world.stranger_d), result.question_id)

    # The model never saw identity data or anything beyond the request and candidate labels.
    assert fake.requests is not None and len(fake.requests) == 1
    sent = json.loads(fake.requests[0].content)
    user = json.loads(sent["messages"][1]["content"])
    assert set(user) == {"requested_information", "private_purpose", "candidates"}
    assert sorted(c["label"] for c in user["candidates"]) == ["Claude", "Muse"]
    assert sorted(c["id"] for c in user["candidates"]) == ["r1", "r2"]
    assert all(set(c) == {"id", "label"} for c in user["candidates"])
    assert sent["max_tokens"] >= 800


async def test_model_may_choose_a_subset(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    fake = FakeMuse(content=proposal("Which flowers does Chelsea like?", ["r2"]))
    result = await submit_question(
        agent_principal(world.requester_d), request(), deps=world.deps(factory, fake.client())
    )
    assert result.routing.respondent_count == 1
    assert fake.requests is not None
    sent = json.loads(json.loads(fake.requests[0].content)["messages"][1]["content"])
    chosen_label = next(c["label"] for c in sent["candidates"] if c["id"] == "r2")
    expected = {"Muse": world.owner_d1.id, "Claude": world.owner_d2.id}[chosen_label]
    assert [a.respondent_delegation_id for a in assignments(factory, result.question_id)] == [
        expected
    ]


async def test_submit_by_profile_id_and_api_via(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    result = await submit_question(
        agent_principal(world.requester_d, via="api"),
        request(handle=None, profile_id=str(world.profile.id)),
        deps=world.deps(factory),
    )
    assert result.routing.minimizer == "fallback"  # no Muse client configured
    assert result.routing.respondent_count == 2


async def test_private_purpose_and_escalation_stored_restricted(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    result = await submit_question(
        agent_principal(world.requester_d), request(), deps=world.deps(factory)
    )
    from sqlalchemy.orm import undefer

    with factory() as s:
        q = s.scalars(
            select(Question)
            .where(Question.id == result.question_id)
            .options(undefer(Question.private_purpose), undefer(Question.escalation_context))
        ).one()
        assert q.private_purpose["candidate_ideas"] == PURPOSE["candidate_ideas"]
        assert q.escalation_permitted is True
        assert q.escalation_context == {"urgency": "high", "notes": "escalation-secret-context"}
        assert q.routing_step == 2  # step 4 is never used in the MVP
    # Neither the response nor any event carries purpose or escalation text.
    blob = result.model_dump_json() + json.dumps(
        [e.details for e in events(factory, result.question_id)]
    )
    for value in SECRET_VALUES:
        assert value not in blob


# --- Permission denials ------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["stranger", "blocked", "owner_self", "unknown_handle", "public"])
async def test_permission_denials_are_identical(
    factory: sessionmaker[Session], fga: Any, world_builder: Any, who: str
) -> None:
    world = await world_builder()
    principal = agent_principal(
        {
            "stranger": world.stranger_d,
            "blocked": world.blocked_d,
            "owner_self": world.owner_d1,
            "unknown_handle": world.requester_d,
            "public": world.public_d,
        }[who]
    )
    req = request(handle="nobody-here") if who == "unknown_handle" else request()
    if who == "public":
        await fga.write_tuples(public_tuples(world.profile.id))
    fake = FakeMuse(content=proposal("x", ["r1"]))
    with pytest.raises(QuestionNotPermitted) as exc:
        await submit_question(principal, req, deps=world.deps(factory, fake.client()))
    assert exc.value.code == "not_permitted"
    assert exc.value.message == QuestionNotPermitted().message
    assert fake.requests == []  # refused before any model call
    with factory() as s:
        assert s.scalars(select(Question)).all() == []


async def test_ui_principal_cannot_submit_or_read_inbox(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    ui = Principal(user_id=world.requester.id, agent_id=None, delegation_id=None, via="ui")
    with pytest.raises(QuestionNotPermitted):
        await submit_question(ui, request(), deps=world.deps(factory))
    with pytest.raises(QuestionNotPermitted):
        await get_updates(
            Principal(user_id=world.owner.id, agent_id=None, delegation_id=None, via="ui"),
            deps=world.deps(factory),
        )


async def test_owner_policy_can_turn_questions_off(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    set_policy(factory, world, questions_enabled=False)
    with pytest.raises(QuestionNotPermitted):
        await submit_question(
            agent_principal(world.requester_d), request(), deps=world.deps(factory)
        )


# --- Rate limit and deadline ------------------------------------------------------------------


async def test_rate_limit(factory: sessionmaker[Session], fga: Any, world_builder: Any) -> None:
    world = await world_builder()
    deps = world.deps(factory)
    principal = agent_principal(world.requester_d)
    for _ in range(RATE_LIMIT_MAX_QUESTIONS):
        await submit_question(principal, request(), deps=deps)
    with pytest.raises(QuestionRateLimited) as exc:
        await submit_question(principal, request(), deps=deps)
    assert exc.value.code == "rate_limited"
    assert 0 < exc.value.retry_after_seconds <= 3600
    with factory() as s:
        assert len(s.scalars(select(Question)).all()) == RATE_LIMIT_MAX_QUESTIONS
        # Older questions fall out of the window.
        s.execute(update(Question).values(created_at=datetime.now(UTC) - timedelta(hours=2)))
        s.commit()
    await submit_question(principal, request(), deps=deps)


async def test_deadline_clamped_on_submit(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    now = datetime.now(UTC)
    result = await submit_question(
        agent_principal(world.requester_d),
        request(expires_in_hours=24 * 60),
        deps=world.deps(factory, now=lambda: now),
    )
    assert result.deadline_clamped
    assert result.expires_at == now + MAX_DEADLINE


# --- get_updates -------------------------------------------------------------------------------


async def test_get_updates_whitelist_and_retrieved_events(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    fake = FakeMuse(content=proposal("Which flowers does Chelsea like most?", ["r1", "r2"]))
    submitted = await submit_question(
        agent_principal(world.requester_d), request(), deps=world.deps(factory, fake.client())
    )

    updates = await get_updates(agent_principal(world.owner_d1), deps=world.deps(factory))
    assert len(updates.items) == 1
    item = updates.items[0]
    raw = item.model_dump(mode="json")
    assert set(raw) == ITEM_FIELDS
    assert raw["type"] == "question_assignment"
    assert raw["question_id"] == str(submitted.question_id)
    assert raw["status"] == "available"
    assert raw["lease_version"] == 0
    assert raw["minimized_question"] == "Which flowers does Chelsea like most?"
    assert raw["requester"] == {"display_name": "Leo Example", "handle": "leo"}
    assert raw["acting_agent"] == {"name": "Muse"}
    assert raw["about"] == {"display_name": "Chelsea Example", "handle": "chelsea"}
    assert raw["lease_expires_at"] is None

    # Never private_purpose, requested_information or escalation_context, in the item or events.
    blob = updates.model_dump_json() + json.dumps(
        [e.details for e in events(factory, submitted.question_id)]
    )
    for value in [*SECRET_VALUES, REQUESTED]:
        assert value not in blob
    retrieved = [e for e in events(factory, submitted.question_id) if e.event_type == "retrieved"]
    assert len(retrieved) == 1
    assert retrieved[0].actor_delegation_id == world.owner_d1.id
    assert retrieved[0].assignment_id == item.assignment_id
    assert retrieved[0].lease_version == 0

    # The second assistant has its own item; the requester and others have none.
    assert (
        len((await get_updates(agent_principal(world.owner_d2), deps=world.deps(factory))).items)
        == 1
    )
    for d in (world.requester_d, world.stranger_d, world.blocked_d):
        assert (await get_updates(agent_principal(d), deps=world.deps(factory))).items == []


async def test_disconnected_delegation_excluded_and_sees_nothing(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    deps = world.deps(factory)
    submitted = await submit_question(agent_principal(world.requester_d), request(), deps=deps)
    assert world.owner_old.id not in {
        a.respondent_delegation_id for a in assignments(factory, submitted.question_id)
    }
    assert (await get_updates(agent_principal(world.owner_old), deps=deps)).items == []

    # Disconnect owner_d2 after routing (F2 marks the row, F4 deletes its tuples): its existing
    # assignment is no longer served because respond is re-checked at read time.
    with factory() as s:
        s.execute(
            update(AgentDelegation)
            .where(AgentDelegation.id == world.owner_d2.id)
            .values(status=DelegationStatus.DISCONNECTED.value, disconnected_at=datetime.now(UTC))
        )
        s.commit()
    await fga.delete_tuples(delegation_tuples(world.owner.id, world.owner_d2.id))
    assert (await get_updates(agent_principal(world.owner_d2), deps=deps)).items == []
    assert len((await get_updates(agent_principal(world.owner_d1), deps=deps)).items) == 1
    retrieved_by = {
        e.actor_delegation_id
        for e in events(factory, submitted.question_id)
        if e.event_type == "retrieved"
    }
    assert retrieved_by == {world.owner_d1.id}


async def test_new_question_skips_disconnected_and_unconfirmed(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    # A delegation row that is active in the DB but has no OpenFGA tuples is not confirmed by
    # question#respond, so it gets no assignment.
    await fga.delete_tuples(delegation_tuples(world.owner.id, world.owner_d2.id))
    result = await submit_question(
        agent_principal(world.requester_d), request(), deps=world.deps(factory)
    )
    assert result.routing.respondent_count == 1
    assert [a.respondent_delegation_id for a in assignments(factory, result.question_id)] == [
        world.owner_d1.id
    ]


async def test_expired_question_marked_once_and_hidden(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    submitted = await submit_question(
        agent_principal(world.requester_d), request(), deps=world.deps(factory)
    )
    later = submitted.expires_at + timedelta(seconds=1)
    deps = world.deps(factory, now=lambda: later)
    assert (await get_updates(agent_principal(world.owner_d1), deps=deps)).items == []
    assert (await get_updates(agent_principal(world.owner_d2), deps=deps)).items == []
    with factory() as s:
        q = s.get(Question, submitted.question_id)
        assert q is not None and q.status == "expired" and q.closed_at == later
    evs = [e for e in events(factory, submitted.question_id) if e.event_type == "expired"]
    assert len(evs) == 1
    assert evs[0].details == {"reason": "deadline_passed", "detected_by": "get_updates"}
    # Not revived for a reader whose clock is earlier.
    assert (
        await get_updates(agent_principal(world.owner_d1), deps=world.deps(factory))
    ).items == []


# --- Adversarial routing -----------------------------------------------------------------------


UNSAFE_MODEL_OUTPUTS = {
    "birthday": "What flowers would Chelsea like for her birthday?",
    "budget": "What flowers around $80 does Chelsea like?",
    "money_word": "Which flowers does Chelsea like that are not expensive?",
    "candidate": "Would Chelsea enjoy a peony bouquet?",
    "candidate_token": "Does Chelsea like peonies?",
    "candidate_2": "Is Chelsea interested in ceramics?",
    "notes_token": "Does Chelsea like pottery?",
    "date": "What flowers does Chelsea like in March?",
    "gift": "Which flowers would make a nice gift for Chelsea?",
    "surprise": "Keep it a surprise: what flowers does Chelsea like?",
    "fake_identity": "Taylor asks which flowers Chelsea likes.",
    "behalf": "On behalf of a friend, which flowers does Chelsea like?",
    "requester_name": "Leo would like to know which flowers Chelsea likes.",
    "agent_name": "Muse here: which flowers does Chelsea like?",
}


@pytest.mark.parametrize("case", sorted(UNSAFE_MODEL_OUTPUTS))
async def test_unsafe_model_output_falls_back(
    factory: sessionmaker[Session], fga: Any, world_builder: Any, case: str
) -> None:
    world = await world_builder()
    fake = FakeMuse(content=proposal(UNSAFE_MODEL_OUTPUTS[case], ["r1"]))
    result = await submit_question(
        agent_principal(world.requester_d), request(), deps=world.deps(factory, fake.client())
    )
    assert result.routing.minimizer == "fallback"
    assert result.minimized_question == REQUESTED
    assert result.routing.respondent_count == 2  # fallback: every eligible candidate
    evs = events(factory, result.question_id)
    assert {e.details["fallback_reason"] for e in evs} == {"unsafe_question"}
    item = (await get_updates(agent_principal(world.owner_d1), deps=world.deps(factory))).items[0]
    assert item.minimized_question == REQUESTED
    # Identity always comes from the authenticated requester, never from model text.
    assert item.requester.display_name == "Leo Example"


INVALID_MODEL_RESPONSES: dict[str, tuple[dict[str, Any], str]] = {
    "out_of_set": (
        {"content": proposal("Which flowers does she like?", ["r9"])},
        "invalid_respondents",
    ),
    "mixed_set": (
        {"content": proposal("Which flowers does she like?", ["r1", "owner"])},
        "invalid_respondents",
    ),
    "empty_ids": ({"content": proposal("Which flowers does she like?", [])}, "invalid_respondents"),
    "raw_uuid": (
        {
            "content": proposal(
                "Which flowers does she like?", ["00000000-0000-0000-0000-000000000001"]
            )
        },
        "invalid_respondents",
    ),
    "empty_question": ({"content": proposal("   ", ["r1"])}, "invalid_question"),
    "too_long": ({"content": proposal("flowers? " * 100, ["r1"])}, "invalid_question"),
    "non_json": ({"content": "Sure! I'd ask about flowers."}, "malformed"),
    "json_list": ({"content": "[1, 2]"}, "malformed"),
    "null_content": ({"content": None}, "malformed"),
    "http_500": ({"status": 500, "content": "x"}, "http_error"),
    "http_429": ({"status": 429, "content": "x"}, "http_error"),
    "transport": ({"exc": httpx.ConnectError("boom")}, "http_error"),
    "timeout": ({"delay": 1.0, "content": proposal("Which flowers?", ["r1"])}, "timeout"),
}


@pytest.mark.parametrize("case", sorted(INVALID_MODEL_RESPONSES))
async def test_invalid_model_response_falls_back(
    factory: sessionmaker[Session], fga: Any, world_builder: Any, case: str
) -> None:
    world = await world_builder()
    kwargs, reason = INVALID_MODEL_RESPONSES[case]
    fake = FakeMuse(**kwargs)
    result = await submit_question(
        agent_principal(world.requester_d),
        request(),
        deps=world.deps(factory, fake.client(timeout=0.2)),
    )
    assert result.routing.minimizer == "fallback"
    assert result.minimized_question == REQUESTED
    assert {a.respondent_delegation_id for a in assignments(factory, result.question_id)} == {
        world.owner_d1.id,
        world.owner_d2.id,
    }
    assert {e.details["fallback_reason"] for e in events(factory, result.question_id)} == {reason}


async def test_code_fenced_model_output_accepted(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    fake = FakeMuse(
        content="```json\n" + proposal("Which flowers does she like best?", ["r1"]) + "\n```"
    )
    result = await submit_question(
        agent_principal(world.requester_d), request(), deps=world.deps(factory, fake.client())
    )
    assert result.routing.minimizer == "model"
    assert result.minimized_question == "Which flowers does she like best?"


async def test_unavailable_provider_uses_fallback(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    fake = FakeMuse(content=proposal("Which flowers?", ["r1"]))
    result = await submit_question(
        agent_principal(world.requester_d),
        request(),
        deps=world.deps(factory, fake.client(provider="meta")),
    )
    assert result.routing.minimizer == "fallback"
    assert fake.requests == []
    assert {e.details["fallback_reason"] for e in events(factory, result.question_id)} == {
        "unavailable"
    }


@pytest.mark.parametrize(
    "text",
    [
        "What does Chelsea want for her birthday?",
        "Would Chelsea like a peony bouquet for her 30th birthday?",
        "Is the lake house a good place for her?",
        "What can I get her for under $80?",
        "Any ideas for March 14?",
        "What gift would she like?",
    ],
)
async def test_spoiler_in_requested_information_rejected_without_echo(
    factory: sessionmaker[Session], fga: Any, world_builder: Any, text: str
) -> None:
    world = await world_builder()
    fake = FakeMuse(content=proposal("x", ["r1"]))
    with pytest.raises(UnsafeRequestedInformationError) as exc:
        await submit_question(
            agent_principal(world.requester_d),
            request(requested_information=text),
            deps=world.deps(factory, fake.client()),
        )
    assert exc.value.code == "unsafe_requested_information"
    assert "private_purpose" in exc.value.message
    for value in [*SECRET_VALUES, "30th", "lake"]:
        assert value.lower() not in exc.value.message.lower()
    assert fake.requests == []
    with factory() as s:
        assert s.scalars(select(Question)).all() == []


async def test_no_candidates_still_creates_question(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    with factory() as s:
        s.execute(
            update(AgentDelegation)
            .where(AgentDelegation.user_id == world.owner.id)
            .values(status=DelegationStatus.DISCONNECTED.value, disconnected_at=datetime.now(UTC))
        )
        s.commit()
    fake = FakeMuse(content=proposal("x", ["r1"]))
    result = await submit_question(
        agent_principal(world.requester_d), request(), deps=world.deps(factory, fake.client())
    )
    assert result.routing.respondent_count == 0
    assert fake.requests == []
    evs = events(factory, result.question_id)
    assert len(evs) == 1 and evs[0].assignment_id is None
    assert evs[0].details["fallback_reason"] == "no_candidates"
    assert evs[0].details["respondent_count"] == 0


async def test_question_tuples_written(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    result = await submit_question(
        agent_principal(world.requester_d), request(), deps=world.deps(factory)
    )
    assert await fga.check(
        user=f"user:{world.requester.id}",
        relation="requester",
        object=question_object(result.question_id),
    )
