"""Persistence boundary for identity resolution (users, agents, delegations).

F3 owns the models and migrations (``revel.db.models``: ``User``, ``Agent``,
``AgentDelegation``; ``revel.db.enums.DelegationStatus``). This module defines
the narrow interface the resolver needs plus a SQLAlchemy implementation written
against those names. Model imports are deferred so this package imports before
the models exist.

Every ``ensure_*`` method is an insert-if-absent that never updates an existing
row. ``on_created`` runs inside the inserting transaction, so if it raises the
insert is rolled back and a retry will create the row (and call it) again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar
from uuid import UUID, uuid4

import anyio
import anyio.from_thread
import anyio.to_thread

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

DelegationStatusValue = Literal["active", "disconnected"]


@dataclass(frozen=True)
class UserRecord:
    id: UUID
    workos_user_id: str
    email: str | None
    display_name: str | None


@dataclass(frozen=True)
class AgentRecord:
    id: UUID
    oauth_client_id: str
    name: str | None


@dataclass(frozen=True)
class DelegationRecord:
    id: UUID
    user_id: UUID
    agent_id: UUID
    status: DelegationStatusValue


R = TypeVar("R")
OnCreated = Callable[[R], Awaitable[None]]


class IdentityRepository(Protocol):
    async def get_user_by_workos_id(self, workos_user_id: str) -> UserRecord | None: ...

    async def ensure_user(
        self,
        workos_user_id: str,
        *,
        email: str | None,
        display_name: str | None,
        on_created: OnCreated[UserRecord] | None = None,
    ) -> tuple[UserRecord, bool]: ...

    async def ensure_agent(self, oauth_client_id: str) -> tuple[AgentRecord, bool]: ...

    async def ensure_delegation(
        self,
        user_id: UUID,
        agent_id: UUID,
        *,
        on_created: OnCreated[DelegationRecord] | None = None,
    ) -> tuple[DelegationRecord, bool]: ...

    async def get_user(self, user_id: UUID) -> UserRecord | None: ...

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None: ...


def _status_value(status: Any) -> DelegationStatusValue:
    value = getattr(status, "value", status)
    if value not in ("active", "disconnected"):
        raise ValueError("unknown delegation status")
    return value  # type: ignore[return-value]


class SqlAlchemyIdentityRepository:
    """PostgreSQL implementation (``INSERT ... ON CONFLICT DO NOTHING`` then ``SELECT``).

    Uses a synchronous SQLAlchemy ``sessionmaker`` run in worker threads: the
    locked dependencies do not include ``greenlet``, which SQLAlchemy's asyncio
    extension requires. ``on_created`` hooks are async and run on the event loop
    (via ``anyio.from_thread``) while the inserting transaction is still open.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        models: tuple[Any, Any, Any, Any] | None = None,
    ):
        """``models`` overrides ``(User, Agent, AgentDelegation, DelegationStatus)`` for tests."""
        self._session_factory = session_factory
        self._model_override = models

    def _models(self) -> tuple[Any, Any, Any, Any]:
        if self._model_override is not None:
            return self._model_override
        from revel.db.enums import DelegationStatus
        from revel.db.models import Agent, AgentDelegation, User

        return User, Agent, AgentDelegation, DelegationStatus

    @staticmethod
    def _user(row: Any) -> UserRecord:
        return UserRecord(
            id=row.id,
            workos_user_id=row.workos_user_id,
            email=row.email,
            display_name=row.display_name,
        )

    @staticmethod
    def _agent(row: Any) -> AgentRecord:
        return AgentRecord(id=row.id, oauth_client_id=row.oauth_client_id, name=row.name)

    @staticmethod
    def _delegation(row: Any) -> DelegationRecord:
        return DelegationRecord(
            id=row.id, user_id=row.user_id, agent_id=row.agent_id, status=_status_value(row.status)
        )

    # --- reads ---------------------------------------------------------------------

    def _get_user_by_workos_id(self, workos_user_id: str) -> UserRecord | None:
        from sqlalchemy import select

        user_model, *_ = self._models()
        with self._session_factory() as session:
            row = session.execute(
                select(user_model).where(user_model.workos_user_id == workos_user_id)
            ).scalar_one_or_none()
            return self._user(row) if row is not None else None

    def _get_user(self, user_id: UUID) -> UserRecord | None:
        user_model, *_ = self._models()
        with self._session_factory() as session:
            row = session.get(user_model, user_id)
            return self._user(row) if row is not None else None

    def _get_agent(self, agent_id: UUID) -> AgentRecord | None:
        _, agent_model, *_ = self._models()
        with self._session_factory() as session:
            row = session.get(agent_model, agent_id)
            return self._agent(row) if row is not None else None

    async def get_user_by_workos_id(self, workos_user_id: str) -> UserRecord | None:
        return await anyio.to_thread.run_sync(self._get_user_by_workos_id, workos_user_id)

    async def get_user(self, user_id: UUID) -> UserRecord | None:
        return await anyio.to_thread.run_sync(self._get_user, user_id)

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        return await anyio.to_thread.run_sync(self._get_agent, agent_id)

    # --- insert-if-absent ------------------------------------------------------------

    def _ensure_user(
        self,
        workos_user_id: str,
        email: str | None,
        display_name: str | None,
        on_created: OnCreated[UserRecord] | None,
    ) -> tuple[UserRecord, bool]:
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert

        user_model, *_ = self._models()
        with self._session_factory() as session, session.begin():
            stmt = (
                insert(user_model)
                .values(
                    id=uuid4(),
                    workos_user_id=workos_user_id,
                    email=email,
                    display_name=display_name,
                )
                .on_conflict_do_nothing(index_elements=[user_model.workos_user_id])
                .returning(user_model.id)
            )
            created = session.execute(stmt).scalar_one_or_none() is not None
            row = session.execute(
                select(user_model).where(user_model.workos_user_id == workos_user_id)
            ).scalar_one()
            record = self._user(row)
            if created and on_created is not None:
                anyio.from_thread.run(on_created, record)
        return record, created

    def _ensure_agent(self, oauth_client_id: str) -> tuple[AgentRecord, bool]:
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert

        _, agent_model, *_ = self._models()
        with self._session_factory() as session, session.begin():
            stmt = (
                insert(agent_model)
                .values(id=uuid4(), oauth_client_id=oauth_client_id)
                .on_conflict_do_nothing(index_elements=[agent_model.oauth_client_id])
                .returning(agent_model.id)
            )
            created = session.execute(stmt).scalar_one_or_none() is not None
            row = session.execute(
                select(agent_model).where(agent_model.oauth_client_id == oauth_client_id)
            ).scalar_one()
            return self._agent(row), created

    def _ensure_delegation(
        self,
        user_id: UUID,
        agent_id: UUID,
        on_created: OnCreated[DelegationRecord] | None,
    ) -> tuple[DelegationRecord, bool]:
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert

        _, _, delegation_model, status_enum = self._models()
        with self._session_factory() as session, session.begin():
            # Never touches an existing row: a disconnected delegation stays disconnected.
            stmt = (
                insert(delegation_model)
                .values(id=uuid4(), user_id=user_id, agent_id=agent_id, status=status_enum.active)
                .on_conflict_do_nothing(
                    index_elements=[delegation_model.user_id, delegation_model.agent_id]
                )
                .returning(delegation_model.id)
            )
            created = session.execute(stmt).scalar_one_or_none() is not None
            row = session.execute(
                select(delegation_model).where(
                    delegation_model.user_id == user_id, delegation_model.agent_id == agent_id
                )
            ).scalar_one()
            record = self._delegation(row)
            if created and on_created is not None:
                anyio.from_thread.run(on_created, record)
        return record, created

    async def ensure_user(
        self,
        workos_user_id: str,
        *,
        email: str | None,
        display_name: str | None,
        on_created: OnCreated[UserRecord] | None = None,
    ) -> tuple[UserRecord, bool]:
        return await anyio.to_thread.run_sync(
            self._ensure_user, workos_user_id, email, display_name, on_created
        )

    async def ensure_agent(self, oauth_client_id: str) -> tuple[AgentRecord, bool]:
        return await anyio.to_thread.run_sync(self._ensure_agent, oauth_client_id)

    async def ensure_delegation(
        self,
        user_id: UUID,
        agent_id: UUID,
        *,
        on_created: OnCreated[DelegationRecord] | None = None,
    ) -> tuple[DelegationRecord, bool]:
        return await anyio.to_thread.run_sync(
            self._ensure_delegation, user_id, agent_id, on_created
        )
