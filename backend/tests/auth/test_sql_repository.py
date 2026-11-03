"""SqlAlchemyIdentityRepository against real PostgreSQL with F3's Alembic migrations.

Runs only when ``REVEL_TEST_DATABASE_URL`` points at a disposable PostgreSQL
database (see backend/README.md "Database migrations"). The database is reset to
Alembic base and upgraded to head once per session; tables are truncated per test.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from mcp.server.auth.provider import AccessToken
from sqlalchemy import Engine, create_engine, func, pool, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from revel.auth.identity import DelegationDisconnected, IdentityResolver
from revel.auth.repository import SqlAlchemyIdentityRepository
from revel.db.enums import DelegationStatus
from revel.db.models import Agent, AgentDelegation, User

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
        conn.execute(text("TRUNCATE agent_delegations, agents, users CASCADE"))
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


async def test_browser_and_mcp_resolve_to_same_user_row(sql_repo):
    hooks = RecordingHooks()
    resolver = IdentityResolver(sql_repo, hooks=hooks)
    ui = await resolver.resolve_user("user_alice")
    mcp = await resolver.resolve(_token(), via="mcp")
    assert ui.user_id == mcp.user_id and ui.via == "ui" and ui.agent_id is None
    bob_ui, bob_mcp = await asyncio.gather(
        resolver.resolve_user("user_bob"), resolver.resolve(_token(sub="user_bob"), via="mcp")
    )
    assert bob_ui.user_id == bob_mcp.user_id
    assert [name for name, _ in hooks.calls].count("on_user_created") == 2


# --- disconnect_delegation (#14) ---------------------------------------------------------


def _delegation_row(sql_repo, delegation_id):
    with sql_repo._session() as session:
        row = session.get(AgentDelegation, delegation_id)
        return row.status, row.disconnected_at, row.updated_at


async def test_disconnect_delegation_sql(sql_repo):
    hooks = RecordingHooks()
    resolver = IdentityResolver(sql_repo, hooks=hooks)
    p = await resolver.resolve(_token(), via="mcp")
    bob = await resolver.resolve(_token(sub="user_bob"), via="mcp")
    hooks.calls.clear()
    _, _, updated_before = _delegation_row(sql_repo, p.delegation_id)

    # Wrong owner and unknown id: no change, no hook.
    assert not await resolver.disconnect_delegation(
        user_id=bob.user_id, delegation_id=p.delegation_id
    )
    assert not await resolver.disconnect_delegation(user_id=p.user_id, delegation_id=uuid4())
    assert _delegation_row(sql_repo, p.delegation_id)[:2] == ("active", None)
    assert hooks.calls == []

    assert await resolver.disconnect_delegation(user_id=p.user_id, delegation_id=p.delegation_id)
    status, disconnected_at, updated_after = _delegation_row(sql_repo, p.delegation_id)
    assert status == "disconnected" and disconnected_at is not None
    assert updated_after >= updated_before
    assert hooks.calls == [
        (
            "on_agent_disconnected",
            {"user_id": p.user_id, "delegation_id": p.delegation_id, "agent_id": p.agent_id},
        )
    ]
    assert not await resolver.disconnect_delegation(
        user_id=p.user_id, delegation_id=p.delegation_id
    )
    assert len(hooks.calls) == 1

    # The first-call upsert never re-grants or reactivates it.
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(_token(), via="mcp")
    assert _delegation_row(sql_repo, p.delegation_id)[0] == "disconnected"
    assert len(hooks.calls) == 1
    # Bob's delegation is untouched.
    assert _delegation_row(sql_repo, bob.delegation_id)[0] == "active"


async def test_disconnect_delegation_sql_hook_failure_rolls_back(sql_repo, monkeypatch):
    from revel.auth import authz
    from revel.auth.fga import FgaConfigurationError

    monkeypatch.setattr(authz, "_state", None)  # real authz, OpenFGA unconfigured
    resolver = IdentityResolver(sql_repo)
    p = await resolver.resolve(_token(), via="mcp")
    with pytest.raises(FgaConfigurationError):
        await resolver.disconnect_delegation(user_id=p.user_id, delegation_id=p.delegation_id)
    assert _delegation_row(sql_repo, p.delegation_id)[:2] == ("active", None)
    # Still usable: the delegation was not disconnected.
    again = await resolver.resolve(_token(), via="mcp")
    assert again.delegation_id == p.delegation_id


async def test_concurrent_double_disconnect_sql(sql_repo):
    hooks = RecordingHooks()
    resolver = IdentityResolver(sql_repo, hooks=hooks)
    p = await resolver.resolve(_token(), via="mcp")
    hooks.calls.clear()
    results = await asyncio.gather(
        *(
            resolver.disconnect_delegation(user_id=p.user_id, delegation_id=p.delegation_id)
            for _ in range(6)
        )
    )
    assert sorted(results) == [False] * 5 + [True]
    assert [name for name, _ in hooks.calls] == ["on_agent_disconnected"]
    assert _delegation_row(sql_repo, p.delegation_id)[0] == "disconnected"


# --- delegation views (connected agents UI) ----------------------------------------------


async def test_delegation_views_sql(sql_repo):
    resolver = IdentityResolver(sql_repo, hooks=RecordingHooks())
    old = await resolver.resolve(_token(client_id="client_old"), via="mcp")
    new = await resolver.resolve(_token(client_id="client_new"), via="mcp")
    bob = await resolver.resolve(_token(sub="user_bob", client_id="client_new"), via="mcp")
    with sql_repo._session() as session, session.begin():
        session.execute(
            update(AgentDelegation)
            .where(AgentDelegation.id == old.delegation_id)
            .values(connected_at=func.now() - text("interval '1 day'"))
        )
    assert await resolver.disconnect_delegation(
        user_id=old.user_id, delegation_id=old.delegation_id
    )

    views = await sql_repo.list_delegations_for_user(old.user_id)
    assert [v.id for v in views] == [new.delegation_id, old.delegation_id]
    assert [v.status for v in views] == ["active", "disconnected"]
    assert views[0].disconnected_at is None and views[1].disconnected_at is not None
    assert views[0].agent.oauth_client_id == "client_new" and views[0].agent.name is None
    assert all(v.user_id == old.user_id for v in views)

    assert await sql_repo.get_delegation_view(old.user_id, old.delegation_id) == views[1]
    # Another user's delegation and unknown IDs read as absent.
    assert await sql_repo.get_delegation_view(old.user_id, bob.delegation_id) is None
    assert await sql_repo.get_delegation_view(old.user_id, uuid4()) is None
    bob_views = await sql_repo.list_delegations_for_user(bob.user_id)
    assert [v.id for v in bob_views] == [bob.delegation_id]


async def test_set_agent_name_if_empty_sql(sql_repo):
    agent, _ = await sql_repo.ensure_agent("client_named")
    session = sql_repo._session()
    with session:
        before = session.get(Agent, agent.id).updated_at
    assert await sql_repo.set_agent_name_if_empty(agent.id, "Muse") is True
    assert await sql_repo.set_agent_name_if_empty(agent.id, "Impostor") is False
    stored = await sql_repo.get_agent(agent.id)
    assert stored is not None and stored.name == "Muse"
    with sql_repo._session() as session:
        assert session.get(Agent, agent.id).updated_at >= before
    # Unknown agent: no-op.
    assert await sql_repo.set_agent_name_if_empty(uuid4(), "Muse") is False


async def test_set_agent_name_if_empty_treats_empty_string_as_unset_sql(sql_repo):
    agent, _ = await sql_repo.ensure_agent("client_blank")
    with sql_repo._session() as session, session.begin():
        session.execute(update(Agent).where(Agent.id == agent.id).values(name=""))
    assert await sql_repo.set_agent_name_if_empty(agent.id, "Claude") is True
    assert (await sql_repo.get_agent(agent.id)).name == "Claude"


async def test_concurrent_agent_name_writes_first_wins_sql(sql_repo):
    agent, _ = await sql_repo.ensure_agent("client_race")
    names = [f"Name {i}" for i in range(8)]
    results = await asyncio.gather(
        *(sql_repo.set_agent_name_if_empty(agent.id, name) for name in names)
    )
    assert sum(results) == 1
    winner = names[results.index(True)]
    assert (await sql_repo.get_agent(agent.id)).name == winner


async def test_resolve_captures_name_without_touching_delegations_sql(sql_repo):
    class Clients:
        async def fetch_client_name(self, client_id: str) -> str | None:
            return "Muse" if client_id == "client_muse" else None

    resolver = IdentityResolver(sql_repo, hooks=RecordingHooks(), client_directory=Clients())
    principal = await resolver.resolve(_token(), via="mcp")
    assert (await sql_repo.get_agent(principal.agent_id)).name == "Muse"
    with sql_repo._session() as session:
        row = session.get(AgentDelegation, principal.delegation_id)
        assert row.status == DelegationStatus.ACTIVE.value
        assert row.created_at == row.updated_at


# --- reconnect after disconnect (RG-10) ------------------------------------------------------
# Uses F3's migration 0004_delegation_reconnect: partial unique index
# uq_agent_delegations_user_id_agent_id_active, agent_delegations.authorized_sid and the
# agent_delegation_sessions table. Rows are read with plain SQL so the assertions check the
# stored schema, independent of the ORM mapping the repository uses.


def _sid_token(sid: str | None, iat: int | None = None, sub: str = "user_alice") -> AccessToken:
    claims: dict = {"iss": "x", "iat": int(time.time()) - 60 if iat is None else iat}
    if sid is not None:
        claims["sid"] = sid
    return AccessToken(
        token="opaque", client_id="client_muse", scopes=[], subject=sub, claims=claims
    )


def _later() -> int:
    return int(time.time()) + 5


def _full_row(sql_repo, delegation_id):
    with sql_repo._session() as session:
        return session.execute(
            text(
                "SELECT status, connected_at, disconnected_at, updated_at, created_at, "
                "authorized_sid FROM agent_delegations WHERE id = :id"
            ),
            {"id": delegation_id},
        ).one()


def _recorded_sids(sql_repo, delegation_id) -> set[str]:
    with sql_repo._session() as session:
        return set(
            session.execute(
                text("SELECT sid FROM agent_delegation_sessions WHERE delegation_id = :id"),
                {"id": delegation_id},
            ).scalars()
        )


async def test_reconnect_creates_new_delegation_sql(sql_repo):
    hooks = RecordingHooks()
    resolver = IdentityResolver(sql_repo, hooks=hooks)
    old = await resolver.resolve(_sid_token("app_consent_1"), via="mcp")
    assert _full_row(sql_repo, old.delegation_id).authorized_sid == "app_consent_1"
    assert await resolver.disconnect_delegation(
        user_id=old.user_id, delegation_id=old.delegation_id
    )
    old_row = _full_row(sql_repo, old.delegation_id)

    # Pre-disconnect token, refreshed old-session token, unseen pre-disconnect sid, no sid.
    for token in (
        _sid_token("app_consent_1"),
        _sid_token("app_consent_1", _later()),
        _sid_token("app_consent_unseen"),
        _sid_token(None, _later()),
    ):
        with pytest.raises(DelegationDisconnected):
            await resolver.resolve(token, via="mcp")

    new = await resolver.resolve(_sid_token("app_consent_2", _later()), via="mcp")
    assert new.delegation_id != old.delegation_id
    assert (new.user_id, new.agent_id) == (old.user_id, old.agent_id)
    assert [c["delegation_id"] for n, c in hooks.calls if n == "on_agent_delegated"] == [
        old.delegation_id,
        new.delegation_id,
    ]
    new_row = _full_row(sql_repo, new.delegation_id)
    assert new_row.status == "active" and new_row.authorized_sid == "app_consent_2"
    assert _recorded_sids(sql_repo, new.delegation_id) == set()  # stored on the row itself
    # The old row is history, unchanged.
    assert _full_row(sql_repo, old.delegation_id) == old_row
    # The refreshed old-session token cannot act through the new delegation either.
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(_sid_token("app_consent_1", _later()), via="api")
    again = await resolver.resolve(_sid_token("app_consent_2", _later()), via="api")
    assert again.delegation_id == new.delegation_id

    views = await sql_repo.list_delegations_for_user(old.user_id)
    assert [(v.id, v.status) for v in views] == [
        (new.delegation_id, "active"),
        (old.delegation_id, "disconnected"),
    ]

    # The new delegation disconnects like any other; the old one stays as it was.
    hooks.calls.clear()
    assert await resolver.disconnect_delegation(
        user_id=new.user_id, delegation_id=new.delegation_id
    )
    assert [n for n, _ in hooks.calls] == ["on_agent_disconnected"]
    assert _full_row(sql_repo, new.delegation_id).status == "disconnected"
    assert _full_row(sql_repo, old.delegation_id) == old_row
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(_sid_token("app_consent_2", _later()), via="mcp")


async def test_concurrent_fresh_authorizations_one_active_sql(sql_repo):
    hooks = RecordingHooks()
    resolver = IdentityResolver(sql_repo, hooks=hooks)
    old = await resolver.resolve(_sid_token("app_consent_1"), via="mcp")
    assert await resolver.disconnect_delegation(
        user_id=old.user_id, delegation_id=old.delegation_id
    )
    hooks.calls.clear()
    principals = await asyncio.gather(
        *(
            resolver.resolve(_sid_token(f"app_consent_{2 + i % 2}", _later()), via="mcp")
            for i in range(8)
        )
    )
    ids = {p.delegation_id for p in principals}
    assert len(ids) == 1 and old.delegation_id not in ids
    (new_id,) = ids
    with sql_repo._session() as session:
        rows = session.execute(
            text("SELECT id, status FROM agent_delegations WHERE user_id = :u"),
            {"u": old.user_id},
        ).all()
    assert sorted((str(i), s) for i, s in rows) == sorted(
        [(str(new_id), "active"), (str(old.delegation_id), "disconnected")]
    )
    assert [c["delegation_id"] for n, c in hooks.calls if n == "on_agent_delegated"] == [new_id]
    row = _full_row(sql_repo, new_id)
    assert {row.authorized_sid} | _recorded_sids(sql_repo, new_id) == {
        "app_consent_2",
        "app_consent_3",
    }


async def test_second_sid_recorded_and_refused_after_disconnect_sql(sql_repo):
    resolver = IdentityResolver(sql_repo, hooks=RecordingHooks())
    p = await resolver.resolve(_sid_token("app_consent_a"), via="mcp")
    q = await resolver.resolve(_sid_token("app_consent_b"), via="mcp")
    assert q.delegation_id == p.delegation_id
    assert _recorded_sids(sql_repo, p.delegation_id) == {"app_consent_b"}
    assert await resolver.disconnect_delegation(user_id=p.user_id, delegation_id=p.delegation_id)
    for sid in ("app_consent_a", "app_consent_b"):
        with pytest.raises(DelegationDisconnected):
            await resolver.resolve(_sid_token(sid, _later()), via="mcp")


async def test_one_active_delegation_per_pair_enforced_sql(sql_repo):
    resolver = IdentityResolver(sql_repo, hooks=RecordingHooks())
    p = await resolver.resolve(_sid_token("app_consent_1"), via="mcp")
    with (
        pytest.raises(IntegrityError, match="uq_agent_delegations_user_id_agent_id_active"),
        sql_repo._session() as session,
        session.begin(),
    ):
        session.execute(
            text("INSERT INTO agent_delegations (user_id, agent_id) VALUES (:u, :a)"),
            {"u": p.user_id, "a": p.agent_id},
        )
    # A disconnected row beside the active one is allowed (that is the history).
    with sql_repo._session() as session, session.begin():
        session.execute(
            text(
                "INSERT INTO agent_delegations (user_id, agent_id, status, disconnected_at) "
                "VALUES (:u, :a, 'disconnected', now())"
            ),
            {"u": p.user_id, "a": p.agent_id},
        )
