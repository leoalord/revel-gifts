"""Owner-controlled polling preferences and explicitly labelled evidence.

Revel cannot create or inspect a host's recurring tasks. Task references are reports,
never verification. A successful get_updates is observable; its execution context is not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio.to_thread
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from revel.auth.identity import Principal
from revel.db.models import AgentDelegation
from revel.db.models.inbox_setup import InboxCheckSetup


class SetupError(Exception):
    def __init__(self, code: str, detail: str, status: int = 409):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(detail)


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    enabled: bool
    delegation_id: UUID | None = None
    timezone: str | None = None
    check_hours: list[int] = Field(default_factory=lambda: [6, 10, 14, 18])
    owner_confirmed: bool = False

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Use an IANA timezone, confirmed by the owner.") from None
        return value

    @field_validator("check_hours")
    @classmethod
    def valid_hours(cls, value: list[int]) -> list[int]:
        if not value or len(value) > 24 or any(h < 0 or h > 23 for h in value):
            raise ValueError("Choose one to 24 whole local hours from 0 to 23.")
        if len(value) != len(set(value)):
            raise ValueError("Repeated check hours are not allowed.")
        return sorted(value)

    @model_validator(mode="after")
    def enabled_needs_host(self) -> ScheduleRequest:
        if self.enabled and (self.delegation_id is None or self.timezone is None):
            raise ValueError("Enabled checks need a designated assistant and confirmed timezone.")
        return self


class TaskReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    host_task_id: str | None = Field(default=None, min_length=1, max_length=200)
    disabled: bool = False

    @model_validator(mode="after")
    def task_or_disabled(self) -> TaskReport:
        if self.disabled == (self.host_task_id is not None):
            raise ValueError("Report either a host task reference or confirmed disablement.")
        return self


class SetupStatus(BaseModel):
    revision: int = 0
    enabled: bool = False
    delegation_id: UUID | None = None
    timezone: str | None = None
    check_hours: list[int] = Field(default_factory=lambda: [6, 10, 14, 18])
    host_task_id: str | None = None
    owner_confirmed_at: datetime | None = None
    host_task_reported_at: datetime | None = None
    schedule_state: Literal[
        "not_configured",
        "awaiting_host_task",
        "host_reported",
        "disabled",
        "assistant_disconnected",
    ] = "not_configured"
    background_verified: Literal[False] = False
    last_successful_check_at: datetime | None = None
    last_successful_check_delegation_id: UUID | None = None
    last_check_item_count: int | None = None
    last_check_context: Literal["foreground_or_background_unknown"] = (
        "foreground_or_background_unknown"
    )
    email_fallback: Literal["not_implemented"] = "not_implemented"
    host_action_required: bool = False


def _active(session: Session, user_id: UUID, delegation_id: UUID | None) -> bool:
    return (
        delegation_id is not None
        and session.scalar(
            select(AgentDelegation.id).where(
                AgentDelegation.id == delegation_id,
                AgentDelegation.user_id == user_id,
                AgentDelegation.status == "active",
            )
        )
        is not None
    )


def _authorize(session: Session, principal: Principal) -> None:
    if principal.delegation_id is not None and not _active(
        session, principal.user_id, principal.delegation_id
    ):
        raise SetupError("not_allowed", "This assistant is no longer connected.", 403)


def _row(session: Session, user_id: UUID, *, lock: bool = False) -> InboxCheckSetup:
    session.execute(insert(InboxCheckSetup).values(user_id=user_id).on_conflict_do_nothing())
    query = select(InboxCheckSetup).where(InboxCheckSetup.user_id == user_id)
    if lock:
        query = query.with_for_update()
    return session.scalars(query).one()


def _status(session: Session, row: InboxCheckSetup) -> SetupStatus:
    active = _active(session, row.user_id, row.delegation_id)
    state = "not_configured"
    if row.schedule_revision:
        if not row.enabled:
            state = "disabled"
        elif not active:
            state = "assistant_disconnected"
        elif row.host_task_reported_at:
            state = "host_reported"
        else:
            state = "awaiting_host_task"
    return SetupStatus(
        revision=row.schedule_revision,
        enabled=row.enabled,
        delegation_id=row.delegation_id,
        timezone=row.timezone,
        check_hours=row.check_hours,
        host_task_id=row.host_task_id,
        owner_confirmed_at=row.owner_confirmed_at,
        host_task_reported_at=row.host_task_reported_at,
        schedule_state=state,
        last_successful_check_at=row.last_successful_check_at,
        last_successful_check_delegation_id=row.last_successful_check_delegation_id,
        last_check_item_count=row.last_check_item_count,
        host_action_required=(
            bool(row.host_task_id)
            if not row.enabled
            else not active or row.host_task_reported_at is None
        ),
    )


def read_status(principal: Principal, factory: sessionmaker[Session]) -> SetupStatus:
    with factory.begin() as session:
        _authorize(session, principal)
        return _status(session, _row(session, principal.user_id))


def configure(
    principal: Principal, request: ScheduleRequest, factory: sessionmaker[Session]
) -> SetupStatus:
    with factory.begin() as session:
        _authorize(session, principal)
        row = _row(session, principal.user_id, lock=True)
        if row.schedule_revision != request.expected_revision:
            raise SetupError(
                "stale_revision", "Setup changed. Read it again before making changes."
            )
        if request.enabled and not _active(session, principal.user_id, request.delegation_id):
            raise SetupError("not_allowed", "Choose one of your active connected assistants.", 403)
        if row.host_task_id and request.delegation_id != row.delegation_id and request.enabled:
            raise SetupError("host_action_required", "Disable and confirm the old host task first.")
        if principal.via != "ui" and not request.owner_confirmed:
            raise SetupError("owner_confirmation_required", confirmation_text(request), 403)
        values = (request.enabled, request.delegation_id, request.timezone, request.check_hours)
        if values != (row.enabled, row.delegation_id, row.timezone, row.check_hours):
            row.enabled, row.delegation_id, row.timezone, row.check_hours = values
            row.schedule_revision += 1
            row.owner_confirmed_at = datetime.now(UTC)
            # Keep the old task reference so the host can reconcile it instead of duplicating.
            row.host_task_reported_at = None
        return _status(session, row)


def confirmation_text(request: ScheduleRequest) -> str:
    if not request.enabled:
        return (
            "Disable Revel inbox-check preferences. Also disable the actual recurring "
            "task in its assistant host; Revel cannot stop that task."
        )
    return (
        f"Ask assistant {request.delegation_id} to check Revel at local hours "
        f"{', '.join(map(str, request.check_hours))} in {request.timezone}. "
        "The host must create or update one recurring task with your approval. "
        "Revel stores your preference but cannot create or verify that task."
    )


def report_task(
    principal: Principal, report: TaskReport, factory: sessionmaker[Session]
) -> SetupStatus:
    with factory.begin() as session:
        _authorize(session, principal)
        row = _row(session, principal.user_id, lock=True)
        if row.schedule_revision != report.expected_revision:
            raise SetupError("stale_revision", "Setup changed. Read it again before reporting.")
        if row.schedule_revision == 0:
            raise SetupError("not_configured", "Confirm the schedule with the owner first.")
        if principal.delegation_id is not None and principal.delegation_id != row.delegation_id:
            raise SetupError(
                "not_allowed", "Only the designated assistant may report its task.", 403
            )
        if report.disabled != (not row.enabled):
            raise SetupError("invalid_state", "The host report must match current preferences.")
        if row.enabled and not _active(session, principal.user_id, row.delegation_id):
            raise SetupError("not_allowed", "The designated assistant is disconnected.", 403)
        if row.host_task_reported_at and row.host_task_id != report.host_task_id:
            raise SetupError("task_already_reported", "Reconcile the recorded host task first.")
        row.host_task_id = report.host_task_id
        row.host_task_reported_at = datetime.now(UTC)
        return _status(session, row)


async def record_successful_check(
    principal: Principal, item_count: int, factory: sessionmaker[Session], now: datetime
) -> None:
    """Record server-observed success; never accept timestamps or background claims from host."""

    def record() -> None:
        with factory.begin() as session:
            row = session.get(InboxCheckSetup, principal.user_id, with_for_update=True)
            if row is not None and _active(session, principal.user_id, principal.delegation_id):
                row.last_successful_check_at = now
                row.last_successful_check_delegation_id = principal.delegation_id
                row.last_check_item_count = item_count

    await anyio.to_thread.run_sync(record)
