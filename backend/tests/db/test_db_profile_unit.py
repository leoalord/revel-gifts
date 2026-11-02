"""Profile schema checks that never connect to a database."""

import re
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from revel.db.base import Base
from revel.db.enums import (
    LIVE_CLAIM_STATUSES,
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
from revel.db.models import Claim, ClaimEvent, ContributionPolicy, Profile

PROFILE_TABLES = {"profiles", "contribution_policies", "claims", "claim_events"}
MIGRATION_0002 = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0002_profile_schema.py"
)


def test_profile_enum_values() -> None:
    assert [s.value for s in ClaimStatus] == [
        "active",
        "provisional",
        "pending_review",
        "superseded",
        "removed",
    ]
    assert LIVE_CLAIM_STATUSES == (ClaimStatus.ACTIVE, ClaimStatus.PROVISIONAL)
    assert [e.value for e in EvidenceType] == ["explicit", "observed", "inferred"]
    assert [c.value for c in Confidence] == ["low", "medium", "high", "unknown"]
    assert [p.value for p in ObservedPrecision] == ["day", "month", "year", "unknown"]
    assert [v.value for v in ContributedVia] == ["mcp", "api", "ui"]
    assert [a.value for a in ClaimEventAction] == [
        "created",
        "auto_accepted",
        "approved",
        "rejected",
        "updated",
        "audience_changed",
        "status_changed",
        "superseded",
        "removed",
        "restored",
    ]
    assert [m.value for m in AcceptanceMode] == ["automatic", "explicit"]
    # Unchanged contract for F4.
    assert [a.rank for a in Audience] == [0, 1, 2, 3]


def _check_sql(table, name: str) -> str:
    for constraint in table.constraints:
        if constraint.name in (name, f"ck_{table.name}_{name}"):
            return str(constraint.sqltext)
    raise AssertionError(f"missing {name}")


def test_check_expressions_built_from_enums() -> None:
    claims = Claim.__table__
    for column, enum in (
        ("audience", Audience),
        ("evidence_type", EvidenceType),
        ("confidence", Confidence),
        ("observed_precision", ObservedPrecision),
        ("status", ClaimStatus),
        ("contributed_via", ContributedVia),
    ):
        assert _check_sql(claims, column) == check_in(column, enum)
    events = ClaimEvent.__table__
    assert _check_sql(events, "action") == check_in("action", ClaimEventAction)
    assert _check_sql(events, "acceptance") == check_in("acceptance", AcceptanceMode)


def test_migration_check_literals_match_enums() -> None:
    """0002 writes CHECK expressions literally; they must equal the enum-built ones."""
    source = MIGRATION_0002.read_text()
    flat = re.sub(r'"\s*\n\s*"', "", source)  # join implicitly concatenated string literals
    for column, enum in (
        ("audience", Audience),
        ("evidence_type", EvidenceType),
        ("confidence", Confidence),
        ("observed_precision", ObservedPrecision),
        ("status", ClaimStatus),
        ("contributed_via", ContributedVia),
        ("action", ClaimEventAction),
        ("acceptance", AcceptanceMode),
    ):
        assert check_in(column, enum) in flat, column


def _ddl() -> str:
    dialect = postgresql.dialect()
    tables = [t for t in Base.metadata.sorted_tables if t.name in PROFILE_TABLES]
    parts = [str(CreateTable(t).compile(dialect=dialect)) for t in tables]
    parts += [str(CreateIndex(ix).compile(dialect=dialect)) for t in tables for ix in t.indexes]
    return "\n".join(parts)


