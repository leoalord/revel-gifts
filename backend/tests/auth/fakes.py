"""Test doubles for identity resolution, shared by F2 tests and other backend tests.

Usage from another test module (``tests/`` is on ``sys.path`` under pytest)::

    from auth.fakes import InMemoryIdentityRepository, RecordingHooks
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import anyio

from revel.auth.repository import AgentRecord, DelegationRecord, OnCreated, UserRecord


class InMemoryIdentityRepository:
    """Test double with the same insert-if-absent and rollback-on-hook-failure semantics."""

    def __init__(self) -> None:
        self.users: dict[UUID, UserRecord] = {}
        self.agents: dict[UUID, AgentRecord] = {}
        self.delegations: dict[UUID, DelegationRecord] = {}
        self._lock = anyio.Lock()

    async def get_user_by_workos_id(self, workos_user_id: str) -> UserRecord | None:
        return next((u for u in self.users.values() if u.workos_user_id == workos_user_id), None)

    async def get_user(self, user_id: UUID) -> UserRecord | None:
        return self.users.get(user_id)

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        return self.agents.get(agent_id)

    async def ensure_user(
        self,
        workos_user_id: str,
        *,
        email: str | None,
        display_name: str | None,
        on_created: OnCreated[UserRecord] | None = None,
    ) -> tuple[UserRecord, bool]:
        async with self._lock:
            existing = await self.get_user_by_workos_id(workos_user_id)
            if existing is not None:
                return existing, False
            record = UserRecord(uuid4(), workos_user_id, email, display_name)
            self.users[record.id] = record
            try:
                if on_created is not None:
                    await on_created(record)
            except BaseException:
                del self.users[record.id]
                raise
            return record, True

    async def ensure_agent(self, oauth_client_id: str) -> tuple[AgentRecord, bool]:
        async with self._lock:
            existing = next(
                (a for a in self.agents.values() if a.oauth_client_id == oauth_client_id), None
            )
            if existing is not None:
                return existing, False
            record = AgentRecord(uuid4(), oauth_client_id, None)
            self.agents[record.id] = record
            return record, True

    async def ensure_delegation(
        self,
        user_id: UUID,
        agent_id: UUID,
        *,
        on_created: OnCreated[DelegationRecord] | None = None,
    ) -> tuple[DelegationRecord, bool]:
        async with self._lock:
            existing = next(
                (
                    d
                    for d in self.delegations.values()
                    if d.user_id == user_id and d.agent_id == agent_id
                ),
                None,
            )
            if existing is not None:
                return existing, False
            record = DelegationRecord(uuid4(), user_id, agent_id, "active")
            self.delegations[record.id] = record
            try:
                if on_created is not None:
                    await on_created(record)
            except BaseException:
                del self.delegations[record.id]
                raise
            return record, True

    def disconnect(self, delegation_id: UUID) -> None:
        """Simulates #14's disconnect flow."""
        self.delegations[delegation_id] = replace(
            self.delegations[delegation_id], status="disconnected"
        )


class RecordingHooks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_next_user_hook = False

    async def on_user_created(self, *, user_id: UUID) -> None:
        if self.fail_next_user_hook:
            self.fail_next_user_hook = False
            raise RuntimeError("authz unavailable")
        self.calls.append(("on_user_created", {"user_id": user_id}))

    async def on_agent_delegated(
        self, *, user_id: UUID, agent_id: UUID, delegation_id: UUID
    ) -> None:
        self.calls.append(
            (
                "on_agent_delegated",
                {"user_id": user_id, "agent_id": agent_id, "delegation_id": delegation_id},
            )
        )
