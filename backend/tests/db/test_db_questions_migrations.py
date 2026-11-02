"""Connections and question schema against PostgreSQL (REVEL_TEST_DATABASE_URL)."""

import uuid
from collections.abc import Callable, Iterator
from datetime import date

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, delete, inspect, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from revel.db.base import Base
from revel.db.models import (
    Agent,
    AgentDelegation,
    Answer,
    AnswerEvidence,
    Assignment,
    Claim,
    ClaimEvent,
    ConnectionRequest,
    Handle,
    Profile,
    Question,
    QuestionEvent,
    User,
)

RunAlembic = Callable[..., None]
NEW_TABLES = {
    "handles",
    "connection_requests",
    "questions",
    "assignments",
    "question_events",
    "answers",
    "answer_evidence",
}
LATER = text("now() + interval '1 day'")
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


def _rejects(conn: Connection, constraint: str, fn: Callable[[], object]) -> None:
    with pytest.raises(IntegrityError, match=constraint), conn.begin_nested():
        fn()


def _user(conn: Connection) -> uuid.UUID:
    return conn.execute(
        insert(User).values(workos_user_id=f"user_{uuid.uuid4().hex}").returning(User.id)
    ).scalar_one()


def _delegation(conn: Connection, user_id: uuid.UUID) -> uuid.UUID:
    agent_id = conn.execute(
        insert(Agent).values(oauth_client_id=f"client_{uuid.uuid4().hex}").returning(Agent.id)
    ).scalar_one()
    return conn.execute(
        insert(AgentDelegation)
        .values(user_id=user_id, agent_id=agent_id)
        .returning(AgentDelegation.id)
    ).scalar_one()


def _profile(conn: Connection) -> tuple[uuid.UUID, uuid.UUID]:
    owner = _user(conn)
    profile_id = conn.execute(
        insert(Profile).values(user_id=owner).returning(Profile.id)
    ).scalar_one()
    return profile_id, owner


def _claim(conn: Connection, profile_id: uuid.UUID, contributor: uuid.UUID, **values) -> uuid.UUID:
    row = {
        "profile_id": profile_id,
        "category": "hobbies",
        "statement": "Enjoys bouldering",
        "audience": "connections",
        "evidence_type": "explicit",
        "status": "active",
        "contributor_user_id": contributor,
        "contributed_via": "ui",
    }
    row.update(values)
    return conn.execute(insert(Claim).values(**row).returning(Claim.id)).scalar_one()


def _handle(conn: Connection, user_id: uuid.UUID, handle: str, **values) -> None:
    conn.execute(insert(Handle).values(user_id=user_id, handle=handle, **values))


def _request(conn: Connection, requester: uuid.UUID, target: uuid.UUID, **values) -> uuid.UUID:
    return conn.execute(
        insert(ConnectionRequest)
        .values(requester_user_id=requester, target_user_id=target, **values)
        .returning(ConnectionRequest.id)
    ).scalar_one()


def _question(conn: Connection, profile_id: uuid.UUID, requester: uuid.UUID, **values) -> uuid.UUID:
    row = {
        "profile_id": profile_id,
        "requester_user_id": requester,
        "requested_information": "Which coffee does she like?",
        "expires_at": LATER,
    }
    row.update(values)
    return conn.execute(insert(Question).values(**row).returning(Question.id)).scalar_one()


def _assignment(
    conn: Connection, question_id: uuid.UUID, delegation_id: uuid.UUID, **values
) -> uuid.UUID:
    return conn.execute(
        insert(Assignment)
        .values(question_id=question_id, respondent_delegation_id=delegation_id, **values)
        .returning(Assignment.id)
    ).scalar_one()


def _claimed(**values) -> dict:
    row = {"status": "claimed", "lease_expires_at": LATER, "claimed_at": NOW}
    row.update(values)
    return row


def _event(conn: Connection, question_id: uuid.UUID, **values) -> None:
    row = {"question_id": question_id, "event_type": "made_available"}
    row.update(values)
    conn.execute(insert(QuestionEvent).values(**row))


def _answer(
    conn: Connection,
    question_id: uuid.UUID,
    profile_id: uuid.UUID,
    respondent: uuid.UUID,
    **values,
) -> uuid.UUID:
    row = {
        "question_id": question_id,
        "profile_id": profile_id,
        "respondent_user_id": respondent,
        "body": "Flat white, oat milk",
        "audience": "connections",
    }
    row.update(values)
    return conn.execute(insert(Answer).values(**row).returning(Answer.id)).scalar_one()


INLINE = {
    "category": "food_drink",
    "proposed_audience": "connections",
    "evidence_type": "observed",
    "observed_at": date(2026, 9, 1),
    "observed_precision": "month",
}


def _evidence(conn: Connection, answer_id: uuid.UUID, profile_id: uuid.UUID, **values) -> None:
    conn.execute(
        insert(AnswerEvidence).values(answer_id=answer_id, profile_id=profile_id, **values)
    )


