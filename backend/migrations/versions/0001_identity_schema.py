"""Identity schema: users, agents, agent_delegations.

Revision ID: 0001_identity
Revises:
Create Date: 2026-11-01

Hand-reviewed from autogenerate. Constraint names follow revel.db.base.NAMING_CONVENTION and are
written out literally (op.f marks them final) so this migration stays fixed if models change.
Requires PostgreSQL 13+ (built-in gen_random_uuid()).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_identity"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "users",
        _id(),
        sa.Column("workos_user_id", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("workos_user_id", name=op.f("uq_users_workos_user_id")),
    )
    op.create_table(
        "agents",
        _id(),
        sa.Column("oauth_client_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
        sa.UniqueConstraint("oauth_client_id", name=op.f("uq_agents_oauth_client_id")),
    )
    op.create_table(
        "agent_delegations",
        _id(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column(
            "connected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_delegations")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_agent_delegations_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name=op.f("fk_agent_delegations_agent_id_agents"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "user_id", "agent_id", name=op.f("uq_agent_delegations_user_id_agent_id")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disconnected')", name=op.f("ck_agent_delegations_status")
        ),
        sa.CheckConstraint(
            "(status = 'disconnected') = (disconnected_at IS NOT NULL)",
            name=op.f("ck_agent_delegations_disconnected_at_matches_status"),
        ),
    )
    op.create_index(op.f("ix_agent_delegations_agent_id"), "agent_delegations", ["agent_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_delegations_agent_id"), table_name="agent_delegations")
    op.drop_table("agent_delegations")
    op.drop_table("agents")
    op.drop_table("users")
