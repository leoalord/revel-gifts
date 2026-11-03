"""0004_delegation_reconnect against a real PostgreSQL (REVEL_TEST_DATABASE_URL).

One ACTIVE delegation per (user, agent) via a partial unique index, disconnected rows kept as
history, the remembered WorkOS sids (``authorized_sid`` and ``agent_delegation_sessions``), the
downgrade guard against reconnect history, and the migration's lock_timeout.
"""

import io
import uuid
from collections.abc import Callable, Iterator

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, delete, inspect, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from revel.db.enums import DelegationStatus
from revel.db.models import Agent, AgentDelegation, AgentDelegationSession, User
from revel.settings import get_settings

RunAlembic = Callable[..., None]
SESSIONS = "agent_delegation_sessions"
UQ_ACTIVE = "uq_agent_delegations_user_id_agent_id_active"
OLD_UQ = "uq_agent_delegations_user_id_agent_id"
HEAD = "0005_connection_owner_confirmed"


@pytest.fixture
def conn(migrated_engine: Engine) -> Iterator[Connection]:
    """Connection whose work is rolled back after each test."""
    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _user(conn: Connection) -> uuid.UUID:
    return conn.execute(
        insert(User).values(workos_user_id=f"user_{uuid.uuid4().hex}").returning(User.id)
    ).scalar_one()


def _agent(conn: Connection) -> uuid.UUID:
    return conn.execute(
        insert(Agent).values(oauth_client_id=f"client_{uuid.uuid4().hex}").returning(Agent.id)
    ).scalar_one()


def _delegation(
    conn: Connection, user_id: uuid.UUID, agent_id: uuid.UUID, *, active: bool = True, **values
) -> uuid.UUID:
    if not active:
        values |= {
            "status": DelegationStatus.DISCONNECTED.value,
            "disconnected_at": text("now()"),
        }
    return conn.execute(
        insert(AgentDelegation)
        .values(user_id=user_id, agent_id=agent_id, **values)
        .returning(AgentDelegation.id)
    ).scalar_one()


def _revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


# --- schema ----------------------------------------------------------------------------------


