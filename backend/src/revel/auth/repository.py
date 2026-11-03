"""Persistence boundary for identity resolution (users, agents, delegations).

F3 owns the models and migrations (``revel.db.models``: ``User``, ``Agent``,
``AgentDelegation``; ``revel.db.enums.DelegationStatus``) and sessions
(``revel.db.session``). This module defines the narrow interface the resolver
needs plus the SQLAlchemy implementation over F3's models.

Every ``ensure_*``/``create_*`` method is an insert-if-absent that never updates an
existing row. ``on_created`` runs inside the inserting transaction, so if it raises the
insert is rolled back and a retry will create the row (and call it) again. See
``revel.auth.hooks`` for the hook contract (idempotent, external-only).

Reconnect (F2 decision on the Features record): a user may hold several delegations for one
assistant over time, at most one ``active`` (partial unique index on ``(user_id, agent_id)
WHERE status = 'active'``). Disconnected rows are history and are never modified again. Each
delegation remembers the WorkOS ``sid`` values its tokens carried (``authorized_sid`` on the
row, set by the inserting statement, plus ``agent_delegation_sessions`` rows recorded later),
so the resolver can refuse tokens of a disconnected authorization. F3's migration
``0004_delegation_reconnect`` and models (``AgentDelegation.authorized_sid``,
``AgentDelegationSession``) define that schema.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, TypeVar
from uuid import UUID

import anyio
import anyio.from_thread
import anyio.to_thread
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from revel.db.enums import DelegationStatus
from revel.db.models import Agent, AgentDelegation, AgentDelegationSession, User

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


@dataclass(frozen=True)
class DelegationView:
    """A delegation joined with its agent, for listing a user's connected assistants."""

    id: UUID
    user_id: UUID
    status: DelegationStatusValue
    connected_at: datetime
    disconnected_at: datetime | None
    agent: AgentRecord


@dataclass(frozen=True)
class DelegationState:
    """What the resolver needs about one (user, assistant) pair, read in one session.

    ``active`` is the pair's active delegation, if any. ``sid_on_active`` says whether the
    token's ``sid`` is already associated with it; ``sid_on_disconnected`` whether that ``sid``
    belongs to any disconnected delegation of the pair. ``last_disconnected_at`` is the pair's
    latest disconnect (``None`` when it has no disconnected history).
    """

    active: DelegationRecord | None
    sid_on_active: bool
    sid_on_disconnected: bool
    last_disconnected_at: datetime | None


R = TypeVar("R")
OnCreated = Callable[[R], Awaitable[None]]
OnDisconnected = Callable[[DelegationRecord], Awaitable[None]]


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

    async def set_agent_name_if_empty(self, agent_id: UUID, name: str) -> bool: ...

    async def get_delegation_state(
        self, user_id: UUID, agent_id: UUID, sid: str | None
    ) -> DelegationState: ...

    async def create_delegation(
        self,
        user_id: UUID,
        agent_id: UUID,
        *,
        authorized_sid: str | None,
        on_created: OnCreated[DelegationRecord] | None = None,
    ) -> tuple[DelegationRecord | None, bool]: ...

    async def record_delegation_sid(self, delegation_id: UUID, sid: str) -> None: ...

    async def disconnect_delegation(
        self,
        *,
        user_id: UUID,
        delegation_id: UUID,
        on_disconnected: OnDisconnected | None = None,
    ) -> bool: ...

    async def get_user(self, user_id: UUID) -> UserRecord | None: ...

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None: ...

    async def get_agent_by_client_id(self, oauth_client_id: str) -> AgentRecord | None: ...

    async def list_delegations_for_user(self, user_id: UUID) -> list[DelegationView]: ...

    async def get_delegation_view(
        self, user_id: UUID, delegation_id: UUID
    ) -> DelegationView | None: ...


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


def _delegation_view(row: AgentDelegation, agent: Agent) -> DelegationView:
    return DelegationView(
        id=row.id,
        user_id=row.user_id,
        status=_status_value(row.status),
        connected_at=row.connected_at,
        disconnected_at=row.disconnected_at,
        agent=_agent(agent),
    )


