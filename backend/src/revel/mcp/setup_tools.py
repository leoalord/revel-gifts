"""MCP polling setup: host reports are never background verification."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from revel.auth.deps import principal_from_mcp_context
from revel.auth.runtime import runtime_for
from revel.db.session import get_sessionmaker
from revel.services import inbox_setup as svc


def register(server: MCPServer, settings) -> None:
    async def call(fn, *args):
        principal = await principal_from_mcp_context(runtime_for(settings))
        try:
            return await anyio.to_thread.run_sync(lambda: fn(principal, *args, get_sessionmaker()))
        except svc.SetupError as exc:
            raise ToolError(f"{exc.code}: {exc.detail}") from None

    async def get_setup() -> svc.SetupStatus:
        return await call(svc.read_status)

    async def configure_inbox_checks(
        action: Literal["schedule", "report_task"],
        expected_revision: int,
        enabled: bool | None = None,
        delegation_id: UUID | None = None,
        timezone: str | None = None,
        check_hours: list[int] | None = None,
        owner_confirmed: bool = False,
        host_task_id: str | None = None,
        disabled: bool | None = None,
    ) -> svc.SetupStatus:
        try:
            if action == "schedule":
                if enabled is None or host_task_id is not None or disabled is not None:
                    raise ToolError("invalid_request: schedule needs enabled and no task report.")
                body = svc.ScheduleRequest(
                    expected_revision=expected_revision,
                    enabled=enabled,
                    delegation_id=delegation_id,
                    timezone=timezone,
                    check_hours=check_hours if check_hours is not None else [6, 10, 14, 18],
                    owner_confirmed=owner_confirmed,
                )
                return await call(svc.configure, body)
            if (
                any(v is not None for v in (enabled, delegation_id, timezone, check_hours))
                or owner_confirmed
            ):
                raise ToolError("invalid_request: report_task accepts only the task report.")
            body = svc.TaskReport(
                expected_revision=expected_revision,
                host_task_id=host_task_id,
                disabled=disabled or False,
            )
            return await call(svc.report_task, body)
        except ValidationError:
            raise ToolError(
                "invalid_request: Check the schedule, timezone and task report fields."
            ) from None

    server.add_tool(
        get_setup,
        name="get_setup",
        description=(
            "Read your user's inbox polling setup before onboarding or changing schedules. "
            "Includes desired hours/timezone, designated delegation and any host-reported task. "
            "Task reports are unverified. Last successful get_updates is server-observed, "
            "but foreground versus background execution is unknown. Revel cannot schedule tasks."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    server.add_tool(
        configure_inbox_checks,
        name="configure_inbox_checks",
        description=(
            "Record owner-approved polling preferences or report actual host task state. "
            "First read get_setup. action=schedule requires enabled, expected_revision, and "
            "when enabling, delegation_id and confirmed IANA timezone; check_hours defaults "
            "to [6,10,14,18]. Call without owner_confirmed for exact confirmation wording; "
            "show it and get owner approval before repeating with owner_confirmed=true. "
            "Then use your host's scheduling capability to create/update ONE recurring task "
            "under owner approval, reconciling any existing host_task_id. Only after actual "
            "creation/update, action=report_task with expected_revision and host_task_id. "
            "To disable: record enabled=false, stop the actual host task, then report_task "
            "with disabled=true. Server disablement does not stop the host task. Reports "
            "never verify unattended operation. Read revel-get-started guidance."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
        ),
    )
