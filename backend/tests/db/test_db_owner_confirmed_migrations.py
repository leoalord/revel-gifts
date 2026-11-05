"""0005_connection_owner_confirmed against a real PostgreSQL (REVEL_TEST_DATABASE_URL).

``connection_requests.owner_confirmed_at`` (nullable, no default, no backfill), its CHECK that
allows it only on approved/revoked rows, the up/down round trip, and the migration's
lock_timeout.
"""

import io
import uuid
from collections.abc import Callable, Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, delete, inspect, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from revel.db.base import Base
from revel.db.models import ConnectionRequest, User
from revel.settings import get_settings

RunAlembic = Callable[..., None]
HEAD = "0009_invitation_join"
PREVIOUS = "0004_delegation_reconnect"
TABLE = "connection_requests"
CHECK = "ck_connection_requests_owner_confirmed_at_matches_status"
NOW = text("now()")


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


def _row(status: str) -> dict:
    """Column values satisfying the other connection_requests CHECKs for ``status``."""
    row: dict = {"status": status}
    if status != "pending":
        row["responded_at"] = NOW
    if status == "approved":
        row["granted_tier"] = "connections"
    return row


def _request(conn: Connection, requester: uuid.UUID, **values) -> uuid.UUID:
    return conn.execute(
        insert(ConnectionRequest)
        .values(requester_user_id=requester, target_user_id=_user(conn), **values)
        .returning(ConnectionRequest.id)
    ).scalar_one()


def _revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def _checks(engine: Engine) -> dict[str, str]:
    return {c["name"]: c["sqltext"] for c in inspect(engine).get_check_constraints(TABLE)}


# --- schema ----------------------------------------------------------------------------------


def test_column_and_check_schema(migrated_engine: Engine) -> None:
    column = {c["name"]: c for c in inspect(migrated_engine).get_columns(TABLE)}[
        "owner_confirmed_at"
    ]
    assert column["nullable"] and column["default"] is None
    assert column["type"].timezone
    sql = _checks(migrated_engine)[CHECK]
    assert "owner_confirmed_at IS NULL" in sql
    assert "'approved'" in sql and "'revoked'" in sql
    assert "'pending'" not in sql
    assert not any(
        "owner_confirmed_at" in fk["constrained_columns"]
        for fk in inspect(migrated_engine).get_foreign_keys(TABLE)
    )


def test_no_drift(migrated_engine: Engine, run_alembic: RunAlembic) -> None:
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
    run_alembic(command.check)


# --- CHECK -----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "declined", "blocked", "cancelled"])
def test_owner_confirmed_at_rejected_unless_approved_or_revoked(
    conn: Connection, status: str
) -> None:
    requester = _user(conn)
    _request(conn, requester, **_row(status))  # NULL is allowed on every status
    with pytest.raises(IntegrityError, match=CHECK), conn.begin_nested():
        _request(conn, requester, owner_confirmed_at=NOW, **_row(status))


@pytest.mark.parametrize("status", ["approved", "revoked"])
def test_owner_confirmed_at_allowed_on_approved_and_revoked(conn: Connection, status: str) -> None:
    requester = _user(conn)
    request_id = _request(conn, requester, owner_confirmed_at=NOW, **_row(status))
    stored = conn.execute(
        select(ConnectionRequest.owner_confirmed_at).where(ConnectionRequest.id == request_id)
    ).scalar_one()
    assert stored is not None and stored.tzinfo is not None
    assert _request(conn, requester, **_row(status))  # NULL also allowed


def test_status_change_away_from_approved_keeps_check(conn: Connection) -> None:
    """Approved -> revoked keeps the timestamp; approved -> declined must clear it."""
    request_id = _request(conn, _user(conn), owner_confirmed_at=NOW, **_row("approved"))
    table = ConnectionRequest.__table__
    conn.execute(
        table.update().where(table.c.id == request_id).values(status="revoked", granted_tier=None)
    )
    with pytest.raises(IntegrityError, match=CHECK), conn.begin_nested():
        conn.execute(table.update().where(table.c.id == request_id).values(status="declined"))


# --- migration up/down ----------------------------------------------------------------------


