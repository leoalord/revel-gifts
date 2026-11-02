"""Question schema: questions, per-respondent assignments (leases), append-only question events,
answers and their evidence.

F7 owns the lifecycle logic; this schema enforces shapes and integrity. Rows that store both a
user and a delegation use composite ``(delegation_id, user_id)`` FKs so a delegation cannot be
attributed to another user (``agent_delegations`` rows are "user X's agent Y"; ``agents`` rows
are global OAuth clients). Same-question and same-profile composite FKs keep events, answers and
evidence consistent with their question. Never stored anywhere here: quotes, raw conversation,
source details or numeric confidence.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from revel.db.base import Base, TimestampMixin
from revel.db.enums import (
    AnswerStatus,
    AssignmentStatus,
    Audience,
    EvidenceType,
    ObservedPrecision,
    QuestionEventType,
    QuestionStatus,
    check_in,
)
from revel.db.models.profile import CATEGORY_MAX_LENGTH

_UUID_DEFAULT = text("gen_random_uuid()")
_EMPTY_JSONB = text("'{}'::jsonb")

QUESTION_TEXT_MAX_LENGTH = 1000
ANSWER_BODY_MAX_LENGTH = 2000
MAX_ROUTING_STEP = 4
HUMAN_CONTACT_LEASE_WHERE = "human_contact AND status IN ('claimed', 'waiting_for_user')"
ACCEPTED_ANSWER_WHERE = "status = 'accepted'"


class Question(TimestampMixin, Base):
    """A requester's question about a profile: open -> answered -> closed, or terminal declined,
    expired or cancelled (``closed_at`` is set exactly when the question is no longer open or
    answered).

    ``requested_information`` is what the requester wants to know. ``private_purpose`` is the
    requester's restricted context: it is kept separate from the question content and is NEVER
    shown to respondents or copied elsewhere. Respondents see ``minimized_question``, Revel's
    purpose-minimized text set after routing. ``routing_step`` is the routing ladder position:
    1 profile, 2 owner's agents, 3 owner via lease, 4 Trusted Circle (stretch). Escalation
    permission and context are stored as given; Trusted Circle escalation is off in the MVP.

    ``private_purpose`` and ``escalation_context`` are ORM-deferred with raiseload: a plain
    ``select(Question)`` / ``session.get(Question, ...)`` does not load them, and accessing them
    raises instead of lazy-loading. F7 must explicitly ``undefer`` them (e.g.
    ``options(undefer(Question.private_purpose))``) only in requester/router-only code paths;
    respondent-facing reads should select explicit columns.
    """

    __tablename__ = "questions"
    __table_args__ = (
        CheckConstraint(
            f"char_length(requested_information) BETWEEN 1 AND {QUESTION_TEXT_MAX_LENGTH}",
            name="requested_information_length",
        ),
        CheckConstraint(
            f"char_length(minimized_question) <= {QUESTION_TEXT_MAX_LENGTH}",
            name="minimized_question_length",
        ),
        CheckConstraint(check_in("status", QuestionStatus), name="status"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        CheckConstraint(
            f"routing_step BETWEEN 1 AND {MAX_ROUTING_STEP}", name="routing_step_range"
        ),
        CheckConstraint(
            "(status IN ('open', 'answered')) = (closed_at IS NULL)",
            name="closed_at_matches_status",
        ),
        # The requesting delegation must belong to the requester (MATCH SIMPLE: NULL allowed).
        ForeignKeyConstraint(
            ["requester_delegation_id", "requester_user_id"],
            ["agent_delegations.id", "agent_delegations.user_id"],
            name="fk_questions_requester_delegation_id_agent_delegations",
            ondelete="RESTRICT",
        ),
        # Target of the same-profile composite FK from answers.
        UniqueConstraint("profile_id", "id"),
        Index("ix_questions_profile_id_status", "profile_id", "status"),
        Index("ix_questions_requester_user_id_status", "requester_user_id", "status"),
        # Requester rate limits ("N questions per window").
        Index("ix_questions_requester_user_id_created_at", "requester_user_id", "created_at"),
        Index("ix_questions_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    # The profile (person) the question is about.
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    requester_delegation_id: Mapped[uuid.UUID | None] = mapped_column()
    requested_information: Mapped[str] = mapped_column(Text, nullable=False)
    # Restricted: never shown to respondents. Deferred with raiseload (see class docstring).
    private_purpose: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=_EMPTY_JSONB,
        deferred=True,
        deferred_raiseload=True,
    )
    minimized_question: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=QuestionStatus.OPEN.value
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    escalation_permitted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    escalation_context: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, deferred=True, deferred_raiseload=True
    )
    routing_step: Mapped[int | None] = mapped_column(SmallInteger)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Assignment(TimestampMixin, Base):
    """One respondent delegation's lease on a question.

    available -> claimed -> waiting_for_user (optional) -> completed; other exits released,
    declined, lease_expired. Claims, renewals and completions are optimistic
    ``UPDATE ... WHERE id = :id AND lease_version = :v`` statements that bump ``lease_version``,
    so a stale completion affects no row (F7 owns the logic, including returning an expired lease
    to ``available`` only while the question has not expired). Only one lease per question may
    contact the human (``human_contact``); ``waiting_for_user`` records when the user was
    contacted and whether delivery was confirmed (the contacting agent is the respondent
    delegation).
    """

    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint(check_in("status", AssignmentStatus), name="status"),
        CheckConstraint("lease_version >= 0", name="lease_version_non_negative"),
        CheckConstraint(
            "status NOT IN ('claimed', 'waiting_for_user') "
            "OR (lease_expires_at IS NOT NULL AND claimed_at IS NOT NULL)",
            name="lease_when_claimed",
        ),
        CheckConstraint(
            "status <> 'waiting_for_user' OR (human_contact AND user_contacted_at IS NOT NULL)",
            name="waiting_requires_contact",
        ),
        CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)",
            name="completed_at_matches_status",
        ),
        UniqueConstraint("question_id", "respondent_delegation_id"),
        # Target of the same-question composite FK from question_events.
        UniqueConstraint("question_id", "id"),
        # Target of the answers FK: an answer may only cite a lease its own delegation holds.
        UniqueConstraint("question_id", "id", "respondent_delegation_id"),
        Index(
            "uq_assignments_question_id_human_contact",
            "question_id",
            unique=True,
            postgresql_where=text(HUMAN_CONTACT_LEASE_WHERE),
        ),
        Index(
            "ix_assignments_respondent_delegation_id_status", "respondent_delegation_id", "status"
        ),
        Index("ix_assignments_status_lease_expires_at", "status", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False
    )
    respondent_delegation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_delegations.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=AssignmentStatus.AVAILABLE.value
    )
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    human_contact: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    user_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contact_delivery_confirmed: Mapped[bool | None] = mapped_column(Boolean)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QuestionEvent(Base):
    """Append-only evidence of what happened to a question; record only what is evidenced.

    ``details`` holds small structured metadata; never copy ``private_purpose``, question or
    answer text into it.
    """

    __tablename__ = "question_events"
    __table_args__ = (
        CheckConstraint(check_in("event_type", QuestionEventType), name="event_type"),
        # MATCH SIMPLE skips the composite FK when actor_user_id is NULL, so require it.
        CheckConstraint(
            "actor_delegation_id IS NULL OR actor_user_id IS NOT NULL",
            name="delegation_requires_actor",
        ),
        # The assignment must belong to the event's question (MATCH SIMPLE: NULL allowed).
        ForeignKeyConstraint(
            ["question_id", "assignment_id"],
            ["assignments.question_id", "assignments.id"],
            name="fk_question_events_assignment_id_assignments",
            ondelete="RESTRICT",
        ),
        # The delegation must belong to the actor.
        ForeignKeyConstraint(
            ["actor_delegation_id", "actor_user_id"],
            ["agent_delegations.id", "agent_delegations.user_id"],
            name="fk_question_events_actor_delegation_id_agent_delegations",
            ondelete="RESTRICT",
        ),
        Index("ix_question_events_question_id_occurred_at", "question_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False
    )
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    actor_delegation_id: Mapped[uuid.UUID | None] = mapped_column()
    lease_version: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Answer(TimestampMixin, Base):
    """A respondent's answer to a question (one accepted answer per question in the MVP).

    ``audience`` stores the most restrictive audience of the answer's evidence for audit;
    permissions are re-checked on every retrieval. The asserting agent/delegation comes from
    authentication, never from the payload.
    """

    __tablename__ = "answers"
    __table_args__ = (
        CheckConstraint(
            f"char_length(body) BETWEEN 1 AND {ANSWER_BODY_MAX_LENGTH}", name="body_length"
        ),
        CheckConstraint(check_in("audience", Audience), name="audience"),
        CheckConstraint(check_in("status", AnswerStatus), name="status"),
        # MATCH SIMPLE skips the lease FK when the delegation is NULL, so require it.
        CheckConstraint(
            "assignment_id IS NULL OR respondent_delegation_id IS NOT NULL",
            name="assignment_requires_delegation",
        ),
        # The answer's profile must be its question's profile.
        ForeignKeyConstraint(
            ["profile_id", "question_id"],
            ["questions.profile_id", "questions.id"],
            name="fk_answers_question_id_questions",
            ondelete="RESTRICT",
        ),
        # The assignment must belong to the answer's question and be held by the answer's
        # delegation (MATCH SIMPLE: NULL assignment allowed).
        ForeignKeyConstraint(
            ["question_id", "assignment_id", "respondent_delegation_id"],
            ["assignments.question_id", "assignments.id", "assignments.respondent_delegation_id"],
            name="fk_answers_assignment_id_assignments",
            ondelete="RESTRICT",
        ),
        # The delegation must belong to the respondent (MATCH SIMPLE: NULL allowed).
        ForeignKeyConstraint(
            ["respondent_delegation_id", "respondent_user_id"],
            ["agent_delegations.id", "agent_delegations.user_id"],
            name="fk_answers_respondent_delegation_id_agent_delegations",
            ondelete="RESTRICT",
        ),
        # Target of the same-profile composite FK from answer_evidence.
        UniqueConstraint("profile_id", "id"),
        Index(
            "uq_answers_question_id_accepted",
            "question_id",
            unique=True,
            postgresql_where=text(ACCEPTED_ANSWER_WHERE),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    question_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    # Denormalized from the question (kept consistent by the composite FK).
    profile_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    assignment_id: Mapped[uuid.UUID | None] = mapped_column()
    respondent_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    respondent_delegation_id: Mapped[uuid.UUID | None] = mapped_column()
    body: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=AnswerStatus.ACCEPTED.value
    )


class AnswerEvidence(Base):
    """One evidence item for an answer: EITHER a reference to a claim of the same profile OR
    inline evidence with only category, proposed audience, evidence type and approximate observed
    date (month is enough).

    Inline evidence stays scoped to the question and never becomes a claim. No quotes, raw
    conversation or source details are stored. Append-only (no ``updated_at``).
    """

    __tablename__ = "answer_evidence"
    __table_args__ = (
        CheckConstraint(
            f"char_length(category) BETWEEN 1 AND {CATEGORY_MAX_LENGTH}", name="category_length"
        ),
        CheckConstraint(check_in("proposed_audience", Audience), name="proposed_audience"),
        CheckConstraint(check_in("evidence_type", EvidenceType), name="evidence_type"),
        CheckConstraint(
            check_in("observed_precision", ObservedPrecision), name="observed_precision"
        ),
        CheckConstraint(
            "(claim_id IS NOT NULL AND category IS NULL AND proposed_audience IS NULL "
            "AND evidence_type IS NULL AND observed_at IS NULL AND observed_precision IS NULL) "
            "OR (claim_id IS NULL AND category IS NOT NULL AND proposed_audience IS NOT NULL "
            "AND evidence_type IS NOT NULL AND observed_precision IS NOT NULL)",
            name="claim_or_inline",
        ),
        CheckConstraint(
            "observed_precision IS NULL "
            "OR ((observed_precision = 'unknown') = (observed_at IS NULL))",
            name="observed_at_matches_precision",
        ),
        # The evidence's profile must be its answer's profile.
        ForeignKeyConstraint(
            ["profile_id", "answer_id"],
            ["answers.profile_id", "answers.id"],
            name="fk_answer_evidence_answer_id_answers",
            ondelete="RESTRICT",
        ),
        # Only a claim of the profile the question is about may be cited (MATCH SIMPLE).
        ForeignKeyConstraint(
            ["profile_id", "claim_id"],
            ["claims.profile_id", "claims.id"],
            name="fk_answer_evidence_claim_id_claims",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    answer_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    # Denormalized from the answer (kept consistent by the composite FK).
    profile_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    claim_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    category: Mapped[str | None] = mapped_column(Text)
    proposed_audience: Mapped[str | None] = mapped_column(Text)
    evidence_type: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[date | None] = mapped_column(Date)
    observed_precision: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