# Predicate of F3's partial unique index ``uq_agent_delegations_user_id_agent_id_active``.
_ACTIVE_WHERE = text("status = 'active'")


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

    def _get_delegation_state(
        self, user_id: UUID, agent_id: UUID, sid: str | None
    ) -> DelegationState:
        # Plain reads (no explicit transaction): the common path takes no write lock.
        with self._session() as session:
            rows = session.execute(
                select(
                    AgentDelegation.id,
                    AgentDelegation.status,
                    AgentDelegation.disconnected_at,
                    AgentDelegation.authorized_sid,
                ).where(AgentDelegation.user_id == user_id, AgentDelegation.agent_id == agent_id)
            ).all()
            sid_rows: set[UUID] = set()
            if sid is not None and rows:
                sid_rows = set(
                    session.execute(
                        select(AgentDelegationSession.delegation_id).where(
                            AgentDelegationSession.sid == sid,
                            AgentDelegationSession.delegation_id.in_([r.id for r in rows]),
                        )
                    ).scalars()
                )
        active: DelegationRecord | None = None
        sid_on_active = sid_on_disconnected = False
        last_disconnected_at: datetime | None = None
        for row in rows:
            status = _status_value(row.status)
            has_sid = sid is not None and (row.authorized_sid == sid or row.id in sid_rows)
            if status == DelegationStatus.ACTIVE.value:
                active = DelegationRecord(row.id, user_id, agent_id, status)
                sid_on_active = has_sid
                continue
            sid_on_disconnected = sid_on_disconnected or has_sid
            if row.disconnected_at is not None and (
                last_disconnected_at is None or row.disconnected_at > last_disconnected_at
            ):
                last_disconnected_at = row.disconnected_at
        return DelegationState(active, sid_on_active, sid_on_disconnected, last_disconnected_at)

    async def get_delegation_state(
        self, user_id: UUID, agent_id: UUID, sid: str | None
    ) -> DelegationState:
        return await anyio.to_thread.run_sync(self._get_delegation_state, user_id, agent_id, sid)

    def _delegation_views(
        self, user_id: UUID, delegation_id: UUID | None = None
    ) -> list[DelegationView]:
        with self._session() as session, session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            stmt = (
                select(AgentDelegation, Agent)
                .join(Agent, Agent.id == AgentDelegation.agent_id)
                .where(AgentDelegation.user_id == user_id)
                .order_by(AgentDelegation.connected_at.desc(), AgentDelegation.id.desc())
            )
            if delegation_id is not None:
                stmt = stmt.where(AgentDelegation.id == delegation_id)
            return [_delegation_view(row, agent) for row, agent in session.execute(stmt).all()]

    def _get_delegation_view(self, user_id: UUID, delegation_id: UUID) -> DelegationView | None:
        views = self._delegation_views(user_id, delegation_id)
        return views[0] if views else None

    async def list_delegations_for_user(self, user_id: UUID) -> list[DelegationView]:
        """All of ``user_id``'s delegations (any status), newest ``connected_at`` first."""
        return await anyio.to_thread.run_sync(self._delegation_views, user_id)

    async def get_delegation_view(
        self, user_id: UUID, delegation_id: UUID
    ) -> DelegationView | None:
        """``delegation_id`` if it belongs to ``user_id`` (any status), else ``None``."""
        return await anyio.to_thread.run_sync(self._get_delegation_view, user_id, delegation_id)

    async def get_user_by_workos_id(self, workos_user_id: str) -> UserRecord | None:
        return await anyio.to_thread.run_sync(self._get_user_by_workos_id, workos_user_id)

    async def get_user(self, user_id: UUID) -> UserRecord | None:
        return await anyio.to_thread.run_sync(self._get_user, user_id)

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        return await anyio.to_thread.run_sync(self._get_agent, agent_id)

    async def get_agent_by_client_id(self, oauth_client_id: str) -> AgentRecord | None:
        return await anyio.to_thread.run_sync(self._get_agent_by_client_id, oauth_client_id)

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

    def _create_delegation(
        self,
        user_id: UUID,
        agent_id: UUID,
        authorized_sid: str | None,
        on_created: OnCreated[DelegationRecord] | None,
    ) -> tuple[DelegationRecord | None, bool]:
        with self._session() as session, session.begin():
            # Writes only ``agent_delegations`` (F4 rule) and never touches an existing row: a
            # disconnected delegation stays disconnected. A concurrent insert for the same pair
            # loses on the partial unique index (one active delegation per user and assistant),
            # waits for the winner to commit, then reads the winner's row.
            stmt = (
                insert(AgentDelegation)
                .values(
                    user_id=user_id,
                    agent_id=agent_id,
                    status=DelegationStatus.ACTIVE.value,
                    authorized_sid=authorized_sid,
                )
                .on_conflict_do_nothing(
                    index_elements=[AgentDelegation.user_id, AgentDelegation.agent_id],
                    index_where=_ACTIVE_WHERE,
                )
                .returning(AgentDelegation.id)
            )
            created = session.execute(stmt).scalar_one_or_none() is not None
            row_id = session.execute(
                select(AgentDelegation.id).where(
                    AgentDelegation.user_id == user_id,
                    AgentDelegation.agent_id == agent_id,
                    AgentDelegation.status == DelegationStatus.ACTIVE.value,
                )
            ).scalar_one_or_none()
            if row_id is None:
                # The concurrent winner was already disconnected again; nothing to act through.
                return None, False
            record = DelegationRecord(row_id, user_id, agent_id, "active")
            if created and on_created is not None:
                anyio.from_thread.run(on_created, record)
        return record, created

    def _record_delegation_sid(self, delegation_id: UUID, sid: str) -> None:
        # Its own transaction, writing only ``agent_delegation_sessions`` (never combined with
        # a delegation write). Idempotent.
        with self._session() as session, session.begin():
            session.execute(
                insert(AgentDelegationSession)
                .values(delegation_id=delegation_id, sid=sid)
                .on_conflict_do_nothing(
                    index_elements=[
                        AgentDelegationSession.delegation_id,
                        AgentDelegationSession.sid,
                    ]
                )
            )

    def _disconnect_delegation(
        self,
        user_id: UUID,
        delegation_id: UUID,
        on_disconnected: OnDisconnected | None,
    ) -> bool:
        with self._session() as session, session.begin():
            # Only an active row owned by ``user_id`` changes. The row lock is held until
            # commit (including while the hook runs), so a concurrent disconnect waits and
            # then matches nothing. Sets disconnected_at with the status (F3's CHECK).
            stmt = (
                update(AgentDelegation)
                .where(
                    AgentDelegation.id == delegation_id,
                    AgentDelegation.user_id == user_id,
                    AgentDelegation.status == DelegationStatus.ACTIVE.value,
                )
                .values(
                    status=DelegationStatus.DISCONNECTED.value,
                    disconnected_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(AgentDelegation.agent_id)
                .execution_options(synchronize_session=False)
            )
            agent_id = session.execute(stmt).scalar_one_or_none()
            if agent_id is None:
                return False
            if on_disconnected is not None:
                record = DelegationRecord(
                    id=delegation_id, user_id=user_id, agent_id=agent_id, status="disconnected"
                )
                # A raise here rolls the update back and propagates.
                anyio.from_thread.run(on_disconnected, record)
        return True

    async def disconnect_delegation(
        self,
        *,
        user_id: UUID,
        delegation_id: UUID,
        on_disconnected: OnDisconnected | None = None,
    ) -> bool:
        """Disconnect an active delegation owned by ``user_id``.

        Returns ``True`` if this call disconnected it; ``False`` if it is already
        disconnected, owned by someone else, or unknown (no hook runs then).
        ``on_disconnected`` runs inside the transaction; if it raises, the row stays active.
        """
        return await anyio.to_thread.run_sync(
            self._disconnect_delegation, user_id, delegation_id, on_disconnected
        )

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

    # --- agent name ------------------------------------------------------------------

    def _set_agent_name_if_empty(self, agent_id: UUID, name: str) -> bool:
        # Its own transaction touching only ``agents`` (never combined with delegation
        # writes). The WHERE clause makes it first-writer-wins and idempotent: a
        # non-empty name is never overwritten, and concurrent callers serialize on the
        # row lock, after which the loser matches nothing.
        with self._session() as session, session.begin():
            stmt = (
                update(Agent)
                .where(Agent.id == agent_id, or_(Agent.name.is_(None), Agent.name == ""))
                .values(name=name, updated_at=func.now())
                .returning(Agent.id)
                .execution_options(synchronize_session=False)
            )
            return session.execute(stmt).scalar_one_or_none() is not None

    async def set_agent_name_if_empty(self, agent_id: UUID, name: str) -> bool:
        """Set ``agents.name`` only if it is NULL or empty. Returns whether it changed.

        Callers pass an already-sanitized, non-empty name.
        """
        if not name:
            raise ValueError("name must be non-empty")
        return await anyio.to_thread.run_sync(self._set_agent_name_if_empty, agent_id, name)

    async def create_delegation(
        self,
        user_id: UUID,
        agent_id: UUID,
        *,
        authorized_sid: str | None,
        on_created: OnCreated[DelegationRecord] | None = None,
    ) -> tuple[DelegationRecord | None, bool]:
        """Insert an active delegation for the pair unless one is already active.

        Returns ``(record, created)`` for the pair's active delegation (this call's row or a
        concurrent winner's), or ``(None, False)`` when no active row remains to read.
        ``on_created`` runs in the inserting transaction, only when this call inserted.
        """
        return await anyio.to_thread.run_sync(
            self._create_delegation, user_id, agent_id, authorized_sid, on_created
        )

    async def record_delegation_sid(self, delegation_id: UUID, sid: str) -> None:
        """Remember that ``sid`` acted through ``delegation_id`` (insert-if-absent)."""
        if not sid:
            raise ValueError("sid must be non-empty")
        await anyio.to_thread.run_sync(self._record_delegation_sid, delegation_id, sid)