def test_upgrade_keeps_existing_rows_null(migrated_engine: Engine, run_alembic: RunAlembic) -> None:
    """Rows written under 0004 survive 0005 unchanged, with owner_confirmed_at NULL."""
    run_alembic(command.downgrade, PREVIOUS)
    statuses = ("pending", "approved", "declined", "blocked", "cancelled", "revoked")
    users: list[uuid.UUID] = []
    try:
        with migrated_engine.begin() as connection:
            for status in statuses:
                requester, target = _user(connection), _user(connection)
                users += [requester, target]
                connection.execute(
                    text(
                        "INSERT INTO connection_requests "
                        "(requester_user_id, target_user_id, status, granted_tier, responded_at) "
                        "VALUES (:r, :t, :s, :g, CASE WHEN :s = 'pending' THEN NULL ELSE now() END)"
                    ),
                    {
                        "r": requester,
                        "t": target,
                        "s": status,
                        "g": "trusted_circle" if status == "approved" else None,
                    },
                )
        run_alembic(command.upgrade, "head")
        assert _revision(migrated_engine) == HEAD
        with migrated_engine.connect() as connection:
            rows = connection.execute(
                select(
                    ConnectionRequest.status,
                    ConnectionRequest.granted_tier,
                    ConnectionRequest.owner_confirmed_at,
                ).where(ConnectionRequest.requester_user_id.in_(users))
            ).all()
        assert sorted(r.status for r in rows) == sorted(statuses)
        assert all(r.owner_confirmed_at is None for r in rows)
        assert {r.granted_tier for r in rows if r.status == "approved"} == {"trusted_circle"}
    finally:
        run_alembic(command.upgrade, "head")
        with migrated_engine.begin() as connection:
            connection.execute(
                delete(ConnectionRequest).where(ConnectionRequest.requester_user_id.in_(users))
            )
            connection.execute(delete(User).where(User.id.in_(users)))


def test_downgrade_to_0004_round_trip(migrated_engine: Engine, run_alembic: RunAlembic) -> None:
    run_alembic(command.downgrade, PREVIOUS)
    try:
        assert _revision(migrated_engine) == PREVIOUS
        columns = {c["name"] for c in inspect(migrated_engine).get_columns(TABLE)}
        assert "owner_confirmed_at" not in columns
        assert CHECK not in _checks(migrated_engine)
        assert set(_checks(migrated_engine)) == {
            "ck_connection_requests_not_self_request",
            "ck_connection_requests_status",
            "ck_connection_requests_granted_tier",
            "ck_connection_requests_granted_tier_matches_status",
            "ck_connection_requests_responded_at_matches_status",
        }
    finally:
        run_alembic(command.upgrade, "head")
    assert _revision(migrated_engine) == HEAD
    assert "owner_confirmed_at" in {c["name"] for c in inspect(migrated_engine).get_columns(TABLE)}
    assert CHECK in _checks(migrated_engine)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_fails_fast_on_lock_timeout(
    migrated_engine: Engine, run_alembic: RunAlembic, direction: str
) -> None:
    """A concurrent SHARE lock (as the FGA reconcile job takes) makes 0005 fail, not queue."""
    if direction == "upgrade":
        run_alembic(command.downgrade, PREVIOUS)
    start = _revision(migrated_engine)
    try:
        with migrated_engine.connect() as holder:
            holder.begin()
            holder.execute(text("LOCK TABLE connection_requests IN SHARE MODE"))
            with pytest.raises(DBAPIError, match="lock timeout"):
                if direction == "upgrade":
                    run_alembic(command.upgrade, "head")
                else:
                    run_alembic(command.downgrade, PREVIOUS)
            holder.rollback()
        assert _revision(migrated_engine) == start
    finally:
        run_alembic(command.upgrade, "head")
    assert _revision(migrated_engine) == HEAD


def test_offline_sql_sets_lock_timeout(
    monkeypatch: pytest.MonkeyPatch, alembic_config: Config
) -> None:
    """`alembic upgrade/downgrade --sql` sets lock_timeout first; downgrade drops CHECK first."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://revel@localhost/revel")
    get_settings.cache_clear()
    try:
        up = io.StringIO()
        alembic_config.output_buffer = up
        command.upgrade(alembic_config, f"{PREVIOUS}:{HEAD}", sql=True)
        down = io.StringIO()
        alembic_config.output_buffer = down
        command.downgrade(alembic_config, f"{HEAD}:{PREVIOUS}", sql=True)
    finally:
        get_settings.cache_clear()
    up_sql, down_sql = up.getvalue(), down.getvalue()
    assert up_sql.index("SET LOCAL lock_timeout = '5s'") < up_sql.index("ADD COLUMN")
    assert up_sql.index("ADD COLUMN owner_confirmed_at TIMESTAMP WITH TIME ZONE") < up_sql.index(
        f"ADD CONSTRAINT {CHECK}"
    )
    assert "owner_confirmed_at IS NULL OR status IN ('approved', 'revoked')" in up_sql
    assert "UPDATE connection_requests" not in up_sql  # no backfill
    assert down_sql.index("SET LOCAL lock_timeout = '5s'") < down_sql.index(
        f"DROP CONSTRAINT {CHECK}"
    )
    assert down_sql.index(f"DROP CONSTRAINT {CHECK}") < down_sql.index(
        "DROP COLUMN owner_confirmed_at"
    )
