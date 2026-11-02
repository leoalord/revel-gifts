"""SqlAlchemyIdentityRepository against real PostgreSQL with F3's Alembic migrations.

Runs only when ``REVEL_TEST_DATABASE_URL`` points at a disposable PostgreSQL
database (see backend/README.md "Database migrations"). The database is reset to
Alembic base and upgraded to head once per session; tables are truncated per test.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp.server.auth.provider import AccessToken
from sqlalchemy import Engine, create_engine, func, pool, select, text, update
from sqlalchemy.orm import sessionmaker

from revel.auth.identity import DelegationDisconnected, IdentityResolver
from revel.auth.repository import SqlAlchemyIdentityRepository
from revel.db.enums import DelegationStatus
from revel.db.models import AgentDelegation, User

from .conftest import RecordingHooks

DATABASE_URL = os.environ.get("REVEL_TEST_DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not DATABASE_URL, reason="REVEL_TEST_DATABASE_URL not set"),
]


@pytest.fixture(scope="module")
def migrated_engine() -> Iterator[Engine]:
    from alembic import command
    from alembic.config import Config

    engine = create_engine(DATABASE_URL, poolclass=pool.NullPool)
    for action, target in ((command.downgrade, "base"), (command.upgrade, "head")):
        config = Config(str(ALEMBIC_INI))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            action(config, target)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def sql_repo(migrated_engine: Engine) -> SqlAlchemyIdentityRepository:
    with migrated_engine.begin() as conn:
        conn.execute(text("TRUNCATE agent_delegations, agents, users"))
    # Pooled engine so concurrent worker threads get separate connections.
    engine = create_engine(DATABASE_URL, pool_size=10)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return SqlAlchemyIdentityRepository(lambda: factory)


def _token(sub: str = "user_alice", client_id: str = "client_muse") -> AccessToken:
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
    assert bob.agent_id == first.agent_id
    assert bob.delegation_id != first.delegation_id


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
    with sql_repo._session() as session, session.begin():
        session.execute(
            update(AgentDelegation)
            .where(AgentDelegation.id == principal.delegation_id)
            .values(
                status=DelegationStatus.DISCONNECTED.value,
                disconnected_at=func.now(),
                updated_at=func.now(),
            )
        )
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(_token(), via="mcp")
    with sql_repo._session() as session:
        rows = session.execute(select(AgentDelegation)).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == DelegationStatus.DISCONNECTED.value
        assert rows[0].disconnected_at is not None
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


async def test_existing_rows_take_read_only_path(sql_repo, monkeypatch):
    resolver = IdentityResolver(sql_repo, hooks=RecordingHooks())
    await resolver.resolve(_token(), via="mcp")

    def no_insert(*args, **kwargs):
        raise AssertionError("insert path used for existing rows")

    # The insert path is the only place that opens a write transaction (session.begin()).
    real_session = sql_repo._session

    def guarded_session():
        session = real_session()
        session.begin = no_insert  # type: ignore[method-assign]
        return session

    monkeypatch.setattr(sql_repo, "_session", guarded_session)
    principal = await resolver.resolve(_token(), via="api")
    with real_session() as session:
        assert session.get(User, principal.user_id) is not None
