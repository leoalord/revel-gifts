"""Persistence boundary for identity resolution (users, agents, delegations).

F3 owns the models and migrations (``revel.db.models``: ``User``, ``Agent``,
``AgentDelegation``; ``revel.db.enums.DelegationStatus``) and sessions
(``revel.db.session``). This module defines the narrow interface the resolver
needs plus the SQLAlchemy implementation over F3's models.

Every ``ensure_*`` method is an insert-if-absent that never updates an existing
row. ``on_created`` runs inside the inserting transaction, so if it raises the
insert is rolled back and a retry will create the row (and call it) again. See
``revel.auth.hooks`` for the hook contract (idempotent, external-only).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar
from uuid import UUID

import anyio
import anyio.from_thread
import anyio.to_thread
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from revel.db.enums import DelegationStatus
from revel.db.models import Agent, AgentDelegation, User

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
    if value not in (DelegationStatus.ACTIVE.value, DelegationStatus.DISCONNECTED.value):
        raise ValueError("unknown delegation status")
    return value  # type: ignore[return-value]


def _user(row: User) -> UserRecord:
    return UserRecord(
        id=row.id,
        workos_user_id=row.workos_user_id,
        email=row.email,
        display_name=row.display_name,
    )


def _agent(row: Agent) -> AgentRecord:
    return AgentRecord(id=row.id, oauth_client_id=row.oauth_client_id, name=row.name)


def _delegation(row: AgentDelegation) -> DelegationRecord:
    return DelegationRecord(
        id=row.id, user_id=row.user_id, agent_id=row.agent_id, status=_status_value(row.status)
    )


def _default_sessionmaker() -> sessionmaker[Session]:
    from revel.db.session import get_sessionmaker

    return get_sessionmaker()


class SqlAlchemyIdentityRepository:
    """PostgreSQL implementation over F3's models.

    Each ``ensure_*`` reads first (no write lock or WAL on the common path). Only
    when the row is absent does it run ``INSERT ... ON CONFLICT DO NOTHING
    RETURNING`` and re-read, which stays correct under concurrent first calls.

    Uses synchronous SQLAlchemy sessions (``revel.db.session``) in worker threads:
    the locked dependencies do not include ``greenlet``, which SQLAlchemy's asyncio
    extension requires. ``on_created`` hooks are async and run on the event loop
    (via ``anyio.from_thread``) while the inserting transaction is still open.
    """

    def __init__(self, sessionmaker_provider: Callable[[], sessionmaker[Session]] | None = None):
        """``sessionmaker_provider`` defaults to ``revel.db.session.get_sessionmaker``.

        It is resolved on first use, so building the app creates no engine.
        """
        self._provider = sessionmaker_provider or _default_sessionmaker
        self._sessionmaker: sessionmaker[Session] | None = None

    def _session(self) -> Session:
        if self._sessionmaker is None:
            self._sessionmaker = self._provider()
        return self._sessionmaker()

    # --- reads ---------------------------------------------------------------------

    def _get_user_by_workos_id(self, workos_user_id: str) -> UserRecord | None:
        with self._session() as session:
            row = session.execute(
                select(User).where(User.workos_user_id == workos_user_id)
            ).scalar_one_or_none()
            return _user(row) if row is not None else None

    def _get_user(self, user_id: UUID) -> UserRecord | None:
        with self._session() as session:
            row = session.get(User, user_id)
            return _user(row) if row is not None else None

    def _get_agent(self, agent_id: UUID) -> AgentRecord | None:
        with self._session() as session:
            row = session.get(Agent, agent_id)
            return _agent(row) if row is not None else None

    def _get_agent_by_client_id(self, oauth_client_id: str) -> AgentRecord | None:
        with self._session() as session:
            row = session.execute(
                select(Agent).where(Agent.oauth_client_id == oauth_client_id)
            ).scalar_one_or_none()
            return _agent(row) if row is not None else None

    def _get_delegation(self, user_id: UUID, agent_id: UUID) -> DelegationRecord | None:
        with self._session() as session:
            row = session.execute(
                select(AgentDelegation).where(
                    AgentDelegation.user_id == user_id, AgentDelegation.agent_id == agent_id
                )
            ).scalar_one_or_none()
            return _delegation(row) if row is not None else None

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
        existing = self._get_user_by_workos_id(workos_user_id)
        if existing is not None:
            return existing, False
        with self._session() as session, session.begin():
            stmt = (
                insert(User)
                .values(workos_user_id=workos_user_id, email=email, display_name=display_name)
                .on_conflict_do_nothing(index_elements=[User.workos_user_id])
                .returning(User.id)
            )
            created = session.execute(stmt).scalar_one_or_none() is not None
            row = session.execute(
                select(User).where(User.workos_user_id == workos_user_id)
            ).scalar_one()
            record = _user(row)
            if created and on_created is not None:
                anyio.from_thread.run(on_created, record)
        return record, created

    def _ensure_agent(self, oauth_client_id: str) -> tuple[AgentRecord, bool]:
        existing = self._get_agent_by_client_id(oauth_client_id)
        if existing is not None:
            return existing, False
        with self._session() as session, session.begin():
            stmt = (
                insert(Agent)
                .values(oauth_client_id=oauth_client_id)
                .on_conflict_do_nothing(index_elements=[Agent.oauth_client_id])
                .returning(Agent.id)
            )
            created = session.execute(stmt).scalar_one_or_none() is not None
            row = session.execute(
                select(Agent).where(Agent.oauth_client_id == oauth_client_id)
            ).scalar_one()
            return _agent(row), created

    def _ensure_delegation(
        self,
        user_id: UUID,
        agent_id: UUID,
        on_created: OnCreated[DelegationRecord] | None,
    ) -> tuple[DelegationRecord, bool]:
        existing = self._get_delegation(user_id, agent_id)
        if existing is not None:
            return existing, False
        with self._session() as session, session.begin():
            # Never touches an existing row: a disconnected delegation stays disconnected.
            stmt = (
                insert(AgentDelegation)
                .values(user_id=user_id, agent_id=agent_id, status=DelegationStatus.ACTIVE.value)
                .on_conflict_do_nothing(
                    index_elements=[AgentDelegation.user_id, AgentDelegation.agent_id]
                )
                .returning(AgentDelegation.id)
            )
            created = session.execute(stmt).scalar_one_or_none() is not None
            row = session.execute(
                select(AgentDelegation).where(
                    AgentDelegation.user_id == user_id, AgentDelegation.agent_id == agent_id
                )
            ).scalar_one()
            record = _delegation(row)
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