class _Scene:
    """A profile owner, a requester and the owner's respondent agent with an open question."""

    def __init__(self, conn: Connection) -> None:
        self.profile_id, self.owner = _profile(conn)
        self.requester = _user(conn)
        self.requester_delegation = _delegation(conn, self.requester)
        self.owner_delegation = _delegation(conn, self.owner)
        self.question_id = _question(
            conn,
            self.profile_id,
            self.requester,
            requester_delegation_id=self.requester_delegation,
        )
        self.assignment_id = _assignment(conn, self.question_id, self.owner_delegation)


# --- schema --------------------------------------------------------------------------------


def _columns(inspector, table: str) -> dict:
    return {c["name"]: c for c in inspector.get_columns(table)}


def test_schema_after_upgrade(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    assert set(inspector.get_table_names()) >= NEW_TABLES

    handles = _columns(inspector, "handles")
    assert set(handles) == {
        "user_id",
        "handle",
        "discoverable",
        "avatar_url",
        "created_at",
        "updated_at",
    }
    assert handles["discoverable"]["default"] == "true"
    assert not handles["handle"]["nullable"] and handles["avatar_url"]["nullable"]
    assert inspector.get_pk_constraint("handles")["constrained_columns"] == ["user_id"]

    requests = _columns(inspector, "connection_requests")
    assert set(requests) == {
        "id",
        "requester_user_id",
        "requester_delegation_id",
        "target_user_id",
        "status",
        "granted_tier",
        "responded_at",
        "created_at",
        "updated_at",
    }
    assert "'pending'" in requests["status"]["default"]
    assert requests["granted_tier"]["nullable"] and requests["responded_at"]["nullable"]

    questions = _columns(inspector, "questions")
    assert set(questions) == {
        "id",
        "profile_id",
        "requester_user_id",
        "requester_delegation_id",
        "requested_information",
        "private_purpose",
        "minimized_question",
        "status",
        "expires_at",
        "escalation_permitted",
        "escalation_context",
        "routing_step",
        "closed_at",
        "created_at",
        "updated_at",
    }
    assert questions["private_purpose"]["default"] == "'{}'::jsonb"
    assert not questions["private_purpose"]["nullable"]
    assert "'open'" in questions["status"]["default"]
    assert questions["escalation_permitted"]["default"] == "false"
    assert not questions["expires_at"]["nullable"] and questions["expires_at"]["type"].timezone

    assignments = _columns(inspector, "assignments")
    assert set(assignments) == {
        "id",
        "question_id",
        "respondent_delegation_id",
        "status",
        "lease_version",
        "lease_expires_at",
        "claimed_at",
        "human_contact",
        "user_contacted_at",
        "contact_delivery_confirmed",
        "completed_at",
        "created_at",
        "updated_at",
    }
    assert assignments["lease_version"]["default"] == "0"
    assert not assignments["lease_version"]["nullable"]
    assert "'available'" in assignments["status"]["default"]
    assert assignments["human_contact"]["default"] == "false"

    events = _columns(inspector, "question_events")
    assert set(events) == {
        "id",
        "question_id",
        "assignment_id",
        "event_type",
        "actor_user_id",
        "actor_delegation_id",
        "lease_version",
        "details",
        "occurred_at",
    }
    assert "now()" in events["occurred_at"]["default"]

    answers = _columns(inspector, "answers")
    assert set(answers) == {
        "id",
        "question_id",
        "profile_id",
        "assignment_id",
        "respondent_user_id",
        "respondent_delegation_id",
        "body",
        "audience",
        "status",
        "created_at",
        "updated_at",
    }
    assert "'accepted'" in answers["status"]["default"]

    evidence = _columns(inspector, "answer_evidence")
    assert set(evidence) == {
        "id",
        "answer_id",
        "profile_id",
        "claim_id",
        "category",
        "proposed_audience",
        "evidence_type",
        "observed_at",
        "observed_precision",
        "created_at",
    }

    for table in NEW_TABLES - {"question_events"}:
        created = _columns(inspector, table)["created_at"]
        assert not created["nullable"] and "now()" in created["default"], table

    expected_checks = {
        "handles": {"ck_handles_handle_format", "ck_handles_avatar_url_length"},
        "connection_requests": {
            "ck_connection_requests_not_self_request",
            "ck_connection_requests_status",
            "ck_connection_requests_granted_tier",
            "ck_connection_requests_granted_tier_matches_status",
            "ck_connection_requests_responded_at_matches_status",
        },
        "questions": {
            "ck_questions_requested_information_length",
            "ck_questions_minimized_question_length",
            "ck_questions_status",
            "ck_questions_expires_after_created",
            "ck_questions_routing_step_range",
            "ck_questions_closed_at_matches_status",
        },
        "assignments": {
            "ck_assignments_status",
            "ck_assignments_lease_version_non_negative",
            "ck_assignments_lease_when_claimed",
            "ck_assignments_waiting_requires_contact",
            "ck_assignments_completed_at_matches_status",
        },
        "question_events": {
            "ck_question_events_event_type",
            "ck_question_events_delegation_requires_actor",
        },
        "answers": {"ck_answers_body_length", "ck_answers_audience", "ck_answers_status"},
        "answer_evidence": {
            "ck_answer_evidence_category_length",
            "ck_answer_evidence_proposed_audience",
            "ck_answer_evidence_evidence_type",
            "ck_answer_evidence_observed_precision",
            "ck_answer_evidence_claim_or_inline",
            "ck_answer_evidence_observed_at_matches_precision",
        },
    }
    for table, names in expected_checks.items():
        assert {c["name"] for c in inspector.get_check_constraints(table)} == names, table

    expected_uniques = {
        "handles": {"uq_handles_handle": ["handle"]},
        "connection_requests": {},
        "questions": {"uq_questions_profile_id_id": ["profile_id", "id"]},
        "assignments": {
            "uq_assignments_question_id_respondent_delegation_id": [
                "question_id",
                "respondent_delegation_id",
            ],
            "uq_assignments_question_id_id": ["question_id", "id"],
        },
        "question_events": {},
        "answers": {"uq_answers_profile_id_id": ["profile_id", "id"]},
        "answer_evidence": {},
        "agent_delegations": {
            "uq_agent_delegations_user_id_agent_id": ["user_id", "agent_id"],
            "uq_agent_delegations_id_user_id": ["id", "user_id"],
        },
    }
    for table, uniques in expected_uniques.items():
        found = {u["name"]: u["column_names"] for u in inspector.get_unique_constraints(table)}
        assert found == uniques, table

    # name -> (constrained columns, referred table, referred columns)
    delegation = ("agent_delegations", ["id", "user_id"])
    expected_fks = {
        "handles": {"fk_handles_user_id_users": (["user_id"], "users", ["id"])},
        "connection_requests": {
            "fk_connection_requests_requester_user_id_users": (
                ["requester_user_id"],
                "users",
                ["id"],
            ),
            "fk_connection_requests_target_user_id_users": (["target_user_id"], "users", ["id"]),
            "fk_connection_requests_requester_delegation_agent_delegations": (
                ["requester_delegation_id", "requester_user_id"],
                *delegation,
            ),
        },
        "questions": {
            "fk_questions_profile_id_profiles": (["profile_id"], "profiles", ["id"]),
            "fk_questions_requester_user_id_users": (["requester_user_id"], "users", ["id"]),
            "fk_questions_requester_delegation_id_agent_delegations": (
                ["requester_delegation_id", "requester_user_id"],
                *delegation,
            ),
        },
        "assignments": {
            "fk_assignments_question_id_questions": (["question_id"], "questions", ["id"]),
            "fk_assignments_respondent_delegation_id_agent_delegations": (
                ["respondent_delegation_id"],
                "agent_delegations",
                ["id"],
            ),
        },
        "question_events": {
            "fk_question_events_question_id_questions": (["question_id"], "questions", ["id"]),
            "fk_question_events_assignment_id_assignments": (
                ["question_id", "assignment_id"],
                "assignments",
                ["question_id", "id"],
            ),
            "fk_question_events_actor_user_id_users": (["actor_user_id"], "users", ["id"]),
            "fk_question_events_actor_delegation_id_agent_delegations": (
                ["actor_delegation_id", "actor_user_id"],
                *delegation,
            ),
        },
        "answers": {
            "fk_answers_question_id_questions": (
                ["profile_id", "question_id"],
                "questions",
                ["profile_id", "id"],
            ),
            "fk_answers_assignment_id_assignments": (
                ["question_id", "assignment_id"],
                "assignments",
                ["question_id", "id"],
            ),
            "fk_answers_respondent_user_id_users": (["respondent_user_id"], "users", ["id"]),
            "fk_answers_respondent_delegation_id_agent_delegations": (
                ["respondent_delegation_id", "respondent_user_id"],
                *delegation,
            ),
        },
        "answer_evidence": {
            "fk_answer_evidence_answer_id_answers": (
                ["profile_id", "answer_id"],
                "answers",
                ["profile_id", "id"],
            ),
            "fk_answer_evidence_claim_id_claims": (
                ["profile_id", "claim_id"],
                "claims",
                ["profile_id", "id"],
            ),
        },
        "claims": {
            "fk_claims_contributor_delegation_id_agent_delegations": (
                ["contributor_delegation_id", "contributor_user_id"],
                *delegation,
            ),
        },
        "claim_events": {
            "fk_claim_events_actor_delegation_id_agent_delegations": (
                ["actor_delegation_id", "actor_user_id"],
                *delegation,
            ),
        },
    }
    for table, fks in expected_fks.items():
        found = {fk["name"]: fk for fk in inspector.get_foreign_keys(table)}
        if table not in ("claims", "claim_events"):
            assert set(found) == set(fks), table
        for name, (columns, referred, referred_columns) in fks.items():
            fk = found[name]
            assert fk["constrained_columns"] == columns, name
            assert fk["referred_table"] == referred, name
            assert fk["referred_columns"] == referred_columns, name
            assert fk["options"].get("ondelete") == "RESTRICT", name

    expected_indexes = {
        "connection_requests": {
            "uq_connection_requests_requester_user_id_target_user_id_live": [
                "requester_user_id",
                "target_user_id",
            ],
            "ix_connection_requests_target_user_id_status": ["target_user_id", "status"],
            "ix_connection_requests_requester_user_id_created_at": [
                "requester_user_id",
                "created_at",
            ],
        },
        "questions": {
            "ix_questions_profile_id_status": ["profile_id", "status"],
            "ix_questions_requester_user_id_status": ["requester_user_id", "status"],
            "ix_questions_status_expires_at": ["status", "expires_at"],
        },
        "assignments": {
            "uq_assignments_question_id_human_contact": ["question_id"],
            "ix_assignments_respondent_delegation_id_status": [
                "respondent_delegation_id",
                "status",
            ],
            "ix_assignments_status_lease_expires_at": ["status", "lease_expires_at"],
        },
        "question_events": {
            "ix_question_events_question_id_occurred_at": ["question_id", "occurred_at"],
            "ix_question_events_assignment_id": ["assignment_id"],
        },
        "answers": {
            "ix_answers_question_id": ["question_id"],
            "uq_answers_question_id_accepted": ["question_id"],
        },
        "answer_evidence": {
            "ix_answer_evidence_answer_id": ["answer_id"],
            "ix_answer_evidence_claim_id": ["claim_id"],
        },
    }
    for table, indexes in expected_indexes.items():
        found = {ix["name"]: ix for ix in inspector.get_indexes(table)}
        assert {name: found[name]["column_names"] for name in indexes} == indexes, table

    def where(table: str, name: str) -> str:
        index = {ix["name"]: ix for ix in inspector.get_indexes(table)}[name]
        assert index["unique"], name
        return index["dialect_options"]["postgresql_where"]

    live = where(
        "connection_requests", "uq_connection_requests_requester_user_id_target_user_id_live"
    )
    assert all(f"'{s}'" in live for s in ("pending", "approved", "blocked"))
    assert "'declined'" not in live
    contact = where("assignments", "uq_assignments_question_id_human_contact")
    assert "human_contact" in contact and "'claimed'" in contact and "'waiting_for_user'" in contact
    assert "'accepted'" in where("answers", "uq_answers_question_id_accepted")


def test_no_drift(migrated_engine: Engine, run_alembic: RunAlembic) -> None:
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
    run_alembic(command.check)


# --- handles -------------------------------------------------------------------------------


@pytest.mark.parametrize("handle", ["Alex", "ab", "a" * 31, "alex-w", "alex w", "al.ex", "élan"])
def test_handle_format_rejected(conn: Connection, handle: str) -> None:
    user_id = _user(conn)
    _rejects(conn, "ck_handles_handle_format", lambda: _handle(conn, user_id, handle))


def test_handles_accepted_unique_and_one_per_user(conn: Connection) -> None:
    alex, sam = _user(conn), _user(conn)
    _handle(conn, alex, "alex_w")
    row = conn.execute(select(Handle).where(Handle.user_id == alex)).one()
    assert row.discoverable is True and row.avatar_url is None
    _handle(conn, sam, "a" * 30, discoverable=False, avatar_url="https://example.test/a.png")
    _rejects(conn, "uq_handles_handle", lambda: _handle(conn, _user(conn), "alex_w"))
    _rejects(conn, "pk_handles", lambda: _handle(conn, alex, "alex_two"))
    _rejects(
        conn,
        "ck_handles_avatar_url_length",
        lambda: _handle(conn, _user(conn), "abc", avatar_url="x" * 2049),
    )


# --- connection requests -------------------------------------------------------------------


def test_self_request_rejected(conn: Connection) -> None:
    user_id = _user(conn)
    _rejects(
        conn, "ck_connection_requests_not_self_request", lambda: _request(conn, user_id, user_id)
    )


def test_request_tier_and_responded_at_consistency(conn: Connection) -> None:
    a = _user(conn)
    approve = {"status": "approved", "responded_at": NOW}
    for kwargs, constraint in (
        (approve, "ck_connection_requests_granted_tier_matches_status"),
        (
            {"status": "declined", "responded_at": NOW, "granted_tier": "connections"},
            "ck_connection_requests_granted_tier_matches_status",
        ),
        ({**approve, "granted_tier": "family"}, "ck_connection_requests_granted_tier"),
        ({"status": "friends", "responded_at": NOW}, "ck_connection_requests_status"),
        ({"status": "pending", "responded_at": NOW}, "ck_connection_requests_responded_at"),
        (
            {"status": "approved", "granted_tier": "connections"},
            "ck_connection_requests_responded_at",
        ),
    ):
        _rejects(conn, constraint, lambda kwargs=kwargs: _request(conn, a, _user(conn), **kwargs))
    pending = _request(conn, a, _user(conn))
    row = conn.execute(select(ConnectionRequest).where(ConnectionRequest.id == pending)).one()
    assert row.status == "pending" and row.responded_at is None and row.granted_tier is None
    _request(conn, a, _user(conn), granted_tier="trusted_circle", **approve)
    _request(conn, a, _user(conn), granted_tier="connections", **approve)
    for status in ("declined", "blocked", "cancelled", "revoked"):
        _request(conn, a, _user(conn), status=status, responded_at=NOW)


def test_one_live_request_per_direction(conn: Connection) -> None:
    a, b = _user(conn), _user(conn)
    live = "uq_connection_requests_requester_user_id_target_user_id_live"
    first = _request(conn, a, b)
    _rejects(conn, live, lambda: _request(conn, a, b))
    _request(conn, b, a)  # reverse direction is independent
    # Declined leaves room for a new request.
    conn.execute(
        update(ConnectionRequest)
        .where(ConnectionRequest.id == first)
        .values(status="declined", responded_at=NOW)
    )
    second = _request(conn, a, b)
    # Approved is live.
    conn.execute(
        update(ConnectionRequest)
        .where(ConnectionRequest.id == second)
        .values(status="approved", granted_tier="connections", responded_at=NOW)
    )
    _rejects(conn, live, lambda: _request(conn, a, b))
    # A block persists and prevents a new request.
    c, d = _user(conn), _user(conn)
    _request(conn, c, d, status="blocked", responded_at=NOW)
    _rejects(conn, live, lambda: _request(conn, c, d))
    _request(conn, c, d, status="cancelled", responded_at=NOW)


# --- delegation ownership ------------------------------------------------------------------


def test_connection_request_delegation_must_belong_to_requester(conn: Connection) -> None:
    a, b = _user(conn), _user(conn)
    others = _delegation(conn, b)
    _rejects(
        conn,
        "fk_connection_requests_requester_delegation_agent_delegations",
        lambda: _request(conn, a, b, requester_delegation_id=others),
    )
    _request(conn, a, b, requester_delegation_id=_delegation(conn, a))


def test_question_delegation_must_belong_to_requester(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    requester = _user(conn)
    _rejects(
        conn,
        "fk_questions_requester_delegation_id_agent_delegations",
        lambda: _question(
            conn, profile_id, requester, requester_delegation_id=_delegation(conn, owner)
        ),
    )
    _question(conn, profile_id, requester, requester_delegation_id=_delegation(conn, requester))


def test_answer_delegation_must_belong_to_respondent(conn: Connection) -> None:
    s = _Scene(conn)
    _rejects(
        conn,
        "fk_answers_respondent_delegation_id_agent_delegations",
        lambda: _answer(
            conn,
            s.question_id,
            s.profile_id,
            s.owner,
            respondent_delegation_id=s.requester_delegation,
        ),
    )
    _answer(
        conn,
        s.question_id,
        s.profile_id,
        s.owner,
        respondent_delegation_id=s.owner_delegation,
        assignment_id=s.assignment_id,
    )


def test_question_event_delegation_must_belong_to_actor(conn: Connection) -> None:
    s = _Scene(conn)
    _rejects(
        conn,
        "fk_question_events_actor_delegation_id_agent_delegations",
        lambda: _event(
            conn, s.question_id, actor_user_id=s.owner, actor_delegation_id=s.requester_delegation
        ),
    )
    _rejects(
        conn,
        "ck_question_events_delegation_requires_actor",
        lambda: _event(conn, s.question_id, actor_delegation_id=s.owner_delegation),
    )
    _event(
        conn,
        s.question_id,
        event_type="claimed",
        assignment_id=s.assignment_id,
        actor_user_id=s.owner,
        actor_delegation_id=s.owner_delegation,
        lease_version=1,
        details={"lease_seconds": 600},
    )
    _event(conn, s.question_id, event_type="expired")  # system event, no actor


def test_retrofitted_claim_delegation_must_belong_to_contributor(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    contributor = _user(conn)
    others = _delegation(conn, owner)
    _rejects(
        conn,
        "fk_claims_contributor_delegation_id_agent_delegations",
        lambda: _claim(
            conn,
            profile_id,
            contributor,
            contributed_via="mcp",
            contributor_delegation_id=others,
            status="provisional",
        ),
    )
    claim_id = _claim(
        conn,
        profile_id,
        contributor,
        contributed_via="mcp",
        contributor_delegation_id=_delegation(conn, contributor),
        status="provisional",
    )
    _rejects(
        conn,
        "fk_claim_events_actor_delegation_id_agent_delegations",
        lambda: conn.execute(
            insert(ClaimEvent).values(
                claim_id=claim_id,
                profile_id=profile_id,
                action="created",
                actor_user_id=contributor,
                actor_delegation_id=others,
            )
        ),
    )
    conn.execute(
        insert(ClaimEvent).values(
            claim_id=claim_id,
            profile_id=profile_id,
            action="approved",
            actor_user_id=owner,
            actor_delegation_id=others,
        )
    )


# --- questions -----------------------------------------------------------------------------


def test_question_checks(conn: Connection) -> None:
    profile_id, _ = _profile(conn)
    requester = _user(conn)
    for kwargs, constraint in (
        ({"expires_at": NOW}, "ck_questions_expires_after_created"),
        ({"expires_at": text("now() - interval '1 hour'")}, "ck_questions_expires_after_created"),
        ({"status": "closed"}, "ck_questions_closed_at_matches_status"),
        ({"status": "expired"}, "ck_questions_closed_at_matches_status"),
        ({"status": "open", "closed_at": NOW}, "ck_questions_closed_at_matches_status"),
        ({"status": "answered", "closed_at": NOW}, "ck_questions_closed_at_matches_status"),
        ({"status": "archived", "closed_at": NOW}, "ck_questions_status"),
        ({"routing_step": 0}, "ck_questions_routing_step_range"),
        ({"routing_step": 5}, "ck_questions_routing_step_range"),
        ({"requested_information": ""}, "ck_questions_requested_information_length"),
        ({"requested_information": "x" * 1001}, "ck_questions_requested_information_length"),
        ({"minimized_question": "x" * 1001}, "ck_questions_minimized_question_length"),
    ):
        _rejects(
            conn, constraint, lambda kwargs=kwargs: _question(conn, profile_id, requester, **kwargs)
        )

    question_id = _question(conn, profile_id, requester)
    row = conn.execute(select(Question).where(Question.id == question_id)).one()
    assert row.status == "open" and row.private_purpose == {} and row.closed_at is None
    assert row.escalation_permitted is False and row.routing_step is None
    _question(conn, profile_id, requester, status="answered", routing_step=1)
    _question(
        conn,
        profile_id,
        requester,
        routing_step=4,
        private_purpose={"occasion": "birthday"},
        minimized_question="x" * 1000,
        requested_information="x" * 1000,
        escalation_permitted=True,
        escalation_context={"note": "ok to ask"},
    )
    for status in ("closed", "declined", "expired", "cancelled"):
        _question(conn, profile_id, requester, status=status, closed_at=NOW)


# --- assignments ---------------------------------------------------------------------------


def test_assignment_checks(conn: Connection) -> None:
    s = _Scene(conn)
    row = conn.execute(select(Assignment).where(Assignment.id == s.assignment_id)).one()
    assert (row.status, row.lease_version, row.human_contact) == ("available", 0, False)
    assert row.contact_delivery_confirmed is None
    _rejects(
        conn,
        "uq_assignments_question_id_respondent_delegation_id",
        lambda: _assignment(conn, s.question_id, s.owner_delegation),
    )
    for kwargs, constraint in (
        ({"status": "claimed", "claimed_at": NOW}, "ck_assignments_lease_when_claimed"),
        ({"status": "claimed", "lease_expires_at": LATER}, "ck_assignments_lease_when_claimed"),
        (_claimed(status="waiting_for_user"), "ck_assignments_waiting_requires_contact"),
        (
            _claimed(status="waiting_for_user", human_contact=True),
            "ck_assignments_waiting_requires_contact",
        ),
        (
            _claimed(status="waiting_for_user", user_contacted_at=NOW),
            "ck_assignments_waiting_requires_contact",
        ),
        ({"status": "completed"}, "ck_assignments_completed_at_matches_status"),
        ({"status": "released", "completed_at": NOW}, "ck_assignments_completed_at_matches_status"),
        ({"lease_version": -1}, "ck_assignments_lease_version_non_negative"),
        ({"status": "assigned"}, "ck_assignments_status"),
    ):
        _rejects(
            conn,
            constraint,
            lambda kwargs=kwargs: _assignment(
                conn, s.question_id, _delegation(conn, s.owner), **kwargs
            ),
        )
    _assignment(conn, s.question_id, _delegation(conn, s.owner), **_claimed())
    _assignment(
        conn,
        s.question_id,
        _delegation(conn, s.owner),
        **_claimed(
            status="waiting_for_user",
            human_contact=True,
            user_contacted_at=NOW,
            contact_delivery_confirmed=True,
        ),
    )
    _assignment(
        conn, s.question_id, _delegation(conn, s.owner), status="completed", completed_at=NOW
    )
    for status in ("released", "declined", "lease_expired"):
        _assignment(conn, s.question_id, _delegation(conn, s.owner), status=status)


def test_one_human_contact_lease_per_question(conn: Connection) -> None:
    s = _Scene(conn)
    contact = "uq_assignments_question_id_human_contact"
    _assignment(conn, s.question_id, _delegation(conn, s.owner), **_claimed(human_contact=True))
    # Non-contact claims and available rows flagged for contact do not count.
    _assignment(conn, s.question_id, _delegation(conn, s.owner), **_claimed())
    _assignment(conn, s.question_id, _delegation(conn, s.owner), human_contact=True)
    _rejects(
        conn,
        contact,
        lambda: _assignment(
            conn, s.question_id, _delegation(conn, s.owner), **_claimed(human_contact=True)
        ),
    )
    _rejects(
        conn,
        contact,
        lambda: _assignment(
            conn,
            s.question_id,
            _delegation(conn, s.owner),
            **_claimed(status="waiting_for_user", human_contact=True, user_contacted_at=NOW),
        ),
    )
    # Another question may have its own human-contact lease.
    other = _question(conn, s.profile_id, s.requester)
    _assignment(conn, other, s.owner_delegation, **_claimed(human_contact=True))


def test_optimistic_lease_claim_rejects_replay(conn: Connection) -> None:
    s = _Scene(conn)

    def claim_at(version: int) -> int:
        return conn.execute(
            update(Assignment)
            .where(Assignment.id == s.assignment_id, Assignment.lease_version == version)
            .values(
                status="claimed",
                lease_version=Assignment.lease_version + 1,
                lease_expires_at=LATER,
                claimed_at=NOW,
            )
        ).rowcount

    assert claim_at(0) == 1
    assert claim_at(0) == 0  # stale replay affects nothing
    complete_stale = conn.execute(
        update(Assignment)
        .where(Assignment.id == s.assignment_id, Assignment.lease_version == 0)
        .values(status="completed", completed_at=NOW)
    ).rowcount
    assert complete_stale == 0
    row = conn.execute(select(Assignment).where(Assignment.id == s.assignment_id)).one()
    assert (row.status, row.lease_version) == ("claimed", 1)


# --- events --------------------------------------------------------------------------------


def test_question_event_assignment_must_match_question(conn: Connection) -> None:
    s = _Scene(conn)
    other = _question(conn, s.profile_id, s.requester)
    _rejects(
        conn,
        "fk_question_events_assignment_id_assignments",
        lambda: _event(conn, other, event_type="claimed", assignment_id=s.assignment_id),
    )
    _rejects(
        conn,
        "ck_question_events_event_type",
        lambda: _event(conn, s.question_id, event_type="viewed"),
    )
    _event(conn, s.question_id, event_type="claimed", assignment_id=s.assignment_id)
    row = conn.execute(
        select(QuestionEvent).where(QuestionEvent.question_id == s.question_id)
    ).one()
    assert row.occurred_at is not None and row.details is None


# --- answers -------------------------------------------------------------------------------


def test_answer_question_and_assignment_consistency(conn: Connection) -> None:
    s = _Scene(conn)
    other_profile, _ = _profile(conn)
    other_question = _question(conn, s.profile_id, s.requester)
    _rejects(
        conn,
        "fk_answers_question_id_questions",
        lambda: _answer(conn, s.question_id, other_profile, s.owner),
    )
    _rejects(
        conn,
        "fk_answers_assignment_id_assignments",
        lambda: _answer(conn, other_question, s.profile_id, s.owner, assignment_id=s.assignment_id),
    )
    for kwargs, constraint in (
        ({"body": ""}, "ck_answers_body_length"),
        ({"body": "x" * 2001}, "ck_answers_body_length"),
        ({"audience": "everyone"}, "ck_answers_audience"),
        ({"status": "draft"}, "ck_answers_status"),
    ):
        _rejects(
            conn,
            constraint,
            lambda kwargs=kwargs: _answer(conn, s.question_id, s.profile_id, s.owner, **kwargs),
        )
    answer_id = _answer(
        conn, s.question_id, s.profile_id, s.owner, assignment_id=s.assignment_id, body="x" * 2000
    )
    assert (
        conn.execute(select(Answer.status).where(Answer.id == answer_id)).scalar_one() == "accepted"
    )


def test_one_accepted_answer_per_question(conn: Connection) -> None:
    s = _Scene(conn)
    _answer(conn, s.question_id, s.profile_id, s.owner, status="withdrawn")
    _answer(conn, s.question_id, s.profile_id, s.owner)
    _answer(conn, s.question_id, s.profile_id, s.owner, status="withdrawn")
    _rejects(
        conn,
        "uq_answers_question_id_accepted",
        lambda: _answer(conn, s.question_id, s.profile_id, s.owner),
    )
    other = _question(conn, s.profile_id, s.requester)
    _answer(conn, other, s.profile_id, s.owner)


# --- answer evidence -----------------------------------------------------------------------


def test_answer_evidence_shapes(conn: Connection) -> None:
    s = _Scene(conn)
    answer_id = _answer(conn, s.question_id, s.profile_id, s.owner)
    claim_id = _claim(conn, s.profile_id, s.owner)
    shape = "ck_answer_evidence_claim_or_inline"
    _rejects(
        conn,
        shape,
        lambda: _evidence(conn, answer_id, s.profile_id, claim_id=claim_id, category="food"),
    )
    _rejects(
        conn,
        shape,
        lambda: _evidence(
            conn, answer_id, s.profile_id, claim_id=claim_id, observed_precision="month"
        ),
    )
    _rejects(conn, shape, lambda: _evidence(conn, answer_id, s.profile_id))
    for missing in ("category", "proposed_audience", "evidence_type", "observed_precision"):
        partial = {k: v for k, v in INLINE.items() if k != missing}
        _rejects(
            conn, shape, lambda partial=partial: _evidence(conn, answer_id, s.profile_id, **partial)
        )
    for kwargs, constraint in (
        ({"observed_precision": "unknown"}, "ck_answer_evidence_observed_at_matches_precision"),
        ({"observed_at": None}, "ck_answer_evidence_observed_at_matches_precision"),
        ({"proposed_audience": "everyone"}, "ck_answer_evidence_proposed_audience"),
        ({"evidence_type": "rumour"}, "ck_answer_evidence_evidence_type"),
        ({"observed_precision": "week"}, "ck_answer_evidence_observed_precision"),
        ({"category": "c" * 65}, "ck_answer_evidence_category_length"),
    ):
        _rejects(
            conn,
            constraint,
            lambda kwargs=kwargs: _evidence(conn, answer_id, s.profile_id, **{**INLINE, **kwargs}),
        )
    _evidence(conn, answer_id, s.profile_id, claim_id=claim_id)
    _evidence(conn, answer_id, s.profile_id, **INLINE)
    _evidence(
        conn,
        answer_id,
        s.profile_id,
        **{**INLINE, "observed_at": None, "observed_precision": "unknown"},
    )
    count = conn.execute(
        select(AnswerEvidence.id).where(AnswerEvidence.answer_id == answer_id)
    ).all()
    assert len(count) == 3


def test_answer_evidence_claim_must_be_same_profile(conn: Connection) -> None:
    s = _Scene(conn)
    answer_id = _answer(conn, s.question_id, s.profile_id, s.owner)
    other_profile, other_owner = _profile(conn)
    others_claim = _claim(conn, other_profile, other_owner)
    _rejects(
        conn,
        "fk_answer_evidence_claim_id_claims",
        lambda: _evidence(conn, answer_id, s.profile_id, claim_id=others_claim),
    )
    # Claiming the other profile for the evidence row breaks the answer link instead.
    _rejects(
        conn,
        "fk_answer_evidence_answer_id_answers",
        lambda: _evidence(conn, answer_id, other_profile, claim_id=others_claim),
    )
    _evidence(conn, answer_id, s.profile_id, claim_id=_claim(conn, s.profile_id, s.owner))


# --- RESTRICT ------------------------------------------------------------------------------


def test_deletes_with_dependents_restricted(conn: Connection) -> None:
    s = _Scene(conn)
    _handle(conn, s.owner, f"owner_{uuid.uuid4().hex[:8]}")
    _request(conn, s.requester, s.owner)
    _event(conn, s.question_id, assignment_id=s.assignment_id)
    claim_id = _claim(conn, s.profile_id, s.owner)
    answer_id = _answer(conn, s.question_id, s.profile_id, s.owner, assignment_id=s.assignment_id)
    _evidence(conn, answer_id, s.profile_id, claim_id=claim_id)

    _rejects(
        conn,
        "fk_[a-z_]+_questions",
        lambda: conn.execute(delete(Question).where(Question.id == s.question_id)),
    )
    _rejects(
        conn,
        "fk_[a-z_]+_assignments",
        lambda: conn.execute(delete(Assignment).where(Assignment.id == s.assignment_id)),
    )
    _rejects(
        conn,
        "fk_answer_evidence_answer_id_answers",
        lambda: conn.execute(delete(Answer).where(Answer.id == answer_id)),
    )
    _rejects(
        conn,
        "fk_answer_evidence_claim_id_claims",
        lambda: conn.execute(delete(Claim).where(Claim.id == claim_id)),
    )
    _rejects(
        conn,
        "fk_[a-z_]+_agent_delegations",
        lambda: conn.execute(
            delete(AgentDelegation).where(AgentDelegation.id == s.owner_delegation)
        ),
    )
    _rejects(
        conn,
        "fk_[a-z_]+_agent_delegations",
        lambda: conn.execute(
            delete(AgentDelegation).where(AgentDelegation.id == s.requester_delegation)
        ),
    )
    for user_id in (s.owner, s.requester):
        _rejects(
            conn,
            "fk_[a-z_]+_users",
            lambda user_id=user_id: conn.execute(delete(User).where(User.id == user_id)),
        )
    handle_only = _user(conn)
    _handle(conn, handle_only, f"solo_{uuid.uuid4().hex[:8]}")
    _rejects(
        conn,
        "fk_handles_user_id_users",
        lambda: conn.execute(delete(User).where(User.id == handle_only)),
    )


# --- downgrade -----------------------------------------------------------------------------


def test_downgrade_to_profile_and_reupgrade(
    migrated_engine: Engine, run_alembic: RunAlembic
) -> None:
    run_alembic(command.downgrade, "0002_profile")
    inspector = inspect(migrated_engine)
    remaining = set(inspector.get_table_names())
    assert not (NEW_TABLES & remaining)
    assert {"claims", "claim_events", "agent_delegations"} <= remaining
    assert {u["name"] for u in inspector.get_unique_constraints("agent_delegations")} == {
        "uq_agent_delegations_user_id_agent_id"
    }
    for table, name, column in (
        (
            "claims",
            "fk_claims_contributor_delegation_id_agent_delegations",
            "contributor_delegation_id",
        ),
        (
            "claim_events",
            "fk_claim_events_actor_delegation_id_agent_delegations",
            "actor_delegation_id",
        ),
    ):
        fk = {fk["name"]: fk for fk in inspector.get_foreign_keys(table)}[name]
        assert fk["constrained_columns"] == [column], name
        assert fk["referred_table"] == "agent_delegations" and fk["referred_columns"] == ["id"]
        assert fk["options"].get("ondelete") == "RESTRICT"
    with migrated_engine.connect() as connection:
        assert MigrationContext.configure(connection).get_current_revision() == "0002_profile"

    run_alembic(command.downgrade, "base")
    assert not (NEW_TABLES & set(inspect(migrated_engine).get_table_names()))

    run_alembic(command.upgrade, "head")
    assert set(inspect(migrated_engine).get_table_names()) >= NEW_TABLES
    with migrated_engine.connect() as connection:
        assert (
            MigrationContext.configure(connection).get_current_revision()
            == "0003_connections_questions"
        )
