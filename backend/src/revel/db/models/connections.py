"""Connections schema: public handles and directional connection requests.

OpenFGA is authoritative for relationship membership (who is a Connection or in a Trusted
Circle); PostgreSQL holds the requests and their metadata. Display name stays on
``users.display_name``. Tier changes and revocation take effect on the next request (F4/F6).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from revel.db.base import Base, TimestampMixin
from revel.db.enums import ConnectionRequestStatus, ConnectionTier, check_in

_UUID_DEFAULT = text("gen_random_uuid()")

HANDLE_PATTERN = "^[a-z0-9_]{3,30}$"
AVATAR_URL_MAX_LENGTH = 2048
LIVE_REQUEST_WHERE = "status IN ('pending', 'approved', 'blocked')"


class Handle(TimestampMixin, Base):
    """A user's unique handle for exact-handle lookup (stored lowercase; the CHECK enforces it).

    Discoverable by default; the owner can turn discovery off.
    """

    __tablename__ = "handles"
    __table_args__ = (
        CheckConstraint(f"handle ~ '{HANDLE_PATTERN}'", name="handle_format"),
        CheckConstraint(
            f"char_length(avatar_url) <= {AVATAR_URL_MAX_LENGTH}", name="avatar_url_length"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    handle: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    discoverable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    avatar_url: Mapped[str | None] = mapped_column(Text)


class ConnectionRequest(TimestampMixin, Base):
    """The requester asks for access to the target's profile; approval is directional.

    The target approves the requester as ``connections`` or ``trusted_circle`` (``granted_tier``),
    or declines/blocks. At most one live (pending/approved/blocked) row exists per directed pair:
    a duplicate request returns the existing row and a block persists. ``responded_at`` is set
    whenever the request leaves ``pending``. ``(requester_user_id, created_at)`` supports rate
    limits.
    """

    __tablename__ = "connection_requests"
    __table_args__ = (
        CheckConstraint("requester_user_id <> target_user_id", name="not_self_request"),
        CheckConstraint(check_in("status", ConnectionRequestStatus), name="status"),
        CheckConstraint(check_in("granted_tier", ConnectionTier), name="granted_tier"),
        CheckConstraint(
            "(status = 'approved') = (granted_tier IS NOT NULL)",
            name="granted_tier_matches_status",
        ),
        CheckConstraint(
            "(status = 'pending') = (responded_at IS NULL)", name="responded_at_matches_status"
        ),
        # The requesting delegation must belong to the requester (MATCH SIMPLE: NULL allowed).
        ForeignKeyConstraint(
            ["requester_delegation_id", "requester_user_id"],
            ["agent_delegations.id", "agent_delegations.user_id"],
            name="fk_connection_requests_requester_delegation_agent_delegations",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_connection_requests_requester_user_id_target_user_id_live",
            "requester_user_id",
            "target_user_id",
            unique=True,
            postgresql_where=text(LIVE_REQUEST_WHERE),
        ),
        Index("ix_connection_requests_target_user_id_status", "target_user_id", "status"),
        Index(
            "ix_connection_requests_requester_user_id_created_at",
            "requester_user_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    requester_delegation_id: Mapped[uuid.UUID | None] = mapped_column()
    target_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=ConnectionRequestStatus.PENDING.value
    )
    granted_tier: Mapped[str | None] = mapped_column(Text)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
