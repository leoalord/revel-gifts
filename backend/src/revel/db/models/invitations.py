"""Email-bound capabilities. Tokens are hashed for lookup and sealed for authorized resend."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from revel.db.base import Base, TimestampMixin


class Invitation(TimestampMixin, Base):
    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','joined','accepted','declined','cancelled','expired')",
            name="status",
        ),
        CheckConstraint(
            "delivery_status IN ('not_sent','sending','sent','unknown')", name="delivery_status"
        ),
        CheckConstraint("expires_at > created_at", name="positive_lifetime"),
        Index("ix_invitations_sender_created", "sender_user_id", "created_at"),
        Index(
            "uq_invitations_pending_email",
            "sender_user_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sender_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    email: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    sealed_token: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_status: Mapped[str] = mapped_column(Text, server_default="not_sent")
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    connection_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connection_requests.id", ondelete="RESTRICT")
    )
