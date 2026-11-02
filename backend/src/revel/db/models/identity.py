"""Identity schema: people (users), OAuth clients (agents) and per-user delegations."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint, func, text
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

    First-call registration should use ``INSERT ... ON CONFLICT (user_id, agent_id) DO NOTHING``
    and then read the row, so it can never reactivate a disconnected delegation.
    """

    __tablename__ = "agent_delegations"
    __table_args__ = (
        UniqueConstraint("user_id", "agent_id"),
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
