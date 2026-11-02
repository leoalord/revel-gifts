"""Test doubles for identity resolution, shared by F2 tests and other backend tests.

Usage from another test module (``tests/`` is on ``sys.path`` under pytest)::

    from auth.fakes import InMemoryIdentityRepository, RecordingHooks
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import anyio

from revel.auth.repository import (
    AgentRecord,
    DelegationRecord,
    DelegationView,
    OnCreated,
    OnDisconnected,
    UserRecord,
)


class InMemoryIdentityRepository:
    """Test double with the same insert-if-absent and rollback-on-hook-failure semantics."""

    def __init__(self) -> None:
        self.users: dict[UUID, UserRecord] = {}
        self.agents: dict[UUID, AgentRecord] = {}
        self.delegations: dict[UUID, DelegationRecord] = {}
        # Timestamps kept beside the records (``DelegationRecord`` has none); tests may set
        # ``connected_at`` directly to control list ordering.
        self.connected_at: dict[UUID, datetime] = {}
        self.disconnected_at: dict[UUID, datetime] = {}
        self._lock = anyio.Lock()

    async def get_user_by_workos_id(self, workos_user_id: str) -> UserRecord | None:
        return next((u for u in self.users.values() if u.workos_user_id == workos_user_id), None)

    async def get_user(self, user_id: UUID) -> UserRecord | None:
        return self.users.get(user_id)

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        return self.agents.get(agent_id)

    def _view(self, record: DelegationRecord) -> DelegationView:
        return DelegationView(
            id=record.id,
            user_id=record.user_id,
            status=record.status,
            connected_at=self.connected_at[record.id],
            disconnected_at=self.disconnected_at.get(record.id),
            agent=self.agents[record.agent_id],
        )

    async def list_delegations_for_user(self, user_id: UUID) -> list[DelegationView]:
        views = [self._view(d) for d in self.delegations.values() if d.user_id == user_id]
        return sorted(views, key=lambda v: (v.connected_at, v.id), reverse=True)

    async def get_delegation_view(
        self, user_id: UUID, delegation_id: UUID
    ) -> DelegationView | None:
        record = self.delegations.get(delegation_id)
        if record is None or record.user_id != user_id:
            return None
        return self._view(record)

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
            self.connected_at[record.id] = datetime.now(UTC)
            try:
                if on_created is not None:
                    await on_created(record)
            except BaseException:
                del self.delegations[record.id]
                del self.connected_at[record.id]
                raise
            return record, True

    async def disconnect_delegation(
        self,
        *,
        user_id: UUID,
        delegation_id: UUID,
        on_disconnected: OnDisconnected | None = None,
    ) -> bool:
        async with self._lock:
            existing = self.delegations.get(delegation_id)
            if existing is None or existing.user_id != user_id or existing.status != "active":
                return False
            record = replace(existing, status="disconnected")
            self.delegations[delegation_id] = record
            self.disconnected_at[delegation_id] = datetime.now(UTC)
            try:
                if on_disconnected is not None:
                    await on_disconnected(record)
            except BaseException:
                self.delegations[delegation_id] = existing
                del self.disconnected_at[delegation_id]
                raise
            return True

    def disconnect(self, delegation_id: UUID) -> None:
        """Simulates #14's disconnect flow."""
        self.delegations[delegation_id] = replace(
            self.delegations[delegation_id], status="disconnected"
        )
        self.disconnected_at[delegation_id] = datetime.now(UTC)


class RecordingHooks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_next_user_hook = False
        self.fail_next_disconnect_hook = False

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

    async def on_agent_disconnected(
        self, *, user_id: UUID, delegation_id: UUID, agent_id: UUID
    ) -> None:
        if self.fail_next_disconnect_hook:
            self.fail_next_disconnect_hook = False
            raise RuntimeError("authz unavailable")
        self.calls.append(
            (
                "on_agent_disconnected",
                {"user_id": user_id, "delegation_id": delegation_id, "agent_id": agent_id},
            )
        )
