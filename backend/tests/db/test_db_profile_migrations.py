"""Profile schema migrations and constraints against PostgreSQL (REVEL_TEST_DATABASE_URL)."""

import uuid
from collections.abc import Callable, Iterator
from datetime import date

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, delete, inspect, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from revel.db.base import Base
from revel.db.enums import LIVE_CLAIM_STATUSES, Audience
from revel.db.models import (
    Agent,
    AgentDelegation,
    Claim,
    ClaimEvent,
    ContributionPolicy,
    Profile,
    User,
)

RunAlembic = Callable[..., None]
IDENTITY_TABLES = {"users", "agents", "agent_delegations"}
PROFILE_TABLES = {"profiles", "contribution_policies", "claims", "claim_events"}


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


def _delegation(conn: Connection, user_id: uuid.UUID) -> uuid.UUID:
    agent_id = conn.execute(
        insert(Agent).values(oauth_client_id=f"client_{uuid.uuid4().hex}").returning(Agent.id)
    ).scalar_one()
    return conn.execute(
        insert(AgentDelegation)
        .values(user_id=user_id, agent_id=agent_id)
        .returning(AgentDelegation.id)
    ).scalar_one()


def _profile(conn: Connection, user_id: uuid.UUID | None = None) -> tuple[uuid.UUID, uuid.UUID]:
    owner = user_id or _user(conn)
    profile_id = conn.execute(
        insert(Profile).values(user_id=owner).returning(Profile.id)
    ).scalar_one()
    return profile_id, owner


def _policy(conn: Connection, profile_id: uuid.UUID, owner: uuid.UUID, version: int = 1) -> None:
    conn.execute(
        insert(ContributionPolicy).values(
            profile_id=profile_id, version=version, approved_by_user_id=owner
        )
    )


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


def _event(conn: Connection, claim_id: uuid.UUID, profile_id: uuid.UUID, actor, **values) -> None:
    row = {"claim_id": claim_id, "profile_id": profile_id, "action": "created"}
    row.update(values)
    conn.execute(insert(ClaimEvent).values(actor_user_id=actor, **row))


def _rejects(conn: Connection, constraint: str, fn: Callable[[], object]) -> None:
    with pytest.raises(IntegrityError, match=constraint), conn.begin_nested():
        fn()


