"""Polling preference boundaries against real disposable PostgreSQL."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from revel.auth.identity import Principal
from revel.db.models import Agent, AgentDelegation, User
from revel.db.models.inbox_setup import InboxCheckSetup
from revel.services import inbox_setup as svc
from revel.services.questions import QuestionDeps, get_updates


@pytest.fixture
def setup_actors(migrated_engine):
    factory = sessionmaker(migrated_engine, expire_on_commit=False)
    user, other, agent, delegation, second = [uuid4() for _ in range(5)]
    with factory.begin() as session:
        session.add_all(
            [User(id=user, workos_user_id=str(user)), User(id=other, workos_user_id=str(other))]
        )
        session.add(Agent(id=agent, oauth_client_id=str(agent)))
        session.flush()
        session.add_all(
            [
                AgentDelegation(id=delegation, user_id=user, agent_id=agent),
                AgentDelegation(id=second, user_id=other, agent_id=agent),
            ]
        )
    yield (
        factory,
        Principal(user, None, None, "ui"),
        Principal(user, agent, delegation, "mcp"),
        second,
    )
    with factory.begin() as session:
        session.execute(delete(InboxCheckSetup).where(InboxCheckSetup.user_id == user))
        session.execute(delete(AgentDelegation).where(AgentDelegation.id.in_([delegation, second])))
        session.execute(delete(Agent).where(Agent.id == agent))
        session.execute(delete(User).where(User.id.in_([user, other])))


def schedule(principal, revision=0, **kw):
    return svc.ScheduleRequest(
        expected_revision=revision,
        enabled=True,
        delegation_id=principal.delegation_id,
        timezone="America/Los_Angeles",
        **kw,
    )


def test_confirmation_revision_dedupe_and_reported_evidence(setup_actors):
    factory, owner, assistant, _ = setup_actors
    assert svc.read_status(owner, factory).schedule_state == "not_configured"
    with pytest.raises(svc.SetupError, match="Ask assistant") as exc:
        svc.configure(assistant, schedule(assistant), factory)
    assert exc.value.code == "owner_confirmation_required"
    assert svc.read_status(owner, factory).revision == 0
    body = schedule(assistant, owner_confirmed=True)
    status = svc.configure(assistant, body, factory)
    assert status.revision == 1 and status.schedule_state == "awaiting_host_task"
    with pytest.raises(svc.SetupError) as exc:
        svc.configure(assistant, body, factory)
    assert exc.value.code == "stale_revision"
    body = schedule(assistant, 1, owner_confirmed=True)
    assert svc.configure(assistant, body, factory).revision == 1
    reported = svc.report_task(
        assistant, svc.TaskReport(expected_revision=1, host_task_id="synthetic-task"), factory
    )
    assert reported.schedule_state == "host_reported" and reported.background_verified is False
    assert reported.last_successful_check_at is None
    with pytest.raises(svc.SetupError) as exc:
        svc.report_task(
            assistant, svc.TaskReport(expected_revision=1, host_task_id="another-task"), factory
        )
    assert exc.value.code == "task_already_reported"


def test_other_users_delegation_and_disconnection_rejected(setup_actors):
    factory, owner, assistant, other = setup_actors
    body = schedule(assistant, owner_confirmed=True).model_copy(update={"delegation_id": other})
    with pytest.raises(svc.SetupError) as exc:
        svc.configure(owner, body, factory)
    assert exc.value.code == "not_allowed"
    svc.configure(owner, schedule(assistant), factory)
    with factory.begin() as session:
        delegation = session.get(AgentDelegation, assistant.delegation_id)
        delegation.status = "disconnected"
        delegation.disconnected_at = datetime.now(UTC)
    status = svc.read_status(owner, factory)
    assert status.schedule_state == "assistant_disconnected" and status.host_action_required
    with pytest.raises(svc.SetupError):
        svc.read_status(assistant, factory)


def test_disable_keeps_host_action_until_actual_host_report(setup_actors):
    factory, owner, assistant, _ = setup_actors
    svc.configure(owner, schedule(assistant), factory)
    svc.report_task(
        assistant, svc.TaskReport(expected_revision=1, host_task_id="synthetic"), factory
    )
    disabled = svc.configure(
        owner,
        svc.ScheduleRequest(
            expected_revision=1, enabled=False, delegation_id=assistant.delegation_id
        ),
        factory,
    )
    assert disabled.host_action_required and disabled.host_task_id == "synthetic"
    with pytest.raises(svc.SetupError):
        svc.report_task(assistant, svc.TaskReport(expected_revision=1, disabled=True), factory)
    stopped = svc.report_task(
        assistant, svc.TaskReport(expected_revision=2, disabled=True), factory
    )
    assert not stopped.host_action_required and stopped.host_task_id is None


@pytest.mark.anyio
async def test_empty_actual_inbox_check_records_server_success_without_background_claim(
    setup_actors,
):
    factory, owner, assistant, _ = setup_actors
    svc.read_status(owner, factory)
    now = datetime.now(UTC)
    result = await get_updates(assistant, deps=QuestionDeps(factory, now=lambda: now))
    assert result.items == []
    status = svc.read_status(owner, factory)
    assert status.last_successful_check_at == now and status.last_check_item_count == 0
    assert status.last_successful_check_delegation_id == assistant.delegation_id
    assert status.last_check_context == "foreground_or_background_unknown"
    assert status.background_verified is False and status.host_task_reported_at is None
    with factory() as session:
        assert session.scalar(select(InboxCheckSetup.user_id)) is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "not/a/zone"},
        {"check_hours": [6, 6]},
        {"check_hours": []},
        {"check_hours": [24]},
    ],
)
def test_invalid_schedule_is_refused(changes):
    with pytest.raises(ValidationError):
        svc.ScheduleRequest(
            expected_revision=0,
            enabled=True,
            delegation_id=uuid4(),
            **({"timezone": "UTC"} | changes),
        )
