"""Connections and question schema checks that never connect to a database."""

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from revel.db.base import Base
from revel.db.enums import (
    LIVE_CONNECTION_REQUEST_STATUSES,
    AnswerStatus,
    AssignmentStatus,
    Audience,
    ConnectionRequestStatus,
    ConnectionTier,
    EvidenceType,
    ObservedPrecision,
    QuestionEventType,
    QuestionStatus,
    check_in,
)
from revel.db.models import (
    Answer,
    AnswerEvidence,
    Assignment,
    ConnectionRequest,
    ContributionPolicy,
    Handle,
    Question,
    QuestionEvent,
)

NEW_TABLES = {
    "handles",
    "connection_requests",
    "questions",
    "assignments",
    "question_events",
    "answers",
    "answer_evidence",
}
ALL_TABLES = {
    "users",
    "agents",
    "agent_delegations",
    "profiles",
    "contribution_policies",
    "claims",
    "claim_events",
} | NEW_TABLES
MIGRATION_0003 = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0003_connections_questions_schema.py"
)


def test_connection_and_question_enum_values() -> None:
    assert [s.value for s in ConnectionRequestStatus] == [
        "pending",
        "approved",
        "declined",
        "blocked",
        "cancelled",
        "revoked",
    ]
    assert [s.value for s in LIVE_CONNECTION_REQUEST_STATUSES] == ["pending", "approved", "blocked"]
    assert [t.value for t in ConnectionTier] == ["connections", "trusted_circle"]
    # Tiers are a subset of audiences (same strings F4 uses).
    assert {t.value for t in ConnectionTier} <= {a.value for a in Audience}
    assert [s.value for s in QuestionStatus] == [
        "open",
        "answered",
        "closed",
        "declined",
        "expired",
        "cancelled",
    ]
    assert [s.value for s in AssignmentStatus] == [
        "available",
        "claimed",
        "waiting_for_user",
        "completed",
        "released",
        "declined",
        "lease_expired",
    ]
    assert [e.value for e in QuestionEventType] == [
        "made_available",
        "retrieved",
        "claimed",
        "renewed",
        "released",
        "lease_expired",
        "user_contact_reported",
        "answered",
        "declined",
        "answer_retrieved",
        "expired",
        "cancelled",
        "closed",
    ]
    assert [s.value for s in AnswerStatus] == ["accepted", "withdrawn"]


def _check_sql(table, name: str) -> str:
    for constraint in table.constraints:
        if constraint.name in (name, f"ck_{table.name}_{name}"):
            return str(constraint.sqltext)
    raise AssertionError(f"missing {name}")


ENUM_CHECKS = (
    (ConnectionRequest, "status", ConnectionRequestStatus),
    (ConnectionRequest, "granted_tier", ConnectionTier),
    (Question, "status", QuestionStatus),
    (Assignment, "status", AssignmentStatus),
    (QuestionEvent, "event_type", QuestionEventType),
    (Answer, "audience", Audience),
    (Answer, "status", AnswerStatus),
    (AnswerEvidence, "proposed_audience", Audience),
    (AnswerEvidence, "evidence_type", EvidenceType),
    (AnswerEvidence, "observed_precision", ObservedPrecision),
)


def test_check_expressions_built_from_enums() -> None:
    for model, column, enum in ENUM_CHECKS:
        assert _check_sql(model.__table__, column) == check_in(column, enum), (model, column)


def test_migration_check_literals_match_enums() -> None:
    """0003 writes CHECK expressions literally; they must equal the enum-built ones."""
    source = MIGRATION_0003.read_text()
    flat = re.sub(r'"\s*\n\s*"', "", source)  # join implicitly concatenated string literals
    for _, column, enum in ENUM_CHECKS:
        assert check_in(column, enum) in flat, column


def test_single_alembic_head() -> None:
    script = ScriptDirectory.from_config(
        Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    )
    assert script.get_heads() == ["0003_connections_questions"]
    assert script.get_revision("0003_connections_questions").down_revision == "0002_profile"


def _ddl(tables: set[str]) -> str:
    dialect = postgresql.dialect()
    selected = [t for t in Base.metadata.sorted_tables if t.name in tables]
    parts = [str(CreateTable(t).compile(dialect=dialect)) for t in selected]
    parts += [str(CreateIndex(ix).compile(dialect=dialect)) for t in selected for ix in t.indexes]
    return "\n".join(parts)


