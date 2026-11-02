"""SqlAlchemyIdentityRepository against real PostgreSQL.

Runs only when ``REVEL_TEST_DATABASE_URL`` points at a disposable PostgreSQL
database (e.g. ``postgresql+psycopg://postgres@127.0.0.1:55432/revel_test``).
Until F3's models (#3) merge, it uses test-local stand-in tables matching the
agreed schema; after #3 it should switch to ``revel.db.models`` + migrations.
"""

from __future__ import annotations

import asyncio
import enum
import os
import uuid
from datetime import datetime

import pytest
from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    create_engine,
    func,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from revel.auth.identity import DelegationDisconnected, IdentityResolver
from revel.auth.repository import SqlAlchemyIdentityRepository

from .conftest import RecordingHooks

DATABASE_URL = os.environ.get("REVEL_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not DATABASE_URL, reason="REVEL_TEST_DATABASE_URL not set"),
]


class Base(DeclarativeBase):
    pass


class DelegationStatus(enum.StrEnum):
    active = "active"
    disconnected = "disconnected"


class User(Base):
    __tablename__ = "f2_test_users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workos_user_id: Mapped[str] = mapped_column(String, unique=True)
    email: Mapped[str | None]
    display_name: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Agent(Base):
    __tablename__ = "f2_test_agents"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    oauth_client_id: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentDelegation(Base):
    __tablename__ = "f2_test_agent_delegations"
    __table_args__ = (UniqueConstraint("user_id", "agent_id"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("f2_test_users.id"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("f2_test_agents.id"))
    status: Mapped[DelegationStatus] = mapped_column(
        Enum(DelegationStatus, name="f2_test_delegation_status"), default=DelegationStatus.active
    )
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


MODELS = (User, Agent, AgentDelegation, DelegationStatus)


@pytest.fixture
def sql_repo():
    engine = create_engine(DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield SqlAlchemyIdentityRepository(sessionmaker(engine, expire_on_commit=False), models=MODELS)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _token(sub="user_alice", client_id="client_muse"):
    from mcp.server.auth.provider import AccessToken

    return AccessToken(
        token="opaque", client_id=client_id, scopes=[], subject=sub, claims={"iss": "x"}
    )


async def test_insert_if_absent_and_hooks_once(sql_repo):
    hooks = RecordingHooks()
    resolver = IdentityResolver(sql_repo, hooks=hooks)
    first = await resolver.resolve(_token(), via="mcp")
    second = await resolver.resolve(_token(), via="api")
    assert (first.user_id, first.agent_id, first.delegation_id) == (
        second.user_id,
        second.agent_id,
        second.delegation_id,
    )
    assert [name for name, _ in hooks.calls] == ["on_user_created", "on_agent_delegated"]

    bob = await resolver.resolve(_token(sub="user_bob"), via="mcp")
    assert bob.agent_id == first.agent_id and bob.delegation_id != first.delegation_id


async def test_concurrent_first_calls_create_once(sql_repo):
    hooks = RecordingHooks()
    resolver = IdentityResolver(sql_repo, hooks=hooks)
    principals = await asyncio.gather(*(resolver.resolve(_token(), via="mcp") for _ in range(8)))
    assert len({(p.user_id, p.agent_id, p.delegation_id) for p in principals}) == 1
    assert [name for name, _ in hooks.calls] == ["on_user_created", "on_agent_delegated"]


async def test_disconnected_delegation_stays_disconnected(sql_repo):
    hooks = RecordingHooks()
    resolver = IdentityResolver(sql_repo, hooks=hooks)
    principal = await resolver.resolve(_token(), via="mcp")
    with sql_repo._session_factory() as session, session.begin():
        session.execute(
            update(AgentDelegation)
            .where(AgentDelegation.id == principal.delegation_id)
            .values(status=DelegationStatus.disconnected, disconnected_at=func.now())
        )
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(_token(), via="mcp")
    with sql_repo._session_factory() as session:
        row = session.get(AgentDelegation, principal.delegation_id)
        assert row.status == DelegationStatus.disconnected
    assert len(hooks.calls) == 2


async def test_hook_failure_rolls_back_insert(sql_repo):
    hooks = RecordingHooks()
    hooks.fail_next_user_hook = True
    resolver = IdentityResolver(sql_repo, hooks=hooks)
    with pytest.raises(RuntimeError):
        await resolver.resolve(_token(), via="mcp")
    assert await sql_repo.get_user_by_workos_id("user_alice") is None
    await resolver.resolve(_token(), via="mcp")
    assert await sql_repo.get_user_by_workos_id("user_alice") is not None
