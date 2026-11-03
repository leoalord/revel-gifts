"""Delegation reconnect: one ACTIVE delegation per (user, agent); remembered WorkOS sids.

Revision ID: 0004_delegation_reconnect
Revises: 0003_connections_questions
Create Date: 2026-11-02

Hand-written to match revel.db.models.identity. Constraint names follow
revel.db.base.NAMING_CONVENTION and are written out literally (op.f marks them final), and CHECK
expressions are literal strings, so this migration stays fixed if models change.

agent_delegations: replaces UNIQUE (user_id, agent_id) with a partial unique index on
(user_id, agent_id) WHERE status = 'active' (reconnecting after a disconnect inserts a new row;
disconnected rows stay as history), adds a plain (user_id, agent_id) index for lookups across all
rows, and adds authorized_sid (the WorkOS sid that created the delegation). New table
agent_delegation_sessions records every sid that acted through a delegation (insert-only).

Downgrade refuses (before changing anything) when any (user_id, agent_id) has more than one row,
since the old UNIQUE constraint cannot be restored without deleting reconnect history.

Both directions set a short lock_timeout: agent_delegations is briefly SHARE-locked by the FGA
reconcile job, and failing fast is preferred to queueing every delegation reader behind this
migration's ACCESS EXCLUSIVE lock request.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_delegation_reconnect"
down_revision: str | Sequence[str] | None = "0003_connections_questions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"

# A DO block (not a Python-side query) so the guard also runs from `alembic downgrade --sql`.
_DOWNGRADE_GUARD = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM agent_delegations GROUP BY user_id, agent_id HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'Cannot downgrade 0004_delegation_reconnect: some (user_id, agent_id) '
            'pairs have more than one agent_delegations row (reconnect history). '
            'UNIQUE (user_id, agent_id) cannot be restored without deleting history; '
            'resolve those rows manually first.';
    END IF;
END
$$
"""


def upgrade() -> None:
    op.execute(_LOCK_TIMEOUT)
    op.add_column("agent_delegations", sa.Column("authorized_sid", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_agent_delegations_authorized_sid_not_empty"),
        "agent_delegations",
        "authorized_sid IS NULL OR authorized_sid <> ''",
    )
    op.drop_constraint(
        op.f("uq_agent_delegations_user_id_agent_id"), "agent_delegations", type_="unique"
    )
    op.create_index(
        op.f("uq_agent_delegations_user_id_agent_id_active"),
        "agent_delegations",
        ["user_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        op.f("ix_agent_delegations_user_id_agent_id"), "agent_delegations", ["user_id", "agent_id"]
    )
    op.create_table(
        "agent_delegation_sessions",
        sa.Column("delegation_id", sa.Uuid(), nullable=False),
        sa.Column("sid", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("delegation_id", "sid", name=op.f("pk_agent_delegation_sessions")),
        sa.ForeignKeyConstraint(
            ["delegation_id"],
            ["agent_delegations.id"],
            name=op.f("fk_agent_delegation_sessions_delegation_id_agent_delegations"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("sid <> ''", name=op.f("ck_agent_delegation_sessions_sid_not_empty")),
    )


def downgrade() -> None:
    op.execute(_LOCK_TIMEOUT)
    op.execute(_DOWNGRADE_GUARD)
    op.drop_table("agent_delegation_sessions")
    op.drop_index(op.f("ix_agent_delegations_user_id_agent_id"), table_name="agent_delegations")
    op.drop_index(
        op.f("uq_agent_delegations_user_id_agent_id_active"), table_name="agent_delegations"
    )
    op.create_unique_constraint(
        op.f("uq_agent_delegations_user_id_agent_id"), "agent_delegations", ["user_id", "agent_id"]
    )
    op.drop_constraint(
        op.f("ck_agent_delegations_authorized_sid_not_empty"), "agent_delegations", type_="check"
    )
    op.drop_column("agent_delegations", "authorized_sid")
