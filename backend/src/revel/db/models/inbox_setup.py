"""Owner-approved host scheduling intent and separately observed inbox checks."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from revel.db.base import Base, TimestampMixin


class InboxCheckSetup(TimestampMixin, Base):
    __tablename__ = "inbox_check_setups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["delegation_id", "user_id"],
            ["agent_delegations.id", "agent_delegations.user_id"],
            ondelete="RESTRICT",
            name="fk_inbox_setup_designated_delegation",
        ),
        ForeignKeyConstraint(
            ["last_successful_check_delegation_id", "user_id"],
            ["agent_delegations.id", "agent_delegations.user_id"],
            ondelete="RESTRICT",
            name="fk_inbox_setup_observed_delegation",
        ),
        CheckConstraint("schedule_revision >= 0", name="schedule_revision"),
        CheckConstraint(
            "last_check_item_count IS NULL OR last_check_item_count >= 0", name="check_item_count"
        ),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    delegation_id: Mapped[uuid.UUID | None] = mapped_column()
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    timezone: Mapped[str | None] = mapped_column(Text)
    check_hours: Mapped[list[int]] = mapped_column(
        JSONB, server_default=text("'[6,10,14,18]'::jsonb")
    )
    host_task_id: Mapped[str | None] = mapped_column(Text)
    schedule_revision: Mapped[int] = mapped_column(Integer, server_default="0")
    owner_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    host_task_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_check_delegation_id: Mapped[uuid.UUID | None] = mapped_column()
    last_check_item_count: Mapped[int | None] = mapped_column(Integer)