def test_profile_constraint_and_index_names() -> None:
    assert set(Base.metadata.tables) == {"users", "agents", "agent_delegations"} | PROFILE_TABLES
    ddl = _ddl()
    for name in (
        "pk_profiles",
        "uq_profiles_user_id",
        "fk_profiles_user_id_users",
        "pk_contribution_policies",
        "uq_contribution_policies_profile_id_version",
        "fk_contribution_policies_profile_id_profiles",
        "fk_contribution_policies_approved_by_user_id_users",
        "ck_contribution_policies_version_positive",
        "pk_claims",
        "fk_claims_profile_id_profiles",
        "fk_claims_contributor_user_id_users",
        "fk_claims_contributor_delegation_id_agent_delegations",
        "fk_claims_supersedes_id_claims",
        "fk_claims_profile_id_contribution_policies",
        "uq_claims_profile_id_id",
        "ck_claims_category_length",
        "ck_claims_statement_length",
        "ck_claims_audience",
        "ck_claims_evidence_type",
        "ck_claims_confidence",
        "ck_claims_observed_precision",
        "ck_claims_observed_at_matches_precision",
        "ck_claims_status",
        "ck_claims_contributed_via",
        "ck_claims_mcp_requires_delegation",
        "ck_claims_not_self_supersede",
        "ix_claims_profile_id_status_audience",
        "ix_claims_profile_id_category",
        "ix_claims_supersedes_id",
        "ix_claims_contributor_delegation_id",
        "uq_claims_supersedes_id_live",
        "pk_claim_events",
        "fk_claim_events_claim_id_claims",
        "fk_claim_events_profile_id_profiles",
        "fk_claim_events_actor_user_id_users",
        "fk_claim_events_actor_delegation_id_agent_delegations",
        "fk_claim_events_policy_version_contribution_policies",
        "ck_claim_events_action",
        "ck_claim_events_acceptance",
        "ix_claim_events_claim_id",
        "ix_claim_events_profile_id_occurred_at",
    ):
        assert name in ddl, name
    assert ddl.count("ON DELETE RESTRICT") == 13
    # Same-profile composite FKs.
    assert (
        ddl.count(
            "FOREIGN KEY(profile_id, policy_version) REFERENCES contribution_policies "
            "(profile_id, version)"
        )
        == 2
    )
    assert (
        "CONSTRAINT fk_claims_supersedes_id_claims FOREIGN KEY(profile_id, supersedes_id) "
        "REFERENCES claims (profile_id, id)" in ddl
    )
    assert (
        "CONSTRAINT fk_claim_events_claim_id_claims FOREIGN KEY(profile_id, claim_id) "
        "REFERENCES claims (profile_id, id)" in ddl
    )
    assert "CONSTRAINT uq_claims_profile_id_id UNIQUE (profile_id, id)" in ddl
    assert (
        "CREATE UNIQUE INDEX uq_claims_supersedes_id_live ON claims (supersedes_id) "
        "WHERE status IN ('active', 'provisional') AND supersedes_id IS NOT NULL" in ddl
    )
    assert (
        "CREATE INDEX ix_claims_profile_id_status_audience ON claims "
        "(profile_id, status, audience)" in ddl
    )


def test_profile_model_columns() -> None:
    profiles = Profile.__table__.c
    assert profiles.user_id.unique and not profiles.user_id.nullable

    policies = ContributionPolicy.__table__.c
    assert "updated_at" not in policies  # append-only
    assert policies.public_opt_in.server_default.arg.text == "false"
    assert policies.share_provisional.server_default.arg.text == "true"
    assert policies.require_review_all.server_default.arg.text == "false"
    assert policies.category_settings.server_default.arg.text == "'{}'::jsonb"
    assert not policies.category_settings.nullable

    claims = Claim.__table__.c
    assert "agent_id" not in claims
    assert claims.confidence.server_default.arg == "unknown"
    assert claims.observed_precision.server_default.arg == "unknown"
    for nullable in (
        "details",
        "observed_at",
        "contributor_delegation_id",
        "policy_version",
        "supersedes_id",
    ):
        assert claims[nullable].nullable, nullable
    for required in (
        "profile_id",
        "category",
        "statement",
        "audience",
        "evidence_type",
        "status",
        "contributor_user_id",
        "contributed_via",
    ):
        assert not claims[required].nullable, required
    assert claims.updated_at.onupdate is not None

    events = ClaimEvent.__table__.c
    assert set(events.keys()) == {
        "id",
        "claim_id",
        "profile_id",
        "action",
        "actor_user_id",
        "actor_delegation_id",
        "acceptance",
        "policy_version",
        "changes",
        "occurred_at",
    }
    assert "statement" not in events
