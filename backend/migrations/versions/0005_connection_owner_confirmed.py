"""Connection owner confirmation: record when the owner confirmed an agent approval/upgrade.

Revision ID: 0005_connection_owner_confirmed
Revises: 0004_delegation_reconnect
Create Date: 2026-11-02

Hand-written to match revel.db.models.connections. Constraint names follow
revel.db.base.NAMING_CONVENTION and are written out literally (op.f marks them final), and the
CHECK expression is a literal string, so this migration stays fixed if models change.

connection_requests: adds owner_confirmed_at (nullable, no default, no backfill). When an agent
approves a connection or upgrades its tier, the owner must have confirmed it; this records when.
Existing rows stay NULL, meaning "pre-policy". A CHECK allows it only on approved or revoked rows.

Both directions set a short lock_timeout: connection_requests is briefly SHARE-locked by the FGA
reconcile job, and failing fast is preferred to queueing every connection reader behind this
migration's ACCESS EXCLUSIVE lock request.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_connection_owner_confirmed"
down_revision: str | Sequence[str] | None = "0004_delegation_reconnect"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_LOCK_TIMEOUT)
    op.add_column(
        "connection_requests",
        sa.Column("owner_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_connection_requests_owner_confirmed_at_matches_status"),
        "connection_requests",
        "owner_confirmed_at IS NULL OR status IN ('approved', 'revoked')",
    )


def downgrade() -> None:
    op.execute(_LOCK_TIMEOUT)
    op.drop_constraint(
        op.f("ck_connection_requests_owner_confirmed_at_matches_status"),
        "connection_requests",
        type_="check",
    )
    op.drop_column("connection_requests", "owner_confirmed_at")