def test_constraint_and_index_names() -> None:
    assert set(Base.metadata.tables) == ALL_TABLES
    ddl = _ddl(NEW_TABLES)
    for name in (
        "pk_handles",
        "uq_handles_handle",
        "fk_handles_user_id_users",
        "ck_handles_handle_format",
        "ck_handles_avatar_url_length",
        "pk_connection_requests",
        "fk_connection_requests_requester_user_id_users",
        "fk_connection_requests_target_user_id_users",
        "fk_connection_requests_requester_delegation_agent_delegations",
        "ck_connection_requests_not_self_request",
        "ck_connection_requests_status",
        "ck_connection_requests_granted_tier",
        "ck_connection_requests_granted_tier_matches_status",
        "ck_connection_requests_responded_at_matches_status",
        "uq_connection_requests_requester_user_id_target_user_id_live",
        "ix_connection_requests_target_user_id_status",
        "ix_connection_requests_requester_user_id_created_at",
        "pk_questions",
        "fk_questions_profile_id_profiles",
        "fk_questions_requester_user_id_users",
        "fk_questions_requester_delegation_id_agent_delegations",
        "uq_questions_profile_id_id",
        "ck_questions_requested_information_length",
        "ck_questions_minimized_question_length",
        "ck_questions_status",
        "ck_questions_expires_after_created",
        "ck_questions_routing_step_range",
        "ck_questions_closed_at_matches_status",
        "ix_questions_profile_id_status",
        "ix_questions_requester_user_id_status",
        "ix_questions_status_expires_at",
        "ix_questions_requester_user_id_created_at",
        "fk_connection_requests_responder_delegation_agent_delegations",
        "uq_assignments_question_id_id_respondent_delegation_id",
        "ck_answers_assignment_requires_delegation",
        "pk_assignments",
        "fk_assignments_question_id_questions",
        "fk_assignments_respondent_delegation_id_agent_delegations",
        "uq_assignments_question_id_respondent_delegation_id",
        "uq_assignments_question_id_id",
        "uq_assignments_question_id_human_contact",
        "ck_assignments_status",
        "ck_assignments_lease_version_non_negative",
        "ck_assignments_lease_when_claimed",
        "ck_assignments_waiting_requires_contact",
        "ck_assignments_completed_at_matches_status",
        "ix_assignments_respondent_delegation_id_status",
        "ix_assignments_status_lease_expires_at",
        "pk_question_events",
        "fk_question_events_question_id_questions",
        "fk_question_events_assignment_id_assignments",
        "fk_question_events_actor_user_id_users",
        "fk_question_events_actor_delegation_id_agent_delegations",
        "ck_question_events_event_type",
        "ck_question_events_delegation_requires_actor",
        "ix_question_events_question_id_occurred_at",
        "ix_question_events_assignment_id",
        "pk_answers",
        "fk_answers_question_id_questions",
        "fk_answers_assignment_id_assignments",
        "fk_answers_respondent_user_id_users",
        "fk_answers_respondent_delegation_id_agent_delegations",
        "uq_answers_profile_id_id",
        "uq_answers_question_id_accepted",
        "ck_answers_body_length",
        "ck_answers_audience",
        "ck_answers_status",
        "ix_answers_question_id",
        "pk_answer_evidence",
        "fk_answer_evidence_answer_id_answers",
        "fk_answer_evidence_claim_id_claims",
        "ck_answer_evidence_category_length",
        "ck_answer_evidence_proposed_audience",
        "ck_answer_evidence_evidence_type",
        "ck_answer_evidence_observed_precision",
        "ck_answer_evidence_claim_or_inline",
        "ck_answer_evidence_observed_at_matches_precision",
        "ix_answer_evidence_answer_id",
        "ix_answer_evidence_claim_id",
    ):
        assert name in ddl, name
        assert len(name) <= 63, name
    assert ddl.count("ON DELETE RESTRICT") == 20
    assert "ON DELETE CASCADE" not in ddl
    # Delegation-ownership composite FKs (new tables).
    # connection_requests (requester, responder), questions, question_events, answers
    assert ddl.count("REFERENCES agent_delegations (id, user_id)") == 5
    for fragment in (
        "FOREIGN KEY(requester_delegation_id, requester_user_id) REFERENCES agent_delegations "
        "(id, user_id)",
        "FOREIGN KEY(actor_delegation_id, actor_user_id) REFERENCES agent_delegations "
        "(id, user_id)",
        "FOREIGN KEY(respondent_delegation_id, respondent_user_id) REFERENCES agent_delegations "
        "(id, user_id)",
        "CONSTRAINT fk_question_events_assignment_id_assignments FOREIGN KEY(question_id, "
        "assignment_id) REFERENCES assignments (question_id, id)",
        "CONSTRAINT fk_answers_assignment_id_assignments FOREIGN KEY(question_id, assignment_id, "
        "respondent_delegation_id) REFERENCES assignments (question_id, id, "
        "respondent_delegation_id)",
        "CONSTRAINT uq_assignments_question_id_id_respondent_delegation_id UNIQUE "
        "(question_id, id, respondent_delegation_id)",
        "CONSTRAINT ck_answers_assignment_requires_delegation CHECK (assignment_id IS NULL OR "
        "respondent_delegation_id IS NOT NULL)",
        "CONSTRAINT fk_connection_requests_responder_delegation_agent_delegations FOREIGN KEY("
        "responder_delegation_id, target_user_id) REFERENCES agent_delegations (id, user_id)",
        "CONSTRAINT fk_answers_question_id_questions FOREIGN KEY(profile_id, question_id) "
        "REFERENCES questions (profile_id, id)",
        "CONSTRAINT fk_answer_evidence_answer_id_answers FOREIGN KEY(profile_id, answer_id) "
        "REFERENCES answers (profile_id, id)",
        "CONSTRAINT fk_answer_evidence_claim_id_claims FOREIGN KEY(profile_id, claim_id) "
        "REFERENCES claims (profile_id, id)",
        "CREATE UNIQUE INDEX uq_connection_requests_requester_user_id_target_user_id_live ON "
        "connection_requests (requester_user_id, target_user_id) "
        "WHERE status IN ('pending', 'approved', 'blocked')",
        "CREATE UNIQUE INDEX uq_assignments_question_id_human_contact ON assignments "
        "(question_id) WHERE human_contact AND status IN ('claimed', 'waiting_for_user')",
        "CREATE UNIQUE INDEX uq_answers_question_id_accepted ON answers (question_id) "
        "WHERE status = 'accepted'",
    ):
        assert fragment in ddl, fragment


