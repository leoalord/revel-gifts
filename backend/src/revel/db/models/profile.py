"""Profile schema: profiles, versioned contribution policies, profile facts (claims) and their
audit events.

Never stored anywhere in this schema: raw conversations, quotes, conversation IDs or numeric
confidence. ``claims.category`` values come from F5's central list and are validated in the
service (no CHECK); the shapes of ``claims.details`` and ``contribution_policies.category_settings``
are also validated there. Accepting a replacement (``supersedes_id``) and retiring the old fact
happen in one service transaction (F5); the partial unique index only guarantees at most one live
replacement per fact.
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
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from revel.db.base import Base, TimestampMixin
from revel.db.enums import (
    AcceptanceMode,
    Audience,
    ClaimEventAction,
    ClaimStatus,
    Confidence,
    ContributedVia,
    EvidenceType,
    ObservedPrecision,
    check_in,
)

_UUID_DEFAULT = text("gen_random_uuid()")
_EMPTY_JSONB = text("'{}'::jsonb")

CATEGORY_MAX_LENGTH = 64
STATEMENT_MAX_LENGTH = 500
LIVE_REPLACEMENT_WHERE = "status IN ('active', 'provisional') AND supersedes_id IS NOT NULL"


class Profile(TimestampMixin, Base):
    """A user's gifting profile; exactly one per user (the owner)."""

    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), unique=True, nullable=False
    )


class ContributionPolicy(Base):
    """Owner-approved contribution policy.

    Append-only: the current policy is the row with the highest ``version`` for the profile, and
    changing the policy inserts a new version (hence no ``updated_at``).
    """

    __tablename__ = "contribution_policies"
    __table_args__ = (
        UniqueConstraint("profile_id", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    public_opt_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    share_provisional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    require_review_all: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Shape owned by F5 and validated in the service.
    category_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_EMPTY_JSONB
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Claim(TimestampMixin, Base):
    """One profile fact: a short actionable ``statement`` with audience, evidence and status.

    The contributor is always an authenticated human (``contributor_user_id``). Agent
    contributions also carry the delegation ("which user's which assistant"), which is required
    when ``contributed_via = 'mcp'``; owner UI edits need none. ``contributed_via`` is metadata,
    not proof. ``policy_version`` cites a version of this profile's own contribution policy
    (NULL for owner edits made before any policy exists). ``observed_at`` is the evidence date;
    for ``month``/``year`` precision only those parts are meaningful.
    """

    __tablename__ = "claims"
    __table_args__ = (
        CheckConstraint(
            f"char_length(category) BETWEEN 1 AND {CATEGORY_MAX_LENGTH}", name="category_length"
        ),
        CheckConstraint(
            f"char_length(statement) BETWEEN 1 AND {STATEMENT_MAX_LENGTH}",
            name="statement_length",
        ),
        CheckConstraint(check_in("audience", Audience), name="audience"),
        CheckConstraint(check_in("evidence_type", EvidenceType), name="evidence_type"),
        CheckConstraint(check_in("confidence", Confidence), name="confidence"),
        CheckConstraint(
            check_in("observed_precision", ObservedPrecision), name="observed_precision"
        ),
        CheckConstraint(
            "(observed_precision = 'unknown') = (observed_at IS NULL)",
            name="observed_at_matches_precision",
        ),
        CheckConstraint(check_in("status", ClaimStatus), name="status"),
        CheckConstraint(check_in("contributed_via", ContributedVia), name="contributed_via"),
        CheckConstraint(
            "contributed_via <> 'mcp' OR contributor_delegation_id IS NOT NULL",
            name="mcp_requires_delegation",
        ),
        CheckConstraint("supersedes_id IS NULL OR supersedes_id <> id", name="not_self_supersede"),
        # Target of the same-profile composite FKs below and from claim_events.
        UniqueConstraint("profile_id", "id"),
        # A fact may only replace a fact of the same profile (MATCH SIMPLE: NULL allowed).
        ForeignKeyConstraint(
            ["profile_id", "supersedes_id"],
            ["claims.profile_id", "claims.id"],
            name="fk_claims_supersedes_id_claims",
            ondelete="RESTRICT",
        ),
        # Only this profile's policy versions may be cited (MATCH SIMPLE: NULL version allowed).
        ForeignKeyConstraint(
            ["profile_id", "policy_version"],
            ["contribution_policies.profile_id", "contribution_policies.version"],
            ondelete="RESTRICT",
        ),
        # The delegation must belong to the contributor (MATCH SIMPLE: NULL delegation allowed).
        ForeignKeyConstraint(
            ["contributor_delegation_id", "contributor_user_id"],
            ["agent_delegations.id", "agent_delegations.user_id"],
            name="fk_claims_contributor_delegation_id_agent_delegations",
            ondelete="RESTRICT",
        ),
        # F4 read path: WHERE profile_id = ? AND status IN (...) AND audience IN (...).
        Index("ix_claims_profile_id_status_audience", "profile_id", "status", "audience"),
        Index("ix_claims_profile_id_category", "profile_id", "category"),
        # At most one live replacement per fact.
        Index(
            "uq_claims_supersedes_id_live",
            "supersedes_id",
            unique=True,
            postgresql_where=text(LIVE_REPLACEMENT_WHERE),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    # Stable identifier from F5's central category list; validated in the service.
    category: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional structured details, validated per category in the service.
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    audience: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_type: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=Confidence.UNKNOWN.value
    )
    observed_at: Mapped[date | None] = mapped_column(Date)
    observed_precision: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=ObservedPrecision.UNKNOWN.value
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    contributor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    contributor_delegation_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    contributed_via: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[int | None] = mapped_column(Integer)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(index=True)


class ClaimEvent(Base):
    """Append-only audit log of a claim's lifecycle; powers the contribution summary.

    Records the actor, action, time, policy version and whether acceptance was automatic or
    explicitly approved. ``changes`` holds field names with old/new values for ``audience`` and
    ``status`` only, e.g. ``{"audience": {"old": "connections", "new": "only_me"}}``. Statement
    text, details or any other private content must never be copied into this table.
    """

    __tablename__ = "claim_events"
    __table_args__ = (
        CheckConstraint(check_in("action", ClaimEventAction), name="action"),
        CheckConstraint(check_in("acceptance", AcceptanceMode), name="acceptance"),
        # The event's profile must be its claim's profile.
        ForeignKeyConstraint(
            ["profile_id", "claim_id"],
            ["claims.profile_id", "claims.id"],
            name="fk_claim_events_claim_id_claims",
            ondelete="RESTRICT",
        ),
        # Only this profile's policy versions may be cited (MATCH SIMPLE: NULL version allowed).
        ForeignKeyConstraint(
            ["profile_id", "policy_version"],
            ["contribution_policies.profile_id", "contribution_policies.version"],
            name="fk_claim_events_policy_version_contribution_policies",
            ondelete="RESTRICT",
        ),
        # The delegation must belong to the actor (MATCH SIMPLE: NULL delegation allowed).
        ForeignKeyConstraint(
            ["actor_delegation_id", "actor_user_id"],
            ["agent_delegations.id", "agent_delegations.user_id"],
            name="fk_claim_events_actor_delegation_id_agent_delegations",
            ondelete="RESTRICT",
        ),
        Index("ix_claim_events_profile_id_occurred_at", "profile_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    claim_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    # Denormalized from the claim for the per-profile contribution summary (kept consistent by
    # the composite FK to claims).
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_delegation_id: Mapped[uuid.UUID | None] = mapped_column()
    acceptance: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[int | None] = mapped_column(Integer)
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
