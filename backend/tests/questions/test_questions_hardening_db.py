"""Review fixes for RG-36 against real PostgreSQL and OpenFGA (or the fake; see conftest):
the binoculars demo question (B1), identity claims in requested_information (B2), concurrency of
the rate limit and lazy expiry (O1), the write-transaction permission re-check (O2), OpenFGA
outages while confirming respondents (O3) and assistant labels for unnamed (DCR) agents."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
from questions_support import (
    REQUESTED,
    FakeMuse,
    World,
    agent_principal,
    proposal,
    requires_db,
    set_policy,
)
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from revel.auth.authz import blocked_tuple
from revel.db.models import Agent, Assignment, Question, QuestionEvent
from revel.services.questions import (
    RATE_LIMIT_MAX_QUESTIONS,
    QuestionNotPermitted,
    QuestionRateLimited,
    QuestionServiceUnavailable,
    SubmitQuestionRequest,
    UnsafeRequestedInformationError,
    get_updates,
    submit_question,
)

pytestmark = [pytest.mark.anyio, requires_db]

# The MVP demo (#22): Sam's assistant asks whether Alex already owns binoculars.
DEMO_REQUESTED = "Does Alex already own binoculars?"
DEMO_PURPOSE = {
    "occasion": "birthday",
    "occasion_date": "next week",
    "budget": "$100",
    "candidate_ideas": ["compact binoculars", "bird field guide"],
}
DEMO_FORBIDDEN = ["birthday", "next week", "$100", "100", "compact", "bird", "field guide"]


def demo_request(**overrides: Any) -> SubmitQuestionRequest:
    data: dict[str, Any] = {
        "handle": "chelsea",
        "requested_information": DEMO_REQUESTED,
        "private_purpose": DEMO_PURPOSE,
    }
    data.update(overrides)
    return SubmitQuestionRequest.model_validate(data)


def count(factory: sessionmaker[Session], model: Any, *where: Any) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where)) or 0


def assert_demo_safe(text: str) -> None:
    assert "binoculars" in text
    for term in DEMO_FORBIDDEN:
        assert term not in text.lower()


# --- B1: the demo question ---------------------------------------------------------------------


async def test_demo_model_path(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world: World = await world_builder()
    fake = FakeMuse(content=proposal("Does Alex already own binoculars?", ["r1", "r2"]))
    result = await submit_question(
        agent_principal(world.requester_d), demo_request(), deps=world.deps(factory, fake.client())
    )
    assert result.routing.minimizer == "model"
    assert result.routing.respondent_count == 2
    assert_demo_safe(result.minimized_question)
    item = (await get_updates(agent_principal(world.owner_d1), deps=world.deps(factory))).items[0]
    assert_demo_safe(item.minimized_question)


async def test_demo_fallback_path(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    result = await submit_question(
        agent_principal(world.requester_d), demo_request(), deps=world.deps(factory)
    )
    assert result.routing.minimizer == "fallback"
    assert result.minimized_question == DEMO_REQUESTED
    assert_demo_safe(result.minimized_question)


@pytest.mark.parametrize(
    "model_text",
    [
        "Does Alex already own compact binoculars?",
        "Does Alex already own binoculars or a bird field guide?",
        "Does Alex already own binoculars for his birthday?",
        "Does Alex already own binoculars? Asking before next week.",
        "Does Alex own binoculars under $100?",
    ],
)
async def test_demo_fake_model_adding_terms_falls_back(
    factory: sessionmaker[Session], fga: Any, world_builder: Any, model_text: str
) -> None:
    world = await world_builder()
    fake = FakeMuse(content=proposal(model_text, ["r1"]))
    result = await submit_question(
        agent_principal(world.requester_d), demo_request(), deps=world.deps(factory, fake.client())
    )
    assert result.routing.minimizer == "fallback"
    assert result.minimized_question == DEMO_REQUESTED
    with factory() as s:
        reasons = {
            e.details["fallback_reason"]
            for e in s.scalars(
                select(QuestionEvent).where(QuestionEvent.question_id == result.question_id)
            )
        }
    assert reasons == {"unsafe_question"}


async def test_demo_requested_information_with_birthday_rejected(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    with pytest.raises(UnsafeRequestedInformationError):
        await submit_question(
            agent_principal(world.requester_d),
            demo_request(requested_information="Does Alex own binoculars for his birthday?"),
            deps=world.deps(factory),
        )
    assert count(factory, Question) == 0


# --- B2: identity claims in requested_information ---------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Leo, through his assistant, asks whether Chelsea likes hiking.",
        "Chelsea's boss is asking: does Chelsea like hiking?",
        "On behalf of a friend: does Chelsea like hiking?",
        "Does Chelsea like hiking? Leo Example wants to check.",  # requester's display name
        "Does Chelsea like hiking? From leo",  # requester's handle
        "Does Chelsea like hiking? Sent by Muse.",  # acting assistant's name
    ],
)
async def test_identity_claims_in_requested_information_rejected(
    factory: sessionmaker[Session], fga: Any, world_builder: Any, text: str
) -> None:
    world = await world_builder()
    fake = FakeMuse(content=proposal("Does Chelsea like hiking?", ["r1"]))
    with pytest.raises(UnsafeRequestedInformationError) as exc:
        await submit_question(
            agent_principal(world.requester_d),
            demo_request(requested_information=text, private_purpose={}),
            deps=world.deps(factory, fake.client()),
        )
    assert exc.value.code == "unsafe_requested_information"
    assert "Leo" not in exc.value.message and "Muse" not in exc.value.message
    assert fake.requests == []  # rejected before any model call
    assert count(factory, Question) == 0


async def test_voice_assistant_question_allowed_on_both_paths(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    text = "Does Chelsea use a voice assistant?"
    fallback = await submit_question(
        agent_principal(world.requester_d),
        demo_request(requested_information=text, private_purpose={}),
        deps=world.deps(factory),
    )
    assert fallback.minimized_question == text
    fake = FakeMuse(content=proposal("Which voice assistant does Chelsea use?", ["r1"]))
    model = await submit_question(
        agent_principal(world.requester_d),
        demo_request(requested_information=text, private_purpose={}),
        deps=world.deps(factory, fake.client()),
    )
    assert model.routing.minimizer == "model"


async def test_owner_sharing_requester_first_name_can_be_named(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    # Two people called Leo: the owner's own first name is not treated as the requester's.
    from revel.db.models import User

    with factory() as s:
        s.execute(update(User).where(User.id == world.owner.id).values(display_name="Leo Owner"))
        s.commit()
    result = await submit_question(
        agent_principal(world.requester_d),
        demo_request(requested_information="Does Leo like hiking?", private_purpose={}),
        deps=world.deps(factory),
    )
    assert result.minimized_question == "Does Leo like hiking?"


# --- O1: concurrency ---------------------------------------------------------------------------


async def _seed_questions(factory: sessionmaker[Session], world: World, n: int) -> None:
    with factory() as s:
        for _ in range(n):
            s.add(
                Question(
                    profile_id=world.profile.id,
                    requester_user_id=world.requester.id,
                    requester_delegation_id=world.requester_d.id,
                    requested_information=REQUESTED,
                    minimized_question=REQUESTED,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    routing_step=2,
                )
            )
        s.commit()


async def test_rate_limit_holds_under_concurrent_submits(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    existing, concurrent = RATE_LIMIT_MAX_QUESTIONS - 2, 6
    await _seed_questions(factory, world, existing)
    deps = world.deps(factory)
    outcomes: list[str] = []

    async def one() -> None:
        try:
            await submit_question(agent_principal(world.requester_d), demo_request(), deps=deps)
            outcomes.append("ok")
        except QuestionRateLimited:
            outcomes.append("limited")

    async with anyio.create_task_group() as tg:
        for _ in range(concurrent):
            tg.start_soon(one)
    assert sorted(outcomes) == ["limited"] * 4 + ["ok"] * 2
    assert count(factory, Question) == RATE_LIMIT_MAX_QUESTIONS


async def test_concurrent_get_updates_expire_once(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    submitted = await submit_question(
        agent_principal(world.requester_d), demo_request(), deps=world.deps(factory)
    )
    later = submitted.expires_at + timedelta(seconds=1)

    class SlowInboxSession(Session):
        """Pauses after each session's first SELECT (the inbox read) so concurrent readers all
        see the question open before any of them expires it: the guarded UPDATE must then
        write the expired event exactly once."""

        _paused = False

        def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = super().execute(statement, *args, **kwargs)
            if not self._paused and statement.is_select:
                self._paused = True
                time.sleep(0.3)
            return result

    slow = sessionmaker(bind=factory.kw["bind"], class_=SlowInboxSession, expire_on_commit=False)
    deps = world.deps(slow, now=lambda: later)
    results: list[int] = []

    async def one(delegation: Any) -> None:
        results.append(len((await get_updates(agent_principal(delegation), deps=deps)).items))

    async with anyio.create_task_group() as tg:
        for _ in range(4):
            tg.start_soon(one, world.owner_d1)
            tg.start_soon(one, world.owner_d2)
    assert results == [0] * 8
    expired = count(
        factory,
        QuestionEvent,
        QuestionEvent.question_id == submitted.question_id,
        QuestionEvent.event_type == "expired",
    )
    assert expired == 1


# --- O2: permission re-checked in the write transaction ---------------------------------------


async def test_requester_blocked_during_model_call(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()

    async def block() -> None:
        await fga.write_tuples([blocked_tuple(world.profile.id, world.requester.id)])

    fake = FakeMuse(content=proposal(DEMO_REQUESTED, ["r1"]), on_request=block)
    with pytest.raises(QuestionNotPermitted):
        await submit_question(
            agent_principal(world.requester_d),
            demo_request(),
            deps=world.deps(factory, fake.client()),
        )
    assert count(factory, Question) == 0
    assert count(factory, Assignment) == 0


async def test_questions_disabled_during_model_call(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()

    async def disable() -> None:
        await anyio.to_thread.run_sync(lambda: set_policy(factory, world, questions_enabled=False))

    fake = FakeMuse(content=proposal(DEMO_REQUESTED, ["r1"]), on_request=disable)
    with pytest.raises(QuestionNotPermitted):
        await submit_question(
            agent_principal(world.requester_d),
            demo_request(),
            deps=world.deps(factory, fake.client()),
        )
    assert count(factory, Question) == 0


# --- O3: an OpenFGA outage is not a denial ---------------------------------------------------


class RespondOutage:
    """Delegates to the real client except ``respond`` checks, which fail."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.configured = True

    async def check(self, *, user: str, relation: str, object: str) -> bool:
        if relation == "respond":
            raise ConnectionError("openfga down")
        return await self._inner.check(user=user, relation=relation, object=object)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_respond_check_outage_rolls_back(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    with pytest.raises(QuestionServiceUnavailable) as exc:
        await submit_question(
            agent_principal(world.requester_d),
            demo_request(),
            deps=world.deps(factory, fga=RespondOutage(fga)),
        )
    assert exc.value.status_code == 503
    assert count(factory, Question) == 0
    assert count(factory, QuestionEvent) == 0


# --- Unnamed (DCR) assistants ------------------------------------------------------------------


async def test_unnamed_agent_label_in_get_updates(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    with factory() as s:
        s.execute(
            update(Agent)
            .where(Agent.id == world.requester_d.agent_id)
            .values(name=None, oauth_client_id="https://muse.example/oauth/client.json")
        )
        s.commit()
    fake = FakeMuse(content=proposal(DEMO_REQUESTED, ["r1", "r2"]))
    await submit_question(
        agent_principal(world.requester_d), demo_request(), deps=world.deps(factory, fake.client())
    )
    item = (await get_updates(agent_principal(world.owner_d1), deps=world.deps(factory))).items[0]
    assert item.acting_agent is not None and item.acting_agent.name == "muse.example"
    # Owner candidates with no name get a label too (here the requester shares the owner's
    # client, so the owner's Muse delegation is labelled by host as well).
    sent = json.loads(json.loads(fake.requests[0].content)["messages"][1]["content"])  # type: ignore[index]
    assert {c["label"] for c in sent["candidates"]} == {"muse.example", "Claude"}

    with factory() as s:
        s.execute(
            update(Agent)
            .where(Agent.id == world.requester_d.agent_id)
            .values(oauth_client_id="client_opaque_id")
        )
        s.commit()
    item = (await get_updates(agent_principal(world.owner_d1), deps=world.deps(factory))).items[0]
    assert item.acting_agent is not None and item.acting_agent.name == "Assistant"


async def test_get_updates_respond_outage_is_unavailable_not_empty(
    factory: sessionmaker[Session], fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    await submit_question(
        agent_principal(world.requester_d), demo_request(), deps=world.deps(factory)
    )
    with pytest.raises(QuestionServiceUnavailable):
        await get_updates(
            agent_principal(world.owner_d1), deps=world.deps(factory, fga=RespondOutage(fga))
        )
    assert count(factory, QuestionEvent, QuestionEvent.event_type == "retrieved") == 0
    # Recovered: the item is served.
    updates = await get_updates(agent_principal(world.owner_d1), deps=world.deps(factory))
    assert len(updates.items) == 1


async def test_respond_check_timeout_is_unavailable(
    factory: sessionmaker[Session], fga: Any, world_builder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from revel.auth import authz

    world = await world_builder()

    class SlowRespond(RespondOutage):
        async def check(self, *, user: str, relation: str, object: str) -> bool:
            if relation == "respond":
                await anyio.sleep(1)
            return await self._inner.check(user=user, relation=relation, object=object)

    monkeypatch.setattr(authz, "CHECK_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(QuestionServiceUnavailable):
        await submit_question(
            agent_principal(world.requester_d),
            demo_request(),
            deps=world.deps(factory, fga=SlowRespond(fga)),
        )
    assert count(factory, Question) == 0
