"""Connections and question schema: handles, requests, questions, leases, answers, evidence.

Tables: handles, connection_requests, questions, assignments, question_events, answers,
answer_evidence.

Revision ID: 0003_connections_questions
Revises: 0002_profile
Create Date: 2026-11-01

Hand-written to match revel.db.models.connections and revel.db.models.questions. Constraint names
follow revel.db.base.NAMING_CONVENTION (or are given explicitly in the models) and are written out
literally (op.f marks them final), and CHECK expressions are literal strings, so this migration
stays fixed if models or enums change.

Also adds enrichment_enabled, questions_enabled and question_settings to contribution_policies,
and UNIQUE (id, user_id) on agent_delegations and replaces the single-column delegation FKs
on claims and claim_events with composite (delegation_id, user_id) FKs under the same names, so a
delegation can never be attributed to a different user. Downgrade restores the 0002 form.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_connections_questions"
down_revision: str | Sequence[str] | None = "0002_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _fk(table: str, column: str, referred: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        [f"{referred}.id"],
        name=op.f(f"fk_{table}_{column}_{referred}"),
        ondelete="RESTRICT",
    )


def _delegation_fk(name: str, delegation_column: str, user_column: str) -> sa.ForeignKeyConstraint:
    """Composite FK: the delegation must belong to the stated user (MATCH SIMPLE)."""
    return sa.ForeignKeyConstraint(
        [delegation_column, user_column],
        ["agent_delegations.id", "agent_delegations.user_id"],
        name=op.f(name),
        ondelete="RESTRICT",
    )


def _ck(table: str, name: str, sqltext: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(sqltext, name=op.f(f"ck_{table}_{name}"))


# Existing single-column delegation FKs (0002) retrofitted to composite ownership FKs.
_RETROFITS = (
    (
        "claims",
        "fk_claims_contributor_delegation_id_agent_delegations",
        "contributor_delegation_id",
        "contributor_user_id",
    ),
    (
        "claim_events",
        "fk_claim_events_actor_delegation_id_agent_delegations",
        "actor_delegation_id",
        "actor_user_id",
    ),
)


def upgrade() -> None:
    op.create_unique_constraint(
        op.f("uq_agent_delegations_id_user_id"), "agent_delegations", ["id", "user_id"]
    )
    for table, name, delegation_column, user_column in _RETROFITS:
        op.drop_constraint(op.f(name), table, type_="foreignkey")
        op.create_foreign_key(
            op.f(name),
            table,
            "agent_delegations",
            [delegation_column, user_column],
            ["id", "user_id"],
            ondelete="RESTRICT",
        )

    op.add_column(
        "contribution_policies",
        sa.Column(
            "enrichment_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
    )
    op.add_column(
        "contribution_policies",
        sa.Column(
            "questions_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
    )
    op.add_column(
        "contribution_policies",
        sa.Column(
            "question_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    op.create_table(
        "handles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("handle", sa.Text(), nullable=False),
        sa.Column("discoverable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_handles")),
        _fk("handles", "user_id", "users"),
        sa.UniqueConstraint("handle", name=op.f("uq_handles_handle")),
        _ck("handles", "handle_format", "handle ~ '^[a-z0-9_]{3,30}$'"),
        _ck("handles", "avatar_url_length", "char_length(avatar_url) <= 2048"),
    )

    op.create_table(
        "connection_requests",
        _id(),
        sa.Column("requester_user_id", sa.Uuid(), nullable=False),
        sa.Column("requester_delegation_id", sa.Uuid(), nullable=True),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("granted_tier", sa.Text(), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responder_delegation_id", sa.Uuid(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connection_requests")),
        _fk("connection_requests", "requester_user_id", "users"),
        _fk("connection_requests", "target_user_id", "users"),
        _delegation_fk(
            "fk_connection_requests_requester_delegation_agent_delegations",
            "requester_delegation_id",
            "requester_user_id",
        ),
        _delegation_fk(
            "fk_connection_requests_responder_delegation_agent_delegations",
            "responder_delegation_id",
            "target_user_id",
        ),
        _ck("connection_requests", "not_self_request", "requester_user_id <> target_user_id"),
        _ck(
            "connection_requests",
            "status",
            "status IN ('pending', 'approved', 'declined', 'blocked', 'cancelled', 'revoked')",
        ),
        _ck(
            "connection_requests",
            "granted_tier",
            "granted_tier IN ('connections', 'trusted_circle')",
        ),
        _ck(
            "connection_requests",
            "granted_tier_matches_status",
            "(status = 'approved') = (granted_tier IS NOT NULL)",
        ),
        _ck(
            "connection_requests",
            "responded_at_matches_status",
            "(status = 'pending') = (responded_at IS NULL)",
        ),
    )
    op.create_index(
        op.f("uq_connection_requests_requester_user_id_target_user_id_live"),
        "connection_requests",
        ["requester_user_id", "target_user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'approved', 'blocked')"),
    )
    op.create_index(
        op.f("ix_connection_requests_target_user_id_status"),
        "connection_requests",
        ["target_user_id", "status"],
    )
    op.create_index(
        op.f("ix_connection_requests_requester_user_id_created_at"),
        "connection_requests",
        ["requester_user_id", "created_at"],
    )

    op.create_table(
        "questions",
        _id(),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("requester_user_id", sa.Uuid(), nullable=False),
        sa.Column("requester_delegation_id", sa.Uuid(), nullable=True),
        sa.Column("requested_information", sa.Text(), nullable=False),
        sa.Column(
            "private_purpose",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("minimized_question", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "escalation_permitted", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("escalation_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("routing_step", sa.SmallInteger(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_questions")),
        _fk("questions", "profile_id", "profiles"),
        _fk("questions", "requester_user_id", "users"),
        _delegation_fk(
            "fk_questions_requester_delegation_id_agent_delegations",
            "requester_delegation_id",
            "requester_user_id",
        ),
        sa.UniqueConstraint("profile_id", "id", name=op.f("uq_questions_profile_id_id")),
        _ck(
            "questions",
            "requested_information_length",
            "char_length(requested_information) BETWEEN 1 AND 1000",
        ),
        _ck(
            "questions",
            "minimized_question_length",
            "char_length(minimized_question) <= 1000",
        ),
        _ck(
            "questions",
            "status",
            "status IN ('open', 'answered', 'closed', 'declined', 'expired', 'cancelled')",
        ),
        _ck("questions", "expires_after_created", "expires_at > created_at"),
        _ck("questions", "routing_step_range", "routing_step BETWEEN 1 AND 4"),
        _ck(
            "questions",
            "closed_at_matches_status",
            "(status IN ('open', 'answered')) = (closed_at IS NULL)",
        ),
    )
    op.create_index(op.f("ix_questions_profile_id_status"), "questions", ["profile_id", "status"])
    op.create_index(
        op.f("ix_questions_requester_user_id_status"), "questions", ["requester_user_id", "status"]
    )
    op.create_index(op.f("ix_questions_status_expires_at"), "questions", ["status", "expires_at"])
    op.create_index(
        op.f("ix_questions_requester_user_id_created_at"),
        "questions",
        ["requester_user_id", "created_at"],
    )

    op.create_table(
        "assignments",
        _id(),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("respondent_delegation_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), server_default="available", nullable=False),
        sa.Column("lease_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("human_contact", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("user_contacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contact_delivery_confirmed", sa.Boolean(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assignments")),
        _fk("assignments", "question_id", "questions"),
        _fk("assignments", "respondent_delegation_id", "agent_delegations"),
        sa.UniqueConstraint(
            "question_id",
            "respondent_delegation_id",
            name=op.f("uq_assignments_question_id_respondent_delegation_id"),
        ),
        sa.UniqueConstraint("question_id", "id", name=op.f("uq_assignments_question_id_id")),
        sa.UniqueConstraint(
            "question_id",
            "id",
            "respondent_delegation_id",
            name=op.f("uq_assignments_question_id_id_respondent_delegation_id"),
        ),
        _ck(
            "assignments",
            "status",
            "status IN ('available', 'claimed', 'waiting_for_user', 'completed', 'released', "
            "'declined', 'lease_expired')",
        ),
        _ck("assignments", "lease_version_non_negative", "lease_version >= 0"),
        _ck(
            "assignments",
            "lease_when_claimed",
            "status NOT IN ('claimed', 'waiting_for_user') "
            "OR (lease_expires_at IS NOT NULL AND claimed_at IS NOT NULL)",
        ),
        _ck(
            "assignments",
            "waiting_requires_contact",
            "status <> 'waiting_for_user' OR (human_contact AND user_contacted_at IS NOT NULL)",
        ),
        _ck(
            "assignments",
            "completed_at_matches_status",
            "(status = 'completed') = (completed_at IS NOT NULL)",
        ),
    )
    op.create_index(
        op.f("uq_assignments_question_id_human_contact"),
        "assignments",
        ["question_id"],
        unique=True,
        postgresql_where=sa.text("human_contact AND status IN ('claimed', 'waiting_for_user')"),
    )
    op.create_index(
        op.f("ix_assignments_respondent_delegation_id_status"),
        "assignments",
        ["respondent_delegation_id", "status"],
    )
    op.create_index(
        op.f("ix_assignments_status_lease_expires_at"),
        "assignments",
        ["status", "lease_expires_at"],
    )

    op.create_table(
        "question_events",
        _id(),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_delegation_id", sa.Uuid(), nullable=True),
        sa.Column("lease_version", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _now("occurred_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_question_events")),
        _fk("question_events", "question_id", "questions"),
        _fk("question_events", "actor_user_id", "users"),
        sa.ForeignKeyConstraint(
            ["question_id", "assignment_id"],
            ["assignments.question_id", "assignments.id"],
            name=op.f("fk_question_events_assignment_id_assignments"),
            ondelete="RESTRICT",
        ),
        _delegation_fk(
            "fk_question_events_actor_delegation_id_agent_delegations",
            "actor_delegation_id",
            "actor_user_id",
        ),
        _ck(
            "question_events",
            "event_type",
            "event_type IN ('made_available', 'retrieved', 'claimed', 'renewed', 'released', "
            "'lease_expired', 'user_contact_reported', 'answered', 'declined', "
            "'answer_retrieved', 'expired', 'cancelled', 'closed')",
        ),
        _ck(
            "question_events",
            "delegation_requires_actor",
            "actor_delegation_id IS NULL OR actor_user_id IS NOT NULL",
        ),
    )
    op.create_index(
        op.f("ix_question_events_question_id_occurred_at"),
        "question_events",
        ["question_id", "occurred_at"],
    )
    op.create_index(op.f("ix_question_events_assignment_id"), "question_events", ["assignment_id"])

    op.create_table(
        "answers",
        _id(),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=True),
        sa.Column("respondent_user_id", sa.Uuid(), nullable=False),
        sa.Column("respondent_delegation_id", sa.Uuid(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("audience", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="accepted", nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_answers")),
        sa.ForeignKeyConstraint(
            ["profile_id", "question_id"],
            ["questions.profile_id", "questions.id"],
            name=op.f("fk_answers_question_id_questions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["question_id", "assignment_id", "respondent_delegation_id"],
            ["assignments.question_id", "assignments.id", "assignments.respondent_delegation_id"],
            name=op.f("fk_answers_assignment_id_assignments"),
            ondelete="RESTRICT",
        ),
        _fk("answers", "respondent_user_id", "users"),
        _delegation_fk(
            "fk_answers_respondent_delegation_id_agent_delegations",
            "respondent_delegation_id",
            "respondent_user_id",
        ),
        sa.UniqueConstraint("profile_id", "id", name=op.f("uq_answers_profile_id_id")),
        _ck("answers", "body_length", "char_length(body) BETWEEN 1 AND 2000"),
        _ck(
            "answers",
            "audience",
            "audience IN ('public', 'connections', 'trusted_circle', 'only_me')",
        ),
        _ck("answers", "status", "status IN ('accepted', 'withdrawn')"),
        _ck(
            "answers",
            "assignment_requires_delegation",
            "assignment_id IS NULL OR respondent_delegation_id IS NOT NULL",
        ),
    )
    op.create_index(op.f("ix_answers_question_id"), "answers", ["question_id"])
    op.create_index(
        op.f("uq_answers_question_id_accepted"),
        "answers",
        ["question_id"],
        unique=True,
        postgresql_where=sa.text("status = 'accepted'"),
    )

    op.create_table(
        "answer_evidence",
        _id(),
        sa.Column("answer_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("proposed_audience", sa.Text(), nullable=True),
        sa.Column("evidence_type", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.Date(), nullable=True),
        sa.Column("observed_precision", sa.Text(), nullable=True),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_answer_evidence")),
        sa.ForeignKeyConstraint(
            ["profile_id", "answer_id"],
            ["answers.profile_id", "answers.id"],
            name=op.f("fk_answer_evidence_answer_id_answers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id", "claim_id"],
            ["claims.profile_id", "claims.id"],
            name=op.f("fk_answer_evidence_claim_id_claims"),
            ondelete="RESTRICT",
        ),
        _ck("answer_evidence", "category_length", "char_length(category) BETWEEN 1 AND 64"),
        _ck(
            "answer_evidence",
            "proposed_audience",
            "proposed_audience IN ('public', 'connections', 'trusted_circle', 'only_me')",
        ),
        _ck(
            "answer_evidence",
            "evidence_type",
            "evidence_type IN ('explicit', 'observed', 'inferred')",
        ),
        _ck(
            "answer_evidence",
            "observed_precision",
            "observed_precision IN ('day', 'month', 'year', 'unknown')",
        ),
        _ck(
            "answer_evidence",
            "claim_or_inline",
            "(claim_id IS NOT NULL AND category IS NULL AND proposed_audience IS NULL "
            "AND evidence_type IS NULL AND observed_at IS NULL AND observed_precision IS NULL) "
            "OR (claim_id IS NULL AND category IS NOT NULL AND proposed_audience IS NOT NULL "
            "AND evidence_type IS NOT NULL AND observed_precision IS NOT NULL)",
        ),
        _ck(
            "answer_evidence",
            "observed_at_matches_precision",
            "observed_precision IS NULL "
            "OR ((observed_precision = 'unknown') = (observed_at IS NULL))",
        ),
    )
    op.create_index(op.f("ix_answer_evidence_answer_id"), "answer_evidence", ["answer_id"])
    op.create_index(op.f("ix_answer_evidence_claim_id"), "answer_evidence", ["claim_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_answer_evidence_claim_id"), table_name="answer_evidence")
    op.drop_index(op.f("ix_answer_evidence_answer_id"), table_name="answer_evidence")
    op.drop_table("answer_evidence")
    op.drop_index(op.f("uq_answers_question_id_accepted"), table_name="answers")
    op.drop_index(op.f("ix_answers_question_id"), table_name="answers")
    op.drop_table("answers")
    op.drop_index(op.f("ix_question_events_assignment_id"), table_name="question_events")
    op.drop_index(op.f("ix_question_events_question_id_occurred_at"), table_name="question_events")
    op.drop_table("question_events")
    op.drop_index(op.f("ix_assignments_status_lease_expires_at"), table_name="assignments")
    op.drop_index(op.f("ix_assignments_respondent_delegation_id_status"), table_name="assignments")
    op.drop_index(op.f("uq_assignments_question_id_human_contact"), table_name="assignments")
    op.drop_table("assignments")
    op.drop_index(op.f("ix_questions_requester_user_id_created_at"), table_name="questions")
    op.drop_index(op.f("ix_questions_status_expires_at"), table_name="questions")
    op.drop_index(op.f("ix_questions_requester_user_id_status"), table_name="questions")
    op.drop_index(op.f("ix_questions_profile_id_status"), table_name="questions")
    op.drop_table("questions")
    op.drop_index(
        op.f("ix_connection_requests_requester_user_id_created_at"),
        table_name="connection_requests",
    )
    op.drop_index(
        op.f("ix_connection_requests_target_user_id_status"), table_name="connection_requests"
    )
    op.drop_index(
        op.f("uq_connection_requests_requester_user_id_target_user_id_live"),
        table_name="connection_requests",
    )
    op.drop_table("connection_requests")
    op.drop_table("handles")
    op.drop_column("contribution_policies", "question_settings")
    op.drop_column("contribution_policies", "questions_enabled")
    op.drop_column("contribution_policies", "enrichment_enabled")

    for table, name, delegation_column, _ in reversed(_RETROFITS):
        op.drop_constraint(op.f(name), table, type_="foreignkey")
        op.create_foreign_key(
            op.f(name),
            table,
            "agent_delegations",
            [delegation_column],
            ["id"],
            ondelete="RESTRICT",
        )
    op.drop_constraint(op.f("uq_agent_delegations_id_user_id"), "agent_delegations", type_="unique")
