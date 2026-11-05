"""Migration and constraint behaviour against a real PostgreSQL (REVEL_TEST_DATABASE_URL)."""

import uuid
from collections.abc import Callable, Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, delete, inspect, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from revel.db.base import Base
from revel.db.enums import DelegationStatus
from revel.db.models import Agent, AgentDelegation, User

RunAlembic = Callable[..., None]
TABLES = {"users", "agents", "agent_delegations"}


@pytest.fixture
def conn(migrated_engine: Engine) -> Iterator[Connection]:
    """Connection whose work is rolled back after each test."""
    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _user(conn: Connection, workos_user_id: str | None = None) -> uuid.UUID:
    return conn.execute(
        insert(User)
        .values(workos_user_id=workos_user_id or f"user_{uuid.uuid4().hex}")
        .returning(User.id)
    ).scalar_one()


def _agent(conn: Connection) -> uuid.UUID:
    return conn.execute(
        insert(Agent).values(oauth_client_id=f"client_{uuid.uuid4().hex}").returning(Agent.id)
    ).scalar_one()


def _delegation(conn: Connection, user_id: uuid.UUID, agent_id: uuid.UUID, **values) -> None:
    conn.execute(insert(AgentDelegation).values(user_id=user_id, agent_id=agent_id, **values))


def test_schema_after_upgrade(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    assert set(inspector.get_table_names()) >= TABLES

    columns = {c["name"]: c for c in inspector.get_columns("users")}
    assert set(columns) == {
        "id",
        "workos_user_id",
        "email",
        "display_name",
        "created_at",
        "updated_at",
    }
    assert "gen_random_uuid()" in columns["id"]["default"]
    assert not columns["workos_user_id"]["nullable"] and columns["email"]["nullable"]
    assert {c["name"] for c in inspector.get_columns("agents")} == {
        "id",
        "oauth_client_id",
        "name",
        "created_at",
        "updated_at",
    }
    delegation_columns = {c["name"]: c for c in inspector.get_columns("agent_delegations")}
    assert set(delegation_columns) == {
        "id",
        "user_id",
        "agent_id",
        "status",
        "connected_at",
        "disconnected_at",
        "authorized_sid",
        "created_at",
        "updated_at",
    }
    assert delegation_columns["authorized_sid"]["nullable"]
    assert "'active'" in delegation_columns["status"]["default"]
    assert delegation_columns["disconnected_at"]["nullable"]
    for table in TABLES:
        cols = {c["name"]: c for c in inspector.get_columns(table)}
        for name in ("created_at", "updated_at"):
            assert not cols[name]["nullable"] and "now()" in cols[name]["default"]
            assert cols[name]["type"].timezone

    assert inspector.get_pk_constraint("agent_delegations")["name"] == "pk_agent_delegations"
    assert {u["name"]: u["column_names"] for u in inspector.get_unique_constraints("users")} == {
        "uq_users_workos_user_id": ["workos_user_id"]
    }
    assert {u["name"]: u["column_names"] for u in inspector.get_unique_constraints("agents")} == {
        "uq_agents_oauth_client_id": ["oauth_client_id"]
    }
    assert {
        u["name"]: u["column_names"] for u in inspector.get_unique_constraints("agent_delegations")
    } == {
        "uq_agent_delegations_id_user_id": ["id", "user_id"],
    }
    assert {c["name"] for c in inspector.get_check_constraints("agent_delegations")} == {
        "ck_agent_delegations_status",
        "ck_agent_delegations_disconnected_at_matches_status",
        "ck_agent_delegations_authorized_sid_not_empty",
    }
    fks = {fk["name"]: fk for fk in inspector.get_foreign_keys("agent_delegations")}
    assert set(fks) == {
        "fk_agent_delegations_user_id_users",
        "fk_agent_delegations_agent_id_agents",
    }
    for fk in fks.values():
        assert fk["options"].get("ondelete") == "RESTRICT"
    indexes = {
        ix["name"]: ix
        for ix in inspector.get_indexes("agent_delegations")
        if "duplicates_constraint" not in ix  # skip UNIQUE constraints' backing indexes
    }
    assert {name: ix["column_names"] for name, ix in indexes.items()} == {
        "ix_agent_delegations_agent_id": ["agent_id"],
        "ix_agent_delegations_user_id_agent_id": ["user_id", "agent_id"],
        "uq_agent_delegations_user_id_agent_id_active": ["user_id", "agent_id"],
    }
    active = indexes["uq_agent_delegations_user_id_agent_id_active"]
    assert active["unique"]
    assert active["dialect_options"]["postgresql_where"] == "(status = 'active'::text)"
    assert not indexes["ix_agent_delegations_user_id_agent_id"]["unique"]


def test_no_drift_between_models_and_migrations(
    migrated_engine: Engine, run_alembic: RunAlembic
) -> None:
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
    run_alembic(command.check)  # raises AutogenerateDiffsDetected on drift


def test_orm_defaults(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session:
        user = User(workos_user_id=f"user_{uuid.uuid4().hex}")
        agent = Agent(oauth_client_id=f"client_{uuid.uuid4().hex}")
        session.add_all([user, agent])
        session.flush()
        delegation = AgentDelegation(user_id=user.id, agent_id=agent.id)
        session.add(delegation)
        session.flush()
        session.refresh(delegation)
        assert isinstance(user.id, uuid.UUID)
        assert delegation.status == DelegationStatus.ACTIVE
        assert delegation.connected_at is not None and delegation.disconnected_at is None
        assert delegation.created_at.tzinfo is not None
        session.rollback()


def test_duplicate_workos_user_id_rejected(conn: Connection) -> None:
    _user(conn, "user_dup")
    with pytest.raises(IntegrityError, match="uq_users_workos_user_id"), conn.begin_nested():
        _user(conn, "user_dup")


def test_duplicate_oauth_client_id_rejected(conn: Connection) -> None:
    conn.execute(insert(Agent).values(oauth_client_id="client_dup"))
    with pytest.raises(IntegrityError, match="uq_agents_oauth_client_id"), conn.begin_nested():
        conn.execute(insert(Agent).values(oauth_client_id="client_dup"))


def test_duplicate_active_delegation_rejected(conn: Connection) -> None:
    user_id, agent_id = _user(conn), _agent(conn)
    _delegation(conn, user_id, agent_id)
    with (
        pytest.raises(IntegrityError, match="uq_agent_delegations_user_id_agent_id_active"),
        conn.begin_nested(),
    ):
        _delegation(conn, user_id, agent_id)


def _register(conn: Connection, user_id: uuid.UUID, agent_id: uuid.UUID) -> uuid.UUID | None:
    """Registration as F2 does it: ON CONFLICT on the partial (active-only) unique index."""
    return conn.execute(
        insert(AgentDelegation)
        .values(user_id=user_id, agent_id=agent_id)
        .on_conflict_do_nothing(
            index_elements=["user_id", "agent_id"], index_where=text("status = 'active'")
        )
        .returning(AgentDelegation.id)
    ).scalar_one_or_none()


def test_on_conflict_active_never_reactivates_disconnected(conn: Connection) -> None:
    user_id, agent_id = _user(conn), _agent(conn)
    _delegation(
        conn,
        user_id,
        agent_id,
        status=DelegationStatus.DISCONNECTED.value,
        disconnected_at=text("now()"),
    )
    new_id = _register(conn, user_id, agent_id)  # reconnect: a new active row
    assert new_id is not None
    assert _register(conn, user_id, agent_id) is None  # an active row exists: nothing changes
    rows = conn.execute(
        select(AgentDelegation.id, AgentDelegation.status, AgentDelegation.disconnected_at).where(
            AgentDelegation.user_id == user_id, AgentDelegation.agent_id == agent_id
        )
    ).all()
    assert len(rows) == 2
    by_status = {row.status: row for row in rows}
    assert by_status[DelegationStatus.ACTIVE].id == new_id
    assert by_status[DelegationStatus.DISCONNECTED].disconnected_at is not None


@pytest.mark.parametrize(
    ("status", "disconnected_at"),
    [("disconnected", None), ("active", text("now()"))],
)
def test_status_disconnected_at_check(conn: Connection, status: str, disconnected_at) -> None:
    user_id, agent_id = _user(conn), _agent(conn)
    with (
        pytest.raises(IntegrityError, match="ck_agent_delegations_disconnected_at_matches_status"),
        conn.begin_nested(),
    ):
        _delegation(conn, user_id, agent_id, status=status, disconnected_at=disconnected_at)


def test_invalid_status_rejected(conn: Connection) -> None:
    user_id, agent_id = _user(conn), _agent(conn)
    with pytest.raises(IntegrityError, match="ck_agent_delegations_status"), conn.begin_nested():
        _delegation(conn, user_id, agent_id, status="revoked")


def test_disconnect_is_status_update(conn: Connection) -> None:
    user_id, agent_id = _user(conn), _agent(conn)
    _delegation(conn, user_id, agent_id)
    conn.execute(
        AgentDelegation.__table__.update()
        .where(AgentDelegation.user_id == user_id)
        .values(status=DelegationStatus.DISCONNECTED.value, disconnected_at=text("now()"))
    )
    status = conn.execute(
        select(AgentDelegation.status).where(AgentDelegation.user_id == user_id)
    ).scalar_one()
    assert status == DelegationStatus.DISCONNECTED


def test_delete_with_delegations_restricted(conn: Connection) -> None:
    user_id, agent_id = _user(conn), _agent(conn)
    _delegation(conn, user_id, agent_id)
    with (
        pytest.raises(IntegrityError, match="fk_agent_delegations_user_id_users"),
        conn.begin_nested(),
    ):
        conn.execute(delete(User).where(User.id == user_id))
    with (
        pytest.raises(IntegrityError, match="fk_agent_delegations_agent_id_agents"),
        conn.begin_nested(),
    ):
        conn.execute(delete(Agent).where(Agent.id == agent_id))


def test_downgrade_and_reupgrade(migrated_engine: Engine, run_alembic: RunAlembic) -> None:
    run_alembic(command.downgrade, "base")
    remaining = set(inspect(migrated_engine).get_table_names())
    assert not (TABLES & remaining)
    with migrated_engine.connect() as connection:
        assert MigrationContext.configure(connection).get_current_revision() is None

    run_alembic(command.upgrade, "head")
    assert set(inspect(migrated_engine).get_table_names()) >= TABLES
    with migrated_engine.connect() as connection:
        assert (
            MigrationContext.configure(connection).get_current_revision() == "0009_invitation_join"
        )