def test_retrofitted_delegation_ownership_fks() -> None:
    ddl = _ddl({"agent_delegations", "claims", "claim_events"})
    assert "CONSTRAINT uq_agent_delegations_id_user_id UNIQUE (id, user_id)" in ddl
    assert (
        "CONSTRAINT fk_claims_contributor_delegation_id_agent_delegations FOREIGN KEY("
        "contributor_delegation_id, contributor_user_id) REFERENCES agent_delegations (id, user_id)"
        in ddl
    )
    assert (
        "CONSTRAINT fk_claim_events_actor_delegation_id_agent_delegations FOREIGN KEY("
        "actor_delegation_id, actor_user_id) REFERENCES agent_delegations (id, user_id)" in ddl
    )
    assert "REFERENCES agent_delegations (id)" not in ddl


def test_question_restricted_columns_are_raiseload_deferred() -> None:
    """A plain Question load never materializes private_purpose/escalation_context."""
    mapper = sa_inspect(Question)
    for name in ("private_purpose", "escalation_context"):
        prop = mapper.attrs[name]
        assert prop.deferred and prop.raiseload, name
    eager = {p.key for p in mapper.column_attrs if not p.deferred}
    assert not eager & {"private_purpose", "escalation_context"}
    compiled = str(select(Question).compile(dialect=postgresql.dialect()))
    assert "private_purpose" not in compiled and "escalation_context" not in compiled
    assert "requested_information" in compiled
    assert "undefer" in Question.__doc__
    # Access on a loaded instance raising is checked against PostgreSQL in
    # test_db_questions_migrations.test_question_restricted_columns_raise_unless_undeferred.


def test_contribution_policy_question_controls() -> None:
    policies = ContributionPolicy.__table__.c
    assert policies.questions_enabled.server_default.arg.text == "true"
    assert policies.enrichment_enabled.server_default.arg.text == "true"
    assert policies.question_settings.server_default.arg.text == "'{}'::jsonb"
    for name in ("questions_enabled", "enrichment_enabled", "question_settings"):
        assert not policies[name].nullable, name


def test_model_columns() -> None:
    handles = Handle.__table__.c
    assert handles.user_id.primary_key and not handles.handle.nullable and handles.handle.unique
    assert handles.discoverable.server_default.arg.text == "true"
    assert handles.avatar_url.nullable
    assert "display_name" not in handles  # stays on users

    requests = ConnectionRequest.__table__.c
    assert requests.status.server_default.arg == "pending"
    assert requests.requester_delegation_id.nullable and requests.granted_tier.nullable
    assert requests.responded_at.nullable

    questions = Question.__table__.c
    assert questions.private_purpose.server_default.arg.text == "'{}'::jsonb"
    assert not questions.private_purpose.nullable
    assert questions.status.server_default.arg == "open"
    assert not questions.expires_at.nullable and questions.expires_at.type.timezone
    assert questions.escalation_permitted.server_default.arg.text == "false"
    for nullable in ("minimized_question", "escalation_context", "routing_step", "closed_at"):
        assert questions[nullable].nullable, nullable
    assert "Never" in Question.__doc__ or "NEVER" in Question.__doc__

    assignments = Assignment.__table__.c
    assert assignments.lease_version.server_default.arg.text == "0"
    assert not assignments.lease_version.nullable
    assert assignments.status.server_default.arg == "available"
    assert assignments.human_contact.server_default.arg.text == "false"
    assert assignments.contact_delivery_confirmed.nullable
    assert "lease_version" in Assignment.__doc__

    events = QuestionEvent.__table__.c
    assert "updated_at" not in events  # append-only
    assert events.lease_version.nullable and events.assignment_id.nullable

    answers = Answer.__table__.c
    assert answers.status.server_default.arg == "accepted"
    assert not answers.audience.nullable and not answers.profile_id.nullable

    evidence = AnswerEvidence.__table__.c
    assert set(evidence.keys()) == {
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
    }  # no quote, statement, source or free-text columns
