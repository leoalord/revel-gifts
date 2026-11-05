"""Database layer checks that never connect to a database."""

import io

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

import revel.db.session as db_session
from revel.db.base import NAMING_CONVENTION, Base
from revel.db.enums import Audience, DelegationStatus, check_in
from revel.db.models import Agent, AgentDelegation, User
from revel.settings import Settings, get_settings

CLOUD_SQL_URL = "postgresql+psycopg://revel_app:s3cr%40t@/revel?host=/cloudsql/proj:region:inst"


def test_audience_rank() -> None:
    assert {a: a.rank for a in Audience} == {
        Audience.PUBLIC: 0,
        Audience.CONNECTIONS: 1,
        Audience.TRUSTED_CIRCLE: 2,
        Audience.ONLY_ME: 3,
    }
    assert [a.value for a in Audience] == ["public", "connections", "trusted_circle", "only_me"]
    # F4 filters items with audience_rank <= viewer_rank.
    visible_to_connection = [a for a in Audience if a.rank <= Audience.CONNECTIONS.rank]
    assert visible_to_connection == [Audience.PUBLIC, Audience.CONNECTIONS]
    assert Audience("trusted_circle").rank == 2


def test_delegation_status_values_and_check_expression() -> None:
    assert [s.value for s in DelegationStatus] == ["active", "disconnected"]
    assert check_in("status", DelegationStatus) == "status IN ('active', 'disconnected')"


def test_metadata_naming_convention() -> None:
    assert dict(Base.metadata.naming_convention) == NAMING_CONVENTION
    assert set(Base.metadata.tables) >= {"users", "agents", "agent_delegations"}


def _ddl() -> str:
    dialect = postgresql.dialect()
    parts = [str(CreateTable(t).compile(dialect=dialect)) for t in Base.metadata.sorted_tables]
    parts += [
        str(CreateIndex(ix).compile(dialect=dialect))
        for t in Base.metadata.sorted_tables
        for ix in t.indexes
    ]
    return "\n".join(parts)


def test_deterministic_constraint_names() -> None:
    ddl = _ddl()
    for name in (
        "pk_users",
        "uq_users_workos_user_id",
        "pk_agents",
        "uq_agents_oauth_client_id",
        "pk_agent_delegations",
        "uq_agent_delegations_user_id_agent_id_active",
        "ix_agent_delegations_user_id_agent_id",
        "ck_agent_delegations_authorized_sid_not_empty",
        "pk_agent_delegation_sessions",
        "fk_agent_delegation_sessions_delegation_id_agent_delegations",
        "ck_agent_delegation_sessions_sid_not_empty",
        "fk_agent_delegations_user_id_users",
        "fk_agent_delegations_agent_id_agents",
        "ck_agent_delegations_status",
        "ck_agent_delegations_disconnected_at_matches_status",
        "ix_agent_delegations_agent_id",
    ):
        assert name in ddl, name
    # every FK (identity 2, profile 13, 0003 20, 0004 1)
    assert ddl.count("ON DELETE RESTRICT") == 42
    # 0004: (user_id, agent_id) is unique only among active rows (no table-level UNIQUE).
    assert "UNIQUE (user_id, agent_id)" not in ddl
    assert (
        "CREATE UNIQUE INDEX uq_agent_delegations_user_id_agent_id_active ON agent_delegations "
        "(user_id, agent_id) WHERE status = 'active'"
    ) in ddl
    assert "gen_random_uuid()" in ddl


def test_model_columns() -> None:
    users = User.__table__.c
    assert users.workos_user_id.unique and not users.workos_user_id.nullable
    assert users.email.nullable and users.display_name.nullable
    assert not Agent.__table__.c.oauth_client_id.nullable
    assert Agent.__table__.c.oauth_client_id.unique
    delegations = AgentDelegation.__table__.c
    assert delegations.status.server_default.arg == "active"
    assert delegations.disconnected_at.nullable
    assert delegations.authorized_sid.nullable
    assert not delegations.connected_at.nullable
    for table in (User.__table__, Agent.__table__, AgentDelegation.__table__):
        assert table.c.created_at.type.timezone and not table.c.created_at.nullable
        assert table.c.updated_at.onupdate is not None


def test_single_alembic_head(alembic_config: Config) -> None:
    script = ScriptDirectory.from_config(alembic_config)
    assert script.get_heads() == ["0009_invitation_join"]
    assert [rev.revision for rev in script.walk_revisions()] == [
        "0009_invitation_join",
        "0008_invitation_provider_message",
        "0007_inbox_check_setups",
        "0006_invitations",
        "0005_connection_owner_confirmed",
        "0004_delegation_reconnect",
        "0003_connections_questions",
        "0002_profile",
        "0001_identity",
    ]


def test_cloud_sql_unix_socket_url_engine() -> None:
    engine = db_session.create_db_engine(CLOUD_SQL_URL)
    try:
        assert engine.url.host is None
        assert engine.url.database == "revel"
        assert engine.url.query["host"] == "/cloudsql/proj:region:inst"
        assert engine.url.password == "s3cr@t"
        assert "s3cr" not in repr(engine.url) and "s3cr" not in repr(engine)
        assert "***" in repr(engine.url)
        _, connect_kwargs = engine.dialect.create_connect_args(engine.url)
        assert connect_kwargs["host"] == "/cloudsql/proj:region:inst"
        assert connect_kwargs["dbname"] == "revel"
    finally:
        engine.dispose()


def test_pool_settings_and_secret_url() -> None:
    engine = db_session.create_db_engine(SecretStr(CLOUD_SQL_URL))
    try:
        assert engine.pool.size() == 3
        assert engine.pool._max_overflow == 2
        assert engine.pool._pre_ping is True
        assert engine.url.query["host"] == "/cloudsql/proj:region:inst"
    finally:
        engine.dispose()


def test_url_defaults_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, database_url=CLOUD_SQL_URL)
    monkeypatch.setattr(db_session, "get_settings", lambda: settings)
    assert db_session.database_url().query["host"] == "/cloudsql/proj:region:inst"


def test_offline_sql_from_settings_url(
    monkeypatch: pytest.MonkeyPatch, alembic_config: Config
) -> None:
    """`alembic upgrade head --sql` reads the URL from settings and never prints it."""
    monkeypatch.setenv("DATABASE_URL", CLOUD_SQL_URL)
    get_settings.cache_clear()
    buffer = io.StringIO()
    alembic_config.output_buffer = buffer
    try:
        command.upgrade(alembic_config, "head", sql=True)
    finally:
        get_settings.cache_clear()
    sql = buffer.getvalue()
    assert "CREATE TABLE agent_delegations" in sql
    assert "s3cr" not in sql and "cloudsql" not in sql
