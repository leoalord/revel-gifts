"""Identity schema: people (users), OAuth clients (agents), per-user delegations and the WorkOS
sessions that acted through each delegation."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from revel.db.base import Base, TimestampMixin
from revel.db.enums import DelegationStatus, check_in

_UUID_DEFAULT = text("gen_random_uuid()")


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    workos_user_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)


class Agent(TimestampMixin, Base):
    """One row per OAuth client (global, not per user)."""

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    oauth_client_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)


class AgentDelegation(TimestampMixin, Base):
    """A user's link to an agent. Disconnecting changes ``status``; rows are never deleted.

    At most one ``active`` row per (user, agent) (partial unique index); reconnecting after a
    disconnect inserts a new row, so disconnected rows remain as history and are never
    reactivated. Registration should use ``INSERT ... ON CONFLICT (user_id, agent_id) WHERE
    status = 'active' DO NOTHING`` and then read the active row. ``authorized_sid`` is the WorkOS
    session id whose token created the delegation.
    """

    __tablename__ = "agent_delegations"
    __table_args__ = (
        Index(
            "uq_agent_delegations_user_id_agent_id_active",
            "user_id",
            "agent_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_agent_delegations_user_id_agent_id", "user_id", "agent_id"),
        CheckConstraint(
            "authorized_sid IS NULL OR authorized_sid <> ''", name="authorized_sid_not_empty"
        ),
        # Target of composite (delegation_id, user_id) FKs: a row that stores both a user and a
        # delegation can only cite a delegation that belongs to that user.
        UniqueConstraint("id", "user_id"),
        CheckConstraint(check_in("status", DelegationStatus), name="status"),
        CheckConstraint(
            "(status = 'disconnected') = (disconnected_at IS NOT NULL)",
            name="disconnected_at_matches_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    # RESTRICT: identity history must not silently cascade away.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=DelegationStatus.ACTIVE.value
    )
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorized_sid: Mapped[str | None] = mapped_column(Text)


class AgentDelegationSession(Base):
    """A WorkOS ``sid`` that acted through a delegation (insert-only)."""

    __tablename__ = "agent_delegation_sessions"
    __table_args__ = (CheckConstraint("sid <> ''", name="sid_not_empty"),)

    delegation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_delegations.id", ondelete="RESTRICT"), primary_key=True
    )
    sid: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