def test_profile_schema_after_upgrade(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    assert set(inspector.get_table_names()) >= IDENTITY_TABLES | PROFILE_TABLES

    assert {c["name"] for c in inspector.get_columns("profiles")} == {
        "id",
        "user_id",
        "created_at",
        "updated_at",
    }
    policy_cols = {c["name"]: c for c in inspector.get_columns("contribution_policies")}
    assert set(policy_cols) == {
        "id",
        "profile_id",
        "version",
        "approved_by_user_id",
        "approved_at",
        "public_opt_in",
        "share_provisional",
        "require_review_all",
        "category_settings",
        "created_at",
        # Added by 0003_connections_questions.
        "enrichment_enabled",
        "questions_enabled",
        "question_settings",
    }
    assert policy_cols["public_opt_in"]["default"] == "false"
    assert policy_cols["share_provisional"]["default"] == "true"
    assert policy_cols["require_review_all"]["default"] == "false"
    assert policy_cols["category_settings"]["default"] == "'{}'::jsonb"
    assert "now()" in policy_cols["approved_at"]["default"]

    claim_cols = {c["name"]: c for c in inspector.get_columns("claims")}
    assert set(claim_cols) == {
        "id",
        "profile_id",
        "category",
        "statement",
        "details",
        "audience",
        "evidence_type",
        "confidence",
        "observed_at",
        "observed_precision",
        "status",
        "contributor_user_id",
        "contributor_delegation_id",
        "contributed_via",
        "policy_version",
        "supersedes_id",
        "created_at",
        "updated_at",
    }
    assert "'unknown'" in claim_cols["confidence"]["default"]
    assert "'unknown'" in claim_cols["observed_precision"]["default"]
    for nullable in ("details", "observed_at", "contributor_delegation_id", "policy_version"):
        assert claim_cols[nullable]["nullable"], nullable
    assert not claim_cols["statement"]["nullable"] and not claim_cols["audience"]["nullable"]

    event_cols = {c["name"]: c for c in inspector.get_columns("claim_events")}
    assert set(event_cols) == {
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
    assert "now()" in event_cols["occurred_at"]["default"]
    assert event_cols["occurred_at"]["type"].timezone

    assert {c["name"] for c in inspector.get_check_constraints("claims")} == {
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
    }
    assert {c["name"] for c in inspector.get_check_constraints("claim_events")} == {
        "ck_claim_events_action",
        "ck_claim_events_acceptance",
    }
    assert {c["name"] for c in inspector.get_check_constraints("contribution_policies")} == {
        "ck_contribution_policies_version_positive"
    }
    assert {u["name"]: u["column_names"] for u in inspector.get_unique_constraints("profiles")} == {
        "uq_profiles_user_id": ["user_id"]
    }
    assert {
        u["name"]: u["column_names"]
        for u in inspector.get_unique_constraints("contribution_policies")
    } == {"uq_contribution_policies_profile_id_version": ["profile_id", "version"]}
    assert {u["name"]: u["column_names"] for u in inspector.get_unique_constraints("claims")} == {
        "uq_claims_profile_id_id": ["profile_id", "id"]
    }

    expected_fks = {
        "profiles": {"fk_profiles_user_id_users"},
        "contribution_policies": {
            "fk_contribution_policies_profile_id_profiles",
            "fk_contribution_policies_approved_by_user_id_users",
        },
        "claims": {
            "fk_claims_profile_id_profiles",
            "fk_claims_contributor_user_id_users",
            "fk_claims_contributor_delegation_id_agent_delegations",
            "fk_claims_supersedes_id_claims",
            "fk_claims_profile_id_contribution_policies",
        },
        "claim_events": {
            "fk_claim_events_claim_id_claims",
            "fk_claim_events_profile_id_profiles",
            "fk_claim_events_actor_user_id_users",
            "fk_claim_events_actor_delegation_id_agent_delegations",
            "fk_claim_events_policy_version_contribution_policies",
        },
    }
    for table, names in expected_fks.items():
        fks = {fk["name"]: fk for fk in inspector.get_foreign_keys(table)}
        assert set(fks) == names, table
        for fk in fks.values():
            assert fk["options"].get("ondelete") == "RESTRICT", fk["name"]
    composite_fks = {
        ("claims", "fk_claims_profile_id_contribution_policies"): (
            ["profile_id", "policy_version"],
            "contribution_policies",
            ["profile_id", "version"],
        ),
        ("claims", "fk_claims_supersedes_id_claims"): (
            ["profile_id", "supersedes_id"],
            "claims",
            ["profile_id", "id"],
        ),
        ("claim_events", "fk_claim_events_claim_id_claims"): (
            ["profile_id", "claim_id"],
            "claims",
            ["profile_id", "id"],
        ),
        ("claim_events", "fk_claim_events_policy_version_contribution_policies"): (
            ["profile_id", "policy_version"],
            "contribution_policies",
            ["profile_id", "version"],
        ),
    }
    for (table, name), (columns, referred, referred_columns) in composite_fks.items():
        fk = {fk["name"]: fk for fk in inspector.get_foreign_keys(table)}[name]
        assert fk["constrained_columns"] == columns, name
        assert fk["referred_table"] == referred, name
        assert fk["referred_columns"] == referred_columns, name

    claim_indexes = {ix["name"]: ix for ix in inspector.get_indexes("claims")}
    assert claim_indexes["ix_claims_profile_id_status_audience"]["column_names"] == [
        "profile_id",
        "status",
        "audience",
    ]
    assert claim_indexes["ix_claims_profile_id_category"]["column_names"] == [
        "profile_id",
        "category",
    ]
    assert claim_indexes["ix_claims_supersedes_id"]["column_names"] == ["supersedes_id"]
    assert claim_indexes["ix_claims_contributor_delegation_id"]["column_names"] == [
        "contributor_delegation_id"
    ]
    live = claim_indexes["uq_claims_supersedes_id_live"]
    assert live["unique"] and live["column_names"] == ["supersedes_id"]
    where = live["dialect_options"]["postgresql_where"]
    assert "supersedes_id IS NOT NULL" in where and "'active'" in where and "'provisional'" in where
    event_indexes = {ix["name"]: ix["column_names"] for ix in inspector.get_indexes("claim_events")}
    assert event_indexes["ix_claim_events_claim_id"] == ["claim_id"]
    assert event_indexes["ix_claim_events_profile_id_occurred_at"] == ["profile_id", "occurred_at"]


def test_no_drift_with_profile_schema(migrated_engine: Engine, run_alembic: RunAlembic) -> None:
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
    run_alembic(command.check)


def test_defaults(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    _policy(conn, profile_id, owner)
    policy = conn.execute(
        select(ContributionPolicy).where(ContributionPolicy.profile_id == profile_id)
    ).one()
    assert policy.category_settings == {}
    assert (policy.public_opt_in, policy.share_provisional, policy.require_review_all) == (
        False,
        True,
        False,
    )
    assert policy.approved_at is not None and policy.created_at.tzinfo is not None

    claim_id = _claim(conn, profile_id, owner, details={"month": 4, "day": 12})
    claim = conn.execute(select(Claim).where(Claim.id == claim_id)).one()
    assert claim.confidence == "unknown" and claim.observed_precision == "unknown"
    assert claim.observed_at is None and claim.policy_version is None
    assert claim.details == {"month": 4, "day": 12}
    assert claim.created_at is not None and claim.updated_at is not None

    changes = {"audience": {"old": "connections", "new": "only_me"}}
    _event(conn, claim_id, profile_id, owner, action="audience_changed", changes=changes)
    event = conn.execute(select(ClaimEvent).where(ClaimEvent.claim_id == claim_id)).one()
    assert event.changes == changes and event.occurred_at is not None
    assert event.acceptance is None and event.actor_delegation_id is None


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("audience", "everyone"),
        ("status", "deleted"),
        ("evidence_type", "rumour"),
        ("confidence", "0.9"),
        ("contributed_via", "email"),
    ],
)
def test_invalid_claim_enum_rejected(conn: Connection, column: str, value: str) -> None:
    profile_id, owner = _profile(conn)
    _rejects(
        conn, f"ck_claims_{column}", lambda: _claim(conn, profile_id, owner, **{column: value})
    )


def test_invalid_observed_precision_rejected(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    _rejects(
        conn,
        "ck_claims_observed_precision",
        lambda: _claim(
            conn, profile_id, owner, observed_precision="week", observed_at=date(2026, 1, 1)
        ),
    )


@pytest.mark.parametrize(
    ("precision", "observed_at"),
    [("unknown", date(2026, 3, 1)), ("month", None), ("day", None)],
)
def test_observed_at_precision_consistency(conn: Connection, precision, observed_at) -> None:
    profile_id, owner = _profile(conn)
    _rejects(
        conn,
        "ck_claims_observed_at_matches_precision",
        lambda: _claim(
            conn, profile_id, owner, observed_precision=precision, observed_at=observed_at
        ),
    )


def test_observed_at_with_precision_accepted(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    _claim(conn, profile_id, owner, observed_precision="month", observed_at=date(2026, 3, 1))
    _claim(conn, profile_id, owner, observed_precision="unknown", observed_at=None)


@pytest.mark.parametrize("statement", ["", "x" * 501])
def test_statement_length_rejected(conn: Connection, statement: str) -> None:
    profile_id, owner = _profile(conn)
    _rejects(
        conn,
        "ck_claims_statement_length",
        lambda: _claim(conn, profile_id, owner, statement=statement),
    )


def test_statement_and_category_bounds(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    _claim(conn, profile_id, owner, statement="x" * 500, category="c" * 64)
    _rejects(
        conn, "ck_claims_category_length", lambda: _claim(conn, profile_id, owner, category="")
    )
    _rejects(
        conn,
        "ck_claims_category_length",
        lambda: _claim(conn, profile_id, owner, category="c" * 65),
    )
    # Category values themselves are validated in the service, not the database.
    _claim(conn, profile_id, owner, category="any_future_category")


def test_mcp_contribution_requires_delegation(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    contributor = _user(conn)
    _rejects(
        conn,
        "ck_claims_mcp_requires_delegation",
        lambda: _claim(conn, profile_id, contributor, contributed_via="mcp"),
    )
    delegation_id = _delegation(conn, contributor)
    _claim(
        conn,
        profile_id,
        contributor,
        contributed_via="mcp",
        contributor_delegation_id=delegation_id,
        status="provisional",
    )
    _claim(conn, profile_id, owner, contributed_via="ui")  # owner UI edit needs no agent
    _claim(conn, profile_id, owner, contributed_via="api")


def test_policy_version_must_exist_for_same_profile(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    other_profile_id, other_owner = _profile(conn)
    _policy(conn, other_profile_id, other_owner, version=1)
    # Version 1 exists only for the other profile.
    _rejects(
        conn,
        "fk_claims_profile_id_contribution_policies",
        lambda: _claim(conn, profile_id, owner, policy_version=1),
    )
    _policy(conn, profile_id, owner, version=1)
    _claim(conn, profile_id, owner, policy_version=1)
    _rejects(
        conn,
        "fk_claims_profile_id_contribution_policies",
        lambda: _claim(conn, profile_id, owner, policy_version=2),
    )


def test_policy_versions(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    _policy(conn, profile_id, owner, version=1)
    _policy(conn, profile_id, owner, version=2)
    _rejects(
        conn,
        "uq_contribution_policies_profile_id_version",
        lambda: _policy(conn, profile_id, owner, version=2),
    )
    _rejects(
        conn,
        "ck_contribution_policies_version_positive",
        lambda: _policy(conn, profile_id, owner, version=0),
    )


def test_one_profile_per_user(conn: Connection) -> None:
    _, owner = _profile(conn)
    _rejects(conn, "uq_profiles_user_id", lambda: _profile(conn, owner))


def test_self_supersede_rejected(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    claim_id = _claim(conn, profile_id, owner)
    _rejects(
        conn,
        "ck_claims_not_self_supersede",
        lambda: conn.execute(
            Claim.__table__.update().where(Claim.id == claim_id).values(supersedes_id=claim_id)
        ),
    )


def test_one_live_replacement_per_claim(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    original = _claim(conn, profile_id, owner, status="superseded")
    _claim(conn, profile_id, owner, supersedes_id=original, status="removed")
    _claim(conn, profile_id, owner, supersedes_id=original, status="superseded")
    _claim(conn, profile_id, owner, supersedes_id=original, status="pending_review")
    _claim(conn, profile_id, owner, supersedes_id=original, status="active")
    _rejects(
        conn,
        "uq_claims_supersedes_id_live",
        lambda: _claim(conn, profile_id, owner, supersedes_id=original, status="provisional"),
    )


def test_supersedes_same_profile_only(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    other_profile_id, other_owner = _profile(conn)
    others_fact = _claim(conn, other_profile_id, other_owner, audience="only_me")
    _rejects(
        conn,
        "fk_claims_supersedes_id_claims",
        lambda: _claim(conn, profile_id, owner, supersedes_id=others_fact, status="provisional"),
    )
    own_fact = _claim(conn, profile_id, owner)
    _claim(conn, profile_id, owner, supersedes_id=own_fact, status="provisional")
    _rejects(
        conn,
        "fk_claims_supersedes_id_claims",
        lambda: _claim(conn, profile_id, owner, supersedes_id=uuid.uuid4(), status="provisional"),
    )


def test_event_profile_must_match_claim(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    other_profile_id, other_owner = _profile(conn)
    others_claim = _claim(conn, other_profile_id, other_owner)
    _rejects(
        conn,
        "fk_claim_events_claim_id_claims",
        lambda: _event(conn, others_claim, profile_id, owner),
    )
    _event(conn, others_claim, other_profile_id, other_owner)


def test_event_policy_version_must_exist_for_same_profile(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    other_profile_id, other_owner = _profile(conn)
    _policy(conn, other_profile_id, other_owner, version=1)
    claim_id = _claim(conn, profile_id, owner)
    _rejects(
        conn,
        "fk_claim_events_policy_version_contribution_policies",
        lambda: _event(conn, claim_id, profile_id, owner, policy_version=1),
    )
    _policy(conn, profile_id, owner, version=1)
    _event(conn, claim_id, profile_id, owner, policy_version=1)
    _event(conn, claim_id, profile_id, owner, policy_version=None)
    _rejects(
        conn,
        "fk_claim_events_policy_version_contribution_policies",
        lambda: _event(conn, claim_id, profile_id, owner, policy_version=2),
    )


def test_deletes_with_dependents_restricted(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    _policy(conn, profile_id, owner)
    claim_id = _claim(conn, profile_id, owner, policy_version=1)
    _claim(conn, profile_id, owner, supersedes_id=claim_id, status="provisional")
    _event(conn, claim_id, profile_id, owner)
    _rejects(
        conn,
        "fk_claim_events_claim_id_claims|fk_claims_supersedes_id_claims",
        lambda: conn.execute(delete(Claim).where(Claim.id == claim_id)),
    )
    _rejects(
        conn,
        "fk_[a-z_]+_profiles",
        lambda: conn.execute(delete(Profile).where(Profile.id == profile_id)),
    )
    _rejects(
        conn,
        "fk_[a-z_]+_users",
        lambda: conn.execute(delete(User).where(User.id == owner)),
    )
    _rejects(
        conn,
        "fk_claims_profile_id_contribution_policies",
        lambda: conn.execute(
            delete(ContributionPolicy).where(ContributionPolicy.profile_id == profile_id)
        ),
    )


def test_delegation_delete_restricted(conn: Connection) -> None:
    profile_id, _ = _profile(conn)
    contributor = _user(conn)
    delegation_id = _delegation(conn, contributor)
    _claim(
        conn,
        profile_id,
        contributor,
        contributed_via="mcp",
        contributor_delegation_id=delegation_id,
    )
    _rejects(
        conn,
        "fk_claims_contributor_delegation_id_agent_delegations",
        lambda: conn.execute(delete(AgentDelegation).where(AgentDelegation.id == delegation_id)),
    )


def test_claim_event_checks(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    claim_id = _claim(conn, profile_id, owner)
    _rejects(
        conn,
        "ck_claim_events_action",
        lambda: _event(conn, claim_id, profile_id, owner, action="viewed"),
    )
    _rejects(
        conn,
        "ck_claim_events_acceptance",
        lambda: _event(conn, claim_id, profile_id, owner, acceptance="implicit"),
    )
    _event(conn, claim_id, profile_id, owner, action="auto_accepted", acceptance="automatic")
    _event(conn, claim_id, profile_id, owner, action="approved", acceptance="explicit")


def test_audience_filtered_read(conn: Connection) -> None:
    profile_id, owner = _profile(conn)
    other_profile_id, other_owner = _profile(conn)
    expected = {
        _claim(conn, profile_id, owner, audience="public", status="active"),
        _claim(conn, profile_id, owner, audience="connections", status="provisional"),
    }
    _claim(conn, profile_id, owner, audience="trusted_circle", status="active")
    _claim(conn, profile_id, owner, audience="only_me", status="active")
    _claim(conn, profile_id, owner, audience="public", status="pending_review")
    _claim(conn, profile_id, owner, audience="public", status="removed")
    _claim(conn, profile_id, owner, audience="connections", status="superseded")
    _claim(conn, other_profile_id, other_owner, audience="public", status="active")

    viewer_rank = Audience.CONNECTIONS.rank
    audiences = [a.value for a in Audience if a.rank <= viewer_rank]
    rows = conn.execute(
        select(Claim.id).where(
            Claim.profile_id == profile_id,
            Claim.status.in_([s.value for s in LIVE_CLAIM_STATUSES]),
            Claim.audience.in_(audiences),
        )
    ).scalars()
    assert set(rows) == expected


def test_downgrade_to_identity_and_reupgrade(
    migrated_engine: Engine, run_alembic: RunAlembic
) -> None:
    run_alembic(command.downgrade, "0001_identity")
    remaining = set(inspect(migrated_engine).get_table_names())
    assert not (PROFILE_TABLES & remaining)
    assert remaining >= IDENTITY_TABLES
    with migrated_engine.connect() as connection:
        assert MigrationContext.configure(connection).get_current_revision() == "0001_identity"

    run_alembic(command.downgrade, "base")
    assert not (
        (PROFILE_TABLES | IDENTITY_TABLES) & set(inspect(migrated_engine).get_table_names())
    )

    run_alembic(command.upgrade, "head")
    assert set(inspect(migrated_engine).get_table_names()) >= PROFILE_TABLES | IDENTITY_TABLES
    with migrated_engine.connect() as connection:
        assert (
            MigrationContext.configure(connection).get_current_revision()
            == "0008_invitation_provider_message"
        )
