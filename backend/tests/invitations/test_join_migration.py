"""Existing sent/pending capabilities survive the indefinite-signup migration."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def migration_database():
    url = os.environ.get("REVEL_MIGRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires a separate disposable REVEL_MIGRATION_TEST_DATABASE_URL")
    engine = create_engine(url)

    def migrate(action, revision):
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            action(config, revision)

    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    migrate(command.upgrade, "0008_invitation_provider_message")
    try:
        yield engine, migrate
    finally:
        engine.dispose()


def test_existing_sent_pending_invite_keeps_token_and_send_metadata(migration_database):
    engine, migrate = migration_database
    sender = uuid4()
    pending = "b5c78bd5-5f06-4840-9d56-a57d1d037a8c"
    terminal = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO users(id,workos_user_id) VALUES(:id,'synthetic-sender')"),
            {"id": sender},
        )
        for invite_id, status, token_hash in [
            (pending, "pending", "synthetic-sent-token-hash"),
            (terminal, "expired", "synthetic-terminal-hash"),
        ]:
            connection.execute(
                text("""INSERT INTO invitations(id,sender_user_id,email,token_hash,
                sealed_token,status,created_at,expires_at,delivery_status,sent_at,provider_message_id)
                VALUES(:id,:sender,'synthetic@example.test',:hash,'synthetic-sealed-token',:status,
                    now()-interval '20 days',now()-interval '10 days','sent',
                    now()-interval '19 days','synthetic-provider-reference')"""),
                {"id": invite_id, "sender": sender, "hash": token_hash, "status": status},
            )
    migrate(command.upgrade, "head")
    with engine.connect() as connection:
        rows = {str(row.id): row for row in connection.execute(text("SELECT * FROM invitations"))}
        assert rows[pending].expires_at is None and rows[pending].status == "pending"
        assert rows[pending].token_hash == "synthetic-sent-token-hash"
        assert rows[pending].sealed_token == "synthetic-sealed-token"
        assert rows[pending].delivery_status == "sent"
        assert rows[pending].provider_message_id == "synthetic-provider-reference"
        assert rows[pending].sent_at is not None
        assert rows[terminal].status == "expired" and rows[terminal].expires_at is not None
    with pytest.raises(DBAPIError, match="Cannot downgrade 0009_invitation_join"):
        migrate(command.downgrade, "0008_invitation_provider_message")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE invitations SET status='joined', expires_at=now()+interval '1 day' "
                "WHERE id=:id"
            ),
            {"id": pending},
        )
    with pytest.raises(DBAPIError, match="Cannot downgrade 0009_invitation_join"):
        migrate(command.downgrade, "0008_invitation_provider_message")
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM invitations WHERE id=:id"), {"id": pending})
    migrate(command.downgrade, "0008_invitation_provider_message")
    migrate(command.upgrade, "head")
