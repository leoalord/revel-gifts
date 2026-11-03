"""The answer skill's human-contact sequence through F7's real lease and answer services.

Skipped unless ``REVEL_TEST_DATABASE_URL`` is set. Reuses F7's fixtures from
``tests/questions/questions_support.py`` (loaded by path, read-only): PostgreSQL plus real
OpenFGA when ``OPENFGA_TEST_API_URL`` is reachable, otherwise F4's ``FakeFga``. Run DB suites
serially.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from revel.guidance import documents
from revel.services import answers, question_leases, questions

from .test_skills_f6_f7 import ANSWER, examples


def _support() -> Any:
    if "questions_support" in sys.modules:  # already imported by tests/questions
        return sys.modules["questions_support"]
    path = Path(__file__).resolve().parents[1] / "questions" / "questions_support.py"
    spec = importlib.util.spec_from_file_location("questions_support", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_f7 = _support()
engine = _f7.engine
factory = _f7.factory
fga = _f7.fga
world_builder = _f7.world_builder
anyio_backend = _f7.anyio_backend

pytestmark = [pytest.mark.anyio, _f7.requires_db]


def _example(tool: str, predicate: Any = lambda d: True) -> dict[str, Any]:
    found = [d for t, _, d in examples(ANSWER) if t == tool and predicate(d)]
    assert found, tool
    return {k: v for k, v in found[0].items() if k != "assignment_id"}


async def _assignment(factory: Any, world: Any, deps: questions.QuestionDeps) -> UUID:
    request = questions.SubmitQuestionRequest.model_validate(
        {
            "handle": "chelsea",
            "requested_information": _f7.REQUESTED,
            "private_purpose": _f7.PURPOSE,
        }
    )
    asked = await questions.submit_question(
        _f7.agent_principal(world.requester_d), request, deps=deps
    )
    items = (await questions.get_updates(_f7.agent_principal(world.owner_d1), deps=deps)).items
    (item,) = [i for i in items if i.question_id == asked.question_id]
    return item.assignment_id


async def test_documented_human_contact_sequence(
    factory: Any, fga: Any, world_builder: Any
) -> None:
    world = await world_builder()
    deps = questions.QuestionDeps(sessionmaker=factory, now=lambda: datetime.now(UTC))
    d1 = _f7.agent_principal(world.owner_d1)
    assignment_id = await _assignment(factory, world, deps)

    plain = await question_leases.claim_assignment(
        d1, assignment_id, question_leases.ClaimRequest(), deps=deps
    )
    assert plain.human_contact is False

    # A plain re-claim of your own lease, or an upgrade without lease_version: already_claimed.
    for request in (
        question_leases.ClaimRequest(),
        question_leases.ClaimRequest(human_contact=True),
    ):
        with pytest.raises(question_leases.AlreadyClaimed) as exc:
            await question_leases.claim_assignment(d1, assignment_id, request, deps=deps)
        assert exc.value.code == "already_claimed"

    # Reporting contact without a human-contact lease is refused (and nothing is acquired).
    with pytest.raises(question_leases.HumanContactRequired) as exc:
        await question_leases.report_user_contact(
            d1,
            assignment_id,
            question_leases.UserContactRequest(
                lease_version=plain.lease_version, delivery_confirmed=True
            ),
            deps=deps,
        )
    assert exc.value.code == "human_contact_required" and exc.value.status_code == 409

    # The documented upgrade, with the current lease_version.
    upgrade = _example("claim_assignment") | {"lease_version": plain.lease_version}
    upgraded = await question_leases.claim_assignment(
        d1, assignment_id, question_leases.ClaimRequest.model_validate(upgrade), deps=deps
    )
    assert upgraded.human_contact is True
    assert upgraded.lease_version > plain.lease_version
    assert upgraded.lease_expires_at == plain.lease_expires_at
    assert upgraded.prior_user_contacts == 0

    waiting = await question_leases.report_user_contact(
        d1,
        assignment_id,
        question_leases.UserContactRequest(
            lease_version=upgraded.lease_version, delivery_confirmed=True
        ),
        deps=deps,
    )
    assert waiting.status == "waiting_for_user"

    # The documented inline-evidence answer completes it.
    answer = _example("submit_answer", lambda d: "claim_id" not in d["evidence"][0])
    answer["lease_version"] = waiting.lease_version
    result = await answers.submit_answer(
        d1, assignment_id, answers.SubmitAnswerRequest.model_validate(answer), deps=deps
    )
    assert (result.question_status, result.assignment_status) == ("answered", "completed")
    assert "`human_contact_required`" in documents()[ANSWER].body