def test_sessions_table_schema(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    columns = {c["name"]: c for c in inspector.get_columns(SESSIONS)}
    assert set(columns) == {"delegation_id", "sid", "created_at"}
    assert not any(c["nullable"] for c in columns.values())
    assert columns["created_at"]["type"].timezone and "now()" in columns["created_at"]["default"]
    pk = inspector.get_pk_constraint(SESSIONS)
    assert pk["name"] == "pk_agent_delegation_sessions"
    assert pk["constrained_columns"] == ["delegation_id", "sid"]
    fks = inspector.get_foreign_keys(SESSIONS)
    assert [
        (fk["name"], fk["constrained_columns"], fk["referred_table"], fk["referred_columns"])
        for fk in fks
    ] == [
        (
            "fk_agent_delegation_sessions_delegation_id_agent_delegations",
            ["delegation_id"],
            "agent_delegations",
            ["id"],
        )
    ]
    assert fks[0]["options"].get("ondelete") == "RESTRICT"
    assert {c["name"] for c in inspector.get_check_constraints(SESSIONS)} == {
        "ck_agent_delegation_sessions_sid_not_empty"
    }
    assert inspector.get_unique_constraints(SESSIONS) == []


# --- one active delegation per (user, agent) ------------------------------------------------


def test_disconnected_history_and_one_active_allowed(conn: Connection) -> None:
    user_id, agent_id = _user(conn), _agent(conn)
    _delegation(conn, user_id, agent_id, active=False)
    _delegation(conn, user_id, agent_id, active=False)
    _delegation(conn, user_id, agent_id)
    statuses = conn.execute(
        select(AgentDelegation.status).where(
            AgentDelegation.user_id == user_id, AgentDelegation.agent_id == agent_id
        )
    ).scalars()
    assert sorted(statuses) == ["active", "disconnected", "disconnected"]


def test_two_active_rejected(conn: Connection) -> None:
    user_id, agent_id = _user(conn), _agent(conn)
    _delegation(conn, user_id, agent_id, active=False)
    _delegation(conn, user_id, agent_id)
    with pytest.raises(IntegrityError, match=UQ_ACTIVE), conn.begin_nested():
        _delegation(conn, user_id, agent_id)


def test_reactivating_old_row_rejected_while_another_is_active(conn: Connection) -> None:
    user_id, agent_id = _user(conn), _agent(conn)
    old = _delegation(conn, user_id, agent_id, active=False)
    _delegation(conn, user_id, agent_id)
    with pytest.raises(IntegrityError, match=UQ_ACTIVE), conn.begin_nested():
        conn.execute(
            AgentDelegation.__table__.update()
            .where(AgentDelegation.id == old)
            .values(status=DelegationStatus.ACTIVE.value, disconnected_at=None)
        )


def test_other_users_and_agents_independent(conn: Connection) -> None:
    user_a, user_b, agent_a, agent_b = _user(conn), _user(conn), _agent(conn), _agent(conn)
    for user_id in (user_a, user_b):
        for agent_id in (agent_a, agent_b):
            _delegation(conn, user_id, agent_id)


def test_authorized_sid(conn: Connection) -> None:
    user_id, agent_id = _user(conn), _agent(conn)
    delegation_id = _delegation(conn, user_id, agent_id, authorized_sid="session_01")
    stored = conn.execute(
        select(AgentDelegation.authorized_sid).where(AgentDelegation.id == delegation_id)
    ).scalar_one()
    assert stored == "session_01"
    _delegation(conn, _user(conn), agent_id)  # NULL allowed
    with (
        pytest.raises(IntegrityError, match="ck_agent_delegations_authorized_sid_not_empty"),
        conn.begin_nested(),
    ):
        _delegation(conn, _user(conn), agent_id, authorized_sid="")


# --- agent_delegation_sessions --------------------------------------------------------------


def _record_sid(conn: Connection, delegation_id: uuid.UUID, sid: str) -> bool:
    """Insert-if-absent on the primary key; True when a row was inserted."""
    inserted = conn.execute(
        insert(AgentDelegationSession)
        .values(delegation_id=delegation_id, sid=sid)
        .on_conflict_do_nothing(index_elements=["delegation_id", "sid"])
        .returning(AgentDelegationSession.sid)
    ).scalar_one_or_none()
    return inserted is not None


def test_sessions_pk_idempotent_insert(conn: Connection) -> None:
    delegation_id = _delegation(conn, _user(conn), _agent(conn))
    assert _record_sid(conn, delegation_id, "session_01")
    assert not _record_sid(conn, delegation_id, "session_01")  # ON CONFLICT on the PK
    assert _record_sid(conn, delegation_id, "session_02")
    with pytest.raises(IntegrityError, match="pk_agent_delegation_sessions"), conn.begin_nested():
        conn.execute(
            insert(AgentDelegationSession).values(delegation_id=delegation_id, sid="session_01")
        )
    rows = conn.execute(
        select(AgentDelegationSession.sid, AgentDelegationSession.created_at).where(
            AgentDelegationSession.delegation_id == delegation_id
        )
    ).all()
    assert sorted(r.sid for r in rows) == ["session_01", "session_02"]
    assert all(r.created_at.tzinfo is not None for r in rows)


def test_same_sid_on_several_delegations(conn: Connection) -> None:
    user_id, agent_id = _user(conn), _agent(conn)
    old = _delegation(conn, user_id, agent_id, active=False)
    new = _delegation(conn, user_id, agent_id)
    assert _record_sid(conn, old, "session_01")
    assert _record_sid(conn, new, "session_01")


def test_sessions_empty_sid_rejected(conn: Connection) -> None:
    delegation_id = _delegation(conn, _user(conn), _agent(conn))
    with (
        pytest.raises(IntegrityError, match="ck_agent_delegation_sessions_sid_not_empty"),
        conn.begin_nested(),
    ):
        _record_sid(conn, delegation_id, "")


def test_sessions_fk_requires_delegation(conn: Connection) -> None:
    with (
        pytest.raises(
            IntegrityError, match="fk_agent_delegation_sessions_delegation_id_agent_delegations"
        ),
        conn.begin_nested(),
    ):
        _record_sid(conn, uuid.uuid4(), "session_01")


def test_sessions_fk_restricts_delegation_delete(conn: Connection) -> None:
    delegation_id = _delegation(conn, _user(conn), _agent(conn))
    _record_sid(conn, delegation_id, "session_01")
    with (
        pytest.raises(
            IntegrityError, match="fk_agent_delegation_sessions_delegation_id_agent_delegations"
        ),
        conn.begin_nested(),
    ):
        conn.execute(delete(AgentDelegation).where(AgentDelegation.id == delegation_id))


# --- migration up/down ----------------------------------------------------------------------


def test_downgrade_to_0003_round_trip(migrated_engine: Engine, run_alembic: RunAlembic) -> None:
    run_alembic(command.downgrade, "0003_connections_questions")
    inspector = inspect(migrated_engine)
    assert SESSIONS not in inspector.get_table_names()
    assert "authorized_sid" not in {c["name"] for c in inspector.get_columns("agent_delegations")}
    assert {
        u["name"]: u["column_names"] for u in inspector.get_unique_constraints("agent_delegations")
    } == {OLD_UQ: ["user_id", "agent_id"], "uq_agent_delegations_id_user_id": ["id", "user_id"]}
    assert {
        ix["name"]
        for ix in inspector.get_indexes("agent_delegations")
        if "duplicates_constraint" not in ix  # skip UNIQUE constraints' backing indexes
    } == {"ix_agent_delegations_agent_id"}
    assert {c["name"] for c in inspector.get_check_constraints("agent_delegations")} == {
        "ck_agent_delegations_status",
        "ck_agent_delegations_disconnected_at_matches_status",
    }
    assert _revision(migrated_engine) == "0003_connections_questions"

    run_alembic(command.upgrade, "head")
    inspector = inspect(migrated_engine)
    assert SESSIONS in inspector.get_table_names()
    assert UQ_ACTIVE in {ix["name"] for ix in inspector.get_indexes("agent_delegations")}
    assert OLD_UQ not in {u["name"] for u in inspector.get_unique_constraints("agent_delegations")}
    assert _revision(migrated_engine) == HEAD


def test_upgrade_keeps_existing_delegations(
    migrated_engine: Engine, run_alembic: RunAlembic
) -> None:
    """Rows written under 0003 survive 0004 with authorized_sid NULL."""
    run_alembic(command.downgrade, "0003_connections_questions")
    with migrated_engine.begin() as connection:
        user_id, agent_id = _user(connection), _agent(connection)
        connection.execute(
            text(
                "INSERT INTO agent_delegations (user_id, agent_id, status, disconnected_at) "
                "VALUES (:u, :a, 'disconnected', now())"
            ),
            {"u": user_id, "a": agent_id},
        )
    try:
        run_alembic(command.upgrade, "head")
        with migrated_engine.connect() as connection:
            row = connection.execute(
                select(AgentDelegation.status, AgentDelegation.authorized_sid).where(
                    AgentDelegation.user_id == user_id
                )
            ).one()
        assert row.status == "disconnected" and row.authorized_sid is None
    finally:
        run_alembic(command.upgrade, "head")
        with migrated_engine.begin() as connection:
            connection.execute(delete(AgentDelegation).where(AgentDelegation.user_id == user_id))
            connection.execute(delete(User).where(User.id == user_id))
            connection.execute(delete(Agent).where(Agent.id == agent_id))


def test_downgrade_refuses_reconnect_history(
    migrated_engine: Engine, run_alembic: RunAlembic
) -> None:
    with migrated_engine.begin() as connection:
        user_id, agent_id = _user(connection), _agent(connection)
        old = _delegation(connection, user_id, agent_id, active=False)
        new = _delegation(connection, user_id, agent_id, authorized_sid="session_02")
        _record_sid(connection, new, "session_02")
    try:
        with pytest.raises(DBAPIError, match="Cannot downgrade 0004_delegation_reconnect"):
            run_alembic(command.downgrade, "0003_connections_questions")
        # Nothing changed (one transaction): still at head with both rows and the sessions table.
        assert _revision(migrated_engine) == HEAD
        assert SESSIONS in inspect(migrated_engine).get_table_names()
        with migrated_engine.connect() as connection:
            rows = connection.execute(
                select(AgentDelegation.id).where(AgentDelegation.user_id == user_id)
            ).all()
        assert len(rows) == 2

        # Once the history is resolved the downgrade proceeds.
        with migrated_engine.begin() as connection:
            connection.execute(
                delete(AgentDelegationSession).where(AgentDelegationSession.delegation_id == new)
            )
            connection.execute(delete(AgentDelegation).where(AgentDelegation.id == old))
        run_alembic(command.downgrade, "0003_connections_questions")
        assert _revision(migrated_engine) == "0003_connections_questions"
    finally:
        run_alembic(command.upgrade, "head")
        with migrated_engine.begin() as connection:
            connection.execute(
                delete(AgentDelegationSession).where(
                    AgentDelegationSession.delegation_id.in_([old, new])
                )
            )
            connection.execute(delete(AgentDelegation).where(AgentDelegation.user_id == user_id))
            connection.execute(delete(User).where(User.id == user_id))
            connection.execute(delete(Agent).where(Agent.id == agent_id))
    assert _revision(migrated_engine) == HEAD


def test_upgrade_fails_fast_on_lock_timeout(
    migrated_engine: Engine, run_alembic: RunAlembic
) -> None:
    """A concurrent SHARE lock (as the FGA reconcile job takes) makes 0004 fail, not queue."""
    run_alembic(command.downgrade, "0003_connections_questions")
    try:
        with migrated_engine.connect() as holder:
            holder.begin()
            holder.execute(text("LOCK TABLE agent_delegations IN SHARE MODE"))
            with pytest.raises(DBAPIError, match="lock timeout"):
                run_alembic(command.upgrade, "head")
            holder.rollback()
        assert _revision(migrated_engine) == "0003_connections_questions"
    finally:
        run_alembic(command.upgrade, "head")
    assert _revision(migrated_engine) == HEAD


def test_offline_sql_sets_lock_timeout_and_guard(
    monkeypatch: pytest.MonkeyPatch, alembic_config: Config
) -> None:
    """`alembic upgrade/downgrade --sql` carries the lock_timeout and the downgrade guard."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://revel@localhost/revel")
    get_settings.cache_clear()
    try:
        up = io.StringIO()
        alembic_config.output_buffer = up
        command.upgrade(alembic_config, "0003_connections_questions:head", sql=True)
        down = io.StringIO()
        alembic_config.output_buffer = down
        command.downgrade(alembic_config, "head:0003_connections_questions", sql=True)
    finally:
        get_settings.cache_clear()
    up_sql, down_sql = up.getvalue(), down.getvalue()
    assert up_sql.index("SET LOCAL lock_timeout = '5s'") < up_sql.index("ALTER TABLE")
    assert down_sql.index("SET LOCAL lock_timeout = '5s'") < down_sql.index("RAISE EXCEPTION")
    assert down_sql.index("RAISE EXCEPTION") < down_sql.index("DROP TABLE")
