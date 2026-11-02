"""Pure unit tests (no database, no network): spoiler guard, proposal validation, deadline,
request schemas and the Muse Spark client with a mock transport."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from questions_support import PURPOSE, REQUESTED, FakeMuse, completion, proposal

from revel.services import questions
from revel.services.muse_spark import (
    MuseSparkClient,
    MuseSparkHTTPError,
    MuseSparkMalformed,
    MuseSparkTimeout,
    MuseSparkUnavailable,
    parse_json_object,
)
from revel.services.question_routing import (
    Candidate,
    FallbackReason,
    PrivatePurpose,
    SpoilerCategory,
    UnsafeRequestedInformation,
    publishable_question_problems,
    route_question,
    validate_proposal,
    validate_requested_information,
)
from revel.services.questions import (
    DEFAULT_DEADLINE,
    MAX_DEADLINE,
    MIN_DEADLINE,
    SubmitQuestionRequest,
    acting_agent_label,
    resolve_deadline,
)

PP = PrivatePurpose.model_validate(PURPOSE)
CANDIDATES = (
    Candidate(id="r1", delegation_id=uuid4(), agent_id=uuid4(), label="Muse"),
    Candidate(id="r2", delegation_id=uuid4(), agent_id=uuid4(), label="Claude"),
)


def request(**overrides: Any) -> SubmitQuestionRequest:
    data: dict[str, Any] = {
        "handle": "chelsea",
        "requested_information": REQUESTED,
        "private_purpose": PURPOSE,
    }
    data.update(overrides)
    return SubmitQuestionRequest.model_validate(data)


# --- Spoiler guard ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "What flowers does Chelsea like?",
        "Which colours does she wear most?",
        "What size shoes does she wear?",
        "Does she prefer tea or coffee?",
        "Which authors does Chelsea read?",
    ],
)
def test_safe_requested_information(text: str) -> None:
    assert validate_requested_information(f"  {text}\n", PP) == text


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("What does she want for her birthday?", SpoilerCategory.OCCASION),
        ("Anything for our anniversary?", SpoilerCategory.OCCASION),
        ("Christmas ideas for her?", SpoilerCategory.OCCASION),
        ("What present would she like?", SpoilerCategory.GIFT),
        ("Ideas for a surprise?", SpoilerCategory.GIFT),
        ("What should I buy her?", SpoilerCategory.GIFT),
        ("Something under $50?", SpoilerCategory.MONEY),
        ("Something under 50 dollars?", SpoilerCategory.MONEY),
        ("What is her budget for books?", SpoilerCategory.MONEY),
        ("What does she do on 3/14?", SpoilerCategory.DATE),
        ("Plans for next week?", SpoilerCategory.DATE),
        ("Anything at the lake house?", SpoilerCategory.PRIVATE_PURPOSE),
        ("Anything for a 30th?", SpoilerCategory.DATE),
        ("Does she like peonies for her birthday?", SpoilerCategory.OCCASION),
    ],
)
def test_unsafe_requested_information(text: str, category: SpoilerCategory) -> None:
    with pytest.raises(UnsafeRequestedInformation) as exc:
        validate_requested_information(text, PP)
    assert category in exc.value.categories
    # The message never echoes the text or purpose values.
    for value in [*PP.fixed_values(), text]:
        assert value.lower() not in str(exc.value).lower()


def test_requested_information_length_and_empty() -> None:
    with pytest.raises(ValueError):
        validate_requested_information("   ", PP)
    with pytest.raises(ValueError):
        validate_requested_information("a" * 501, PP)


DEMO_PURPOSE = PrivatePurpose(
    occasion="birthday",
    occasion_date="next week",
    budget="$100",
    candidate_ideas=["compact binoculars", "bird field guide"],
)
DEMO_REQUESTED = "Does Alex already own binoculars?"


def published(text: str, requested: str = DEMO_REQUESTED, **kw: Any) -> set[SpoilerCategory]:
    return publishable_question_problems(
        text,
        requested_information=requested,
        purpose=kw.pop("purpose", DEMO_PURPOSE),
        identity_terms=kw.pop("identity_terms", ("Sam Example", "Sam", "sam")),
    )


def test_demo_question_accepted() -> None:
    assert validate_requested_information(DEMO_REQUESTED, DEMO_PURPOSE) == DEMO_REQUESTED
    for text in (
        "Does Alex own binoculars?",
        "Does Alex already own a field guide?",
        "Does Alex own compact binoculars?",
    ):
        assert validate_requested_information(text, DEMO_PURPOSE) == text
    # The fallback (requested_information itself) passes the publish gate.
    assert not published(DEMO_REQUESTED)
    assert not published("Does Alex have binoculars already?")


@pytest.mark.parametrize(
    "model_text",
    [
        "Does Alex already own compact binoculars?",  # adds "compact"
        "Does Alex own binoculars or a bird field guide?",  # adds another idea
        "Does Alex have a field guide?",
        "Does Alex like birds?",
        "Does Alex already own binoculars for his birthday?",
        "Does Alex already own binoculars? It is for next week.",
        "Does Alex own binoculars under $100?",
        "Does Alex own binoculars under 100?",
        "Sam asks: does Alex already own binoculars?",
        "Does Alex's assistant know if he owns binoculars?",
    ],
)
def test_demo_model_output_adding_terms_is_unsafe(model_text: str) -> None:
    assert published(model_text)


def test_requester_wrote_whole_idea_allows_it() -> None:
    asked = "Does Alex own compact binoculars?"
    assert not published("Does Alex already have compact binoculars?", requested=asked)
    assert published("Does Alex have compact binoculars or a field guide?", requested=asked)


@pytest.mark.parametrize(
    "text",
    [
        "Does Alex own binoculars for his birthday?",
        "Does Alex own binoculars? Need to know by next week.",
        "Does Alex own binoculars under $100?",
    ],
)
def test_demo_requested_information_with_fixed_values_rejected(text: str) -> None:
    with pytest.raises(UnsafeRequestedInformation):
        validate_requested_information(text, DEMO_PURPOSE)


def test_notes_phrases_always_and_tokens_in_model_output() -> None:
    # Notes phrases (and word pairs) are never allowed in requested_information.
    with pytest.raises(UnsafeRequestedInformation):
        validate_requested_information("Has she been to the lake house?", PP)
    # A single notes word is fine in the requester's own question...
    assert validate_requested_information("Does she like pottery?", PP)
    # ...and in model output only when the requester used it.
    assert not publishable_question_problems(
        "Is she into pottery?",
        requested_information="Does she like pottery?",
        purpose=PP,
        identity_terms=(),
    )
    assert publishable_question_problems(
        "Is she into pottery?", requested_information=REQUESTED, purpose=PP, identity_terms=()
    ) == {SpoilerCategory.PRIVATE_PURPOSE}


@pytest.mark.parametrize(
    "text",
    [
        "Sam, through his assistant, asks whether Alex likes hiking.",
        "Alex's boss is asking: does Alex like hiking?",
        "On behalf of a friend, does Alex like hiking?",
        "This is Sam: does Alex like hiking?",
        "I'm wondering whether Alex likes hiking.",
        "Sam's assistant wants to know if Alex likes hiking.",
        "Does Alex like hiking? (anonymous)",
        "Does Alex like hiking? Sam would like to know.",
    ],
)
def test_identity_claims_rejected_in_requested_information(text: str) -> None:
    with pytest.raises(UnsafeRequestedInformation) as exc:
        validate_requested_information(text, PP, identity_terms=("Sam Example", "Sam", "sam"))
    assert SpoilerCategory.IDENTITY in exc.value.categories
    assert "Sam" not in str(exc.value)


@pytest.mark.parametrize(
    "terms",
    [("Leo Example", "Leo", "leo"), ("Muse",), ("muse.example",)],
)
def test_requester_and_agent_names_rejected(terms: tuple[str, ...]) -> None:
    text = f"Does Alex like hiking? {terms[-1]}"
    with pytest.raises(UnsafeRequestedInformation) as exc:
        validate_requested_information(text, PP, identity_terms=terms)
    assert exc.value.categories == {SpoilerCategory.IDENTITY}
    assert "verified identity" in str(exc.value)


def test_bare_assistant_is_not_an_identity_claim() -> None:
    text = "Does Alex use a voice assistant?"
    assert validate_requested_information(text, PP, identity_terms=("Sam",)) == text
    assert not publishable_question_problems(
        "Which voice assistant does Alex use?",
        requested_information=text,
        purpose=PP,
        identity_terms=("Sam",),
    )
    # The model may not bring up assistants on its own.
    assert SpoilerCategory.IDENTITY in publishable_question_problems(
        "Ask the assistant which flowers Chelsea likes.",
        requested_information=REQUESTED,
        purpose=PP,
        identity_terms=(),
    )


def test_identity_wording_detected_in_model_output() -> None:
    for text in (
        "Taylor asks which flowers she likes.",
        "On behalf of Leo, which flowers?",
        "I am Leo's assistant: which flowers?",
        "Does leo know which flowers?",
    ):
        assert SpoilerCategory.IDENTITY in publishable_question_problems(
            text,
            requested_information=REQUESTED,
            purpose=PP,
            identity_terms=("Leo Example", "Leo"),
        )


@pytest.mark.parametrize(
    ("name", "client_id", "label"),
    [
        ("Muse", "https://muse.example/client.json", "Muse"),
        (None, "https://muse.example/oauth/client.json", "muse.example"),
        ("  ", "https://muse.example/x", "muse.example"),
        (None, "client_01ABC", "Assistant"),
        (None, "http://insecure.example/x", "Assistant"),
        (None, None, "Assistant"),
    ],
)
def test_acting_agent_label(name: str | None, client_id: str | None, label: str) -> None:
    assert acting_agent_label(name, client_id) == label


def test_validate_proposal() -> None:
    def check(q: Any, ids: Any) -> Any:
        return validate_proposal(
            {"minimized_question": q, "respondent_ids": ids},
            requested_information=REQUESTED,
            purpose=PP,
            candidates=CANDIDATES,
            identity_terms=("Leo",),
        )

    assert check("Which flowers does Chelsea like?", ["r2", "r2"]) == (
        "Which flowers does Chelsea like?",
        (CANDIDATES[1],),
    )
    assert check("Which flowers?", ["r3"]) is FallbackReason.INVALID_RESPONDENTS
    assert check("Which flowers?", "r1") is FallbackReason.INVALID_RESPONDENTS
    assert check("Which flowers?", [1]) is FallbackReason.INVALID_RESPONDENTS
    assert check(None, ["r1"]) is FallbackReason.INVALID_QUESTION
    assert check("", ["r1"]) is FallbackReason.INVALID_QUESTION
    assert check("Birthday flowers?", ["r1"]) is FallbackReason.UNSAFE_QUESTION


# --- Routing with the fake client -------------------------------------------------------------


@pytest.mark.anyio
async def test_route_question_model_and_fallback() -> None:
    ok = FakeMuse(content=proposal("Which flowers does Chelsea like best?", ["r1"]))
    decision = await route_question(
        requested_information=REQUESTED, purpose=PP, candidates=CANDIDATES, client=ok.client()
    )
    assert decision.path == "model"
    assert decision.respondents == (CANDIDATES[0],)
    assert decision.event_details() == {"routing_path": "model"}

    bad = FakeMuse(content=proposal("Birthday flowers?", ["r1"]))
    decision = await route_question(
        requested_information=REQUESTED, purpose=PP, candidates=CANDIDATES, client=bad.client()
    )
    assert decision.path == "fallback"
    assert decision.minimized_question == REQUESTED
    assert decision.respondents == CANDIDATES
    assert decision.event_details() == {
        "routing_path": "fallback",
        "fallback_reason": "unsafe_question",
    }

    none = await route_question(
        requested_information=REQUESTED, purpose=PP, candidates=CANDIDATES, client=None
    )
    assert none.fallback_reason is FallbackReason.UNAVAILABLE


@pytest.mark.anyio
async def test_route_question_unexpected_client_error_falls_back() -> None:
    class Broken(MuseSparkClient):
        async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("boom")

    decision = await route_question(
        requested_information=REQUESTED,
        purpose=PP,
        candidates=CANDIDATES,
        client=Broken(api_key=SecretStr("k")),
    )
    assert decision.fallback_reason is FallbackReason.ERROR


# --- Muse Spark client -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        'Here you go: {"a": 1} thanks',
    ],
)
def test_parse_json_object(content: str) -> None:
    assert parse_json_object(content) == {"a": 1}


@pytest.mark.parametrize("content", ["", "nope", "[1]", "{not json}", '"str"'])
def test_parse_json_object_malformed(content: str) -> None:
    with pytest.raises(MuseSparkMalformed):
        parse_json_object(content)


def test_client_availability() -> None:
    assert not MuseSparkClient(api_key=None).available
    assert not MuseSparkClient(api_key=SecretStr("")).available
    assert not MuseSparkClient(api_key=SecretStr("k"), provider="meta").available
    client = MuseSparkClient(api_key=SecretStr("very-secret-key"))
    assert client.available
    assert "very-secret-key" not in repr(client)
    assert client.model == "accounts/fireworks/models/muse-spark-open"


@pytest.mark.anyio
async def test_client_request_shape_and_logging(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[httpx.Request] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=completion('{"ok": true}'))

    client = MuseSparkClient(
        api_key=SecretStr("very-secret-key"),
        model="m",
        transport=httpx.MockTransport(handler),
    )
    with caplog.at_level(logging.DEBUG):
        result = await client.complete_json(
            system="sys", user="PRIVATE-PURPOSE-MARKER", max_tokens=10
        )
    assert result == {"ok": True}
    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 800  # raised to the reasoning-model minimum
    assert body["model"] == "m"
    assert body["reasoning_effort"] == "low"
    assert seen[0].url == httpx.URL("https://api.fireworks.ai/inference/v1/chat/completions")
    assert seen[0].headers["authorization"] == "Bearer very-secret-key"
    logs = caplog.text
    assert "status=200" in logs and "latency_ms=" in logs
    for secret in ("very-secret-key", "PRIVATE-PURPOSE-MARKER", '"ok"'):
        assert secret not in logs


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fake", "error"),
    [
        (FakeMuse(status=503, content="x"), MuseSparkHTTPError),
        (FakeMuse(exc=httpx.ConnectError("down")), MuseSparkHTTPError),
        (FakeMuse(exc=httpx.ReadTimeout("slow")), MuseSparkTimeout),
        (FakeMuse(delay=1.0, content="{}"), MuseSparkTimeout),
        (FakeMuse(content="not json"), MuseSparkMalformed),
        (FakeMuse(content=None), MuseSparkMalformed),
    ],
)
async def test_client_errors(fake: FakeMuse, error: type[Exception]) -> None:
    with pytest.raises(error):
        await fake.client(timeout=0.2).complete_json(system="s", user="u")


@pytest.mark.anyio
async def test_client_unavailable_makes_no_request() -> None:
    fake = FakeMuse(content="{}")
    with pytest.raises(MuseSparkUnavailable):
        await fake.client(provider="meta").complete_json(system="s", user="u")
    assert fake.requests == []


# --- Deadline, schemas, deps -------------------------------------------------------------------


def test_resolve_deadline_bounds() -> None:
    now = datetime(2026, 11, 2, 12, tzinfo=UTC)
    assert resolve_deadline(now, expires_at=None, expires_in_hours=None) == (
        now + DEFAULT_DEADLINE,
        False,
    )
    assert resolve_deadline(now, expires_at=None, expires_in_hours=24) == (
        now + timedelta(hours=24),
        False,
    )
    assert resolve_deadline(now, expires_at=None, expires_in_hours=1e9) == (
        now + MAX_DEADLINE,
        True,
    )
    assert resolve_deadline(now, expires_at=None, expires_in_hours=0.01) == (
        now + MIN_DEADLINE,
        True,
    )
    assert resolve_deadline(now, expires_at=now - timedelta(days=1), expires_in_hours=None) == (
        now + MIN_DEADLINE,
        True,
    )
    in_two_days = now + timedelta(days=2)
    assert resolve_deadline(now, expires_at=in_two_days, expires_in_hours=None) == (
        in_two_days,
        False,
    )


def test_request_validation() -> None:
    with pytest.raises(ValueError):
        request(expires_at=datetime(2026, 11, 3, 12))  # naive
    with pytest.raises(ValueError):
        request(expires_at=datetime(2026, 11, 3, 12, tzinfo=UTC), expires_in_hours=3)
    with pytest.raises(ValueError):
        request(profile_id="00000000-0000-0000-0000-000000000000")  # both targets
    with pytest.raises(ValueError):
        request(handle=None)  # neither target
    with pytest.raises(ValueError):
        request(private_purpose={"occasion": "x", "unexpected": "field"})
    with pytest.raises(ValueError):
        request(requested_information="")
    with pytest.raises(ValueError):
        request(escalation_context={"urgency": "now"})


def test_private_purpose_limits() -> None:
    with pytest.raises(ValueError):
        PrivatePurpose(candidate_ideas=["x" * 201])
    with pytest.raises(ValueError):
        PrivatePurpose(budget="x" * 51)
    with pytest.raises(ValueError):
        PrivatePurpose(candidate_ideas=["x"] * 11)
    assert PrivatePurpose(budget=80.0).string_values() == ["80.0", "80"]


def test_deps_for_uses_shared_sessionmaker() -> None:
    from revel.db.session import get_sessionmaker
    from revel.settings import Settings

    settings = Settings(_env_file=None, app_env="test")
    questions.reset_question_deps()
    deps = questions.deps_for(settings)
    assert deps.sessionmaker is get_sessionmaker()
    assert deps.muse is not None and not deps.muse.available  # no key in test settings
    assert questions.deps_for(settings) is deps
    questions.reset_question_deps()
