"""Successful pickup evidence is distinct from a failed check or host task report."""

import pytest
from questions_support import REQUESTED, agent_principal, requires_db

from revel.services import inbox_setup, questions

pytestmark = [pytest.mark.anyio, requires_db]


async def test_nonempty_server_check_and_authorization_failure_evidence(
    factory, fga, world_builder, monkeypatch
):
    world = await world_builder()
    respondent = agent_principal(world.owner_d1)
    inbox_setup.read_status(respondent, factory)
    deps = world.deps(factory)
    await questions.submit_question(
        agent_principal(world.requester_d),
        questions.SubmitQuestionRequest(handle="chelsea", requested_information=REQUESTED),
        deps=deps,
    )
    response = await questions.get_updates(respondent, deps=deps)
    assert len(response.items) == 1
    success = inbox_setup.read_status(respondent, factory)
    assert success.last_check_item_count == 1
    assert success.last_successful_check_at is not None
    assert success.background_verified is False and success.host_task_reported_at is None

    async def unavailable(*args, **kwargs):
        raise questions.RespondCheckUnavailable()

    monkeypatch.setattr(questions, "respond_allowed", unavailable)
    with pytest.raises(questions.QuestionServiceUnavailable):
        await questions.get_updates(respondent, deps=deps)
    failed = inbox_setup.read_status(respondent, factory)
    assert failed.last_successful_check_at == success.last_successful_check_at
    assert failed.last_check_item_count == 1
