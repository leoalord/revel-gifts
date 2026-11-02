"""Profile schema: profiles, contribution_policies, claims, claim_events.

Revision ID: 0002_profile
Revises: 0001_identity
Create Date: 2026-11-01

Hand-written to match revel.db.models.profile. Constraint names follow
revel.db.base.NAMING_CONVENTION and are written out literally (op.f marks them final), and CHECK
expressions are literal strings, so this migration stays fixed if models or enums change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_profile"
down_revision: str | Sequence[str] | None = "0001_identity"
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


def upgrade() -> None:
    op.create_table(
        "profiles",
        _id(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profiles")),
        _fk("profiles", "user_id", "users"),
        sa.UniqueConstraint("user_id", name=op.f("uq_profiles_user_id")),
    )
    op.create_table(
        "contribution_policies",
        _id(),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=False),
        _now("approved_at"),
        sa.Column("public_opt_in", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "share_provisional", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "require_review_all", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "category_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contribution_policies")),
        _fk("contribution_policies", "profile_id", "profiles"),
        _fk("contribution_policies", "approved_by_user_id", "users"),
        sa.UniqueConstraint(
            "profile_id", "version", name=op.f("uq_contribution_policies_profile_id_version")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_contribution_policies_version_positive")),
    )
    op.create_table(
        "claims",
        _id(),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("audience", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("observed_at", sa.Date(), nullable=True),
        sa.Column("observed_precision", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("contributor_user_id", sa.Uuid(), nullable=False),
        sa.Column("contributor_delegation_id", sa.Uuid(), nullable=True),
        sa.Column("contributed_via", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
        _fk("claims", "profile_id", "profiles"),
        _fk("claims", "contributor_user_id", "users"),
        _fk("claims", "contributor_delegation_id", "agent_delegations"),
        _fk("claims", "supersedes_id", "claims"),
        sa.ForeignKeyConstraint(
            ["profile_id", "policy_version"],
            ["contribution_policies.profile_id", "contribution_policies.version"],
            name=op.f("fk_claims_profile_id_contribution_policies"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "char_length(category) BETWEEN 1 AND 64", name=op.f("ck_claims_category_length")
        ),
        sa.CheckConstraint(
            "char_length(statement) BETWEEN 1 AND 500", name=op.f("ck_claims_statement_length")
        ),
        sa.CheckConstraint(
            "audience IN ('public', 'connections', 'trusted_circle', 'only_me')",
            name=op.f("ck_claims_audience"),
        ),
        sa.CheckConstraint(
            "evidence_type IN ('explicit', 'observed', 'inferred')",
            name=op.f("ck_claims_evidence_type"),
        ),
        sa.CheckConstraint(
            "confidence IN ('low', 'medium', 'high', 'unknown')",
            name=op.f("ck_claims_confidence"),
        ),
        sa.CheckConstraint(
            "observed_precision IN ('day', 'month', 'year', 'unknown')",
            name=op.f("ck_claims_observed_precision"),
        ),
        sa.CheckConstraint(
            "(observed_precision = 'unknown') = (observed_at IS NULL)",
            name=op.f("ck_claims_observed_at_matches_precision"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'provisional', 'pending_review', 'superseded', 'removed')",
            name=op.f("ck_claims_status"),
        ),
        sa.CheckConstraint(
            "contributed_via IN ('mcp', 'api', 'ui')", name=op.f("ck_claims_contributed_via")
        ),
        sa.CheckConstraint(
            "contributed_via <> 'mcp' OR contributor_delegation_id IS NOT NULL",
            name=op.f("ck_claims_mcp_requires_delegation"),
        ),
        sa.CheckConstraint(
            "supersedes_id IS NULL OR supersedes_id <> id",
            name=op.f("ck_claims_not_self_supersede"),
        ),
    )
    op.create_index(
        op.f("ix_claims_profile_id_status_audience"),
        "claims",
        ["profile_id", "status", "audience"],
    )
    op.create_index(op.f("ix_claims_profile_id_category"), "claims", ["profile_id", "category"])
    op.create_index(op.f("ix_claims_supersedes_id"), "claims", ["supersedes_id"])
    op.create_index(
        op.f("ix_claims_contributor_delegation_id"), "claims", ["contributor_delegation_id"]
    )
    op.create_index(
        op.f("uq_claims_supersedes_id_live"),
        "claims",
        ["supersedes_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('active', 'provisional') AND supersedes_id IS NOT NULL"
        ),
    )
    op.create_table(
        "claim_events",
        _id(),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_delegation_id", sa.Uuid(), nullable=True),
        sa.Column("acceptance", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _now("occurred_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_events")),
        _fk("claim_events", "claim_id", "claims"),
        _fk("claim_events", "profile_id", "profiles"),
        _fk("claim_events", "actor_user_id", "users"),
        _fk("claim_events", "actor_delegation_id", "agent_delegations"),
        sa.CheckConstraint(
            "action IN ('created', 'auto_accepted', 'approved', 'rejected', 'updated', "
            "'audience_changed', 'status_changed', 'superseded', 'removed', 'restored')",
            name=op.f("ck_claim_events_action"),
        ),
        sa.CheckConstraint(
            "acceptance IN ('automatic', 'explicit')", name=op.f("ck_claim_events_acceptance")
        ),
    )
    op.create_index(op.f("ix_claim_events_claim_id"), "claim_events", ["claim_id"])
    op.create_index(
        op.f("ix_claim_events_profile_id_occurred_at"),
        "claim_events",
        ["profile_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_claim_events_profile_id_occurred_at"), table_name="claim_events")
    op.drop_index(op.f("ix_claim_events_claim_id"), table_name="claim_events")
    op.drop_table("claim_events")
    op.drop_index(op.f("uq_claims_supersedes_id_live"), table_name="claims")
    op.drop_index(op.f("ix_claims_contributor_delegation_id"), table_name="claims")
    op.drop_index(op.f("ix_claims_supersedes_id"), table_name="claims")
    op.drop_index(op.f("ix_claims_profile_id_category"), table_name="claims")
    op.drop_index(op.f("ix_claims_profile_id_status_audience"), table_name="claims")
    op.drop_table("claims")
    op.drop_table("contribution_policies")
    op.drop_table("profiles")
