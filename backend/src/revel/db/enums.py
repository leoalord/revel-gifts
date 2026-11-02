"""String enums that are the source of truth for constrained text columns.

Columns store the enum ``value`` as text guarded by a named CHECK constraint (not a PostgreSQL
ENUM type), so adding a value is a plain constraint migration.
"""

from enum import StrEnum


def check_in(column: str, enum: type[StrEnum]) -> str:
    """SQL expression ``column IN ('a', 'b', ...)`` built from an enum's values."""
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


class DelegationStatus(StrEnum):
    ACTIVE = "active"
    DISCONNECTED = "disconnected"


class Audience(StrEnum):
    """Who may see a piece of profile information, from widest to narrowest."""

    PUBLIC = "public"
    CONNECTIONS = "connections"
    TRUSTED_CIRCLE = "trusted_circle"
    ONLY_ME = "only_me"

    @property
    def rank(self) -> int:
        """Ordering for filtering: a viewer sees items with ``audience.rank <= viewer_rank``."""
        return _AUDIENCE_RANK[self]


_AUDIENCE_RANK: dict[Audience, int] = {
    Audience.PUBLIC: 0,
    Audience.CONNECTIONS: 1,
    Audience.TRUSTED_CIRCLE: 2,
    Audience.ONLY_ME: 3,
}


class ClaimStatus(StrEnum):
    """Lifecycle of a profile fact. Only ``active`` and ``provisional`` facts are live."""

    ACTIVE = "active"
    PROVISIONAL = "provisional"
    PENDING_REVIEW = "pending_review"
    SUPERSEDED = "superseded"
    REMOVED = "removed"


LIVE_CLAIM_STATUSES: tuple[ClaimStatus, ...] = (ClaimStatus.ACTIVE, ClaimStatus.PROVISIONAL)


class EvidenceType(StrEnum):
    EXPLICIT = "explicit"
    OBSERVED = "observed"
    INFERRED = "inferred"


class Confidence(StrEnum):
    """Coarse confidence label; numeric confidence is never stored."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class ObservedPrecision(StrEnum):
    """How precise ``claims.observed_at`` is; ``unknown`` means there is no evidence date."""

    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    UNKNOWN = "unknown"


class ContributedVia(StrEnum):
    """Channel a contribution arrived through (metadata, not proof of identity)."""

    MCP = "mcp"
    API = "api"
    UI = "ui"


class ClaimEventAction(StrEnum):
    CREATED = "created"
    AUTO_ACCEPTED = "auto_accepted"
    APPROVED = "approved"
    REJECTED = "rejected"
    UPDATED = "updated"
    AUDIENCE_CHANGED = "audience_changed"
    STATUS_CHANGED = "status_changed"
    SUPERSEDED = "superseded"
    REMOVED = "removed"
    RESTORED = "restored"


class AcceptanceMode(StrEnum):
    """Whether a contribution was accepted automatically by policy or explicitly approved."""

    AUTOMATIC = "automatic"
    EXPLICIT = "explicit"


class ConnectionRequestStatus(StrEnum):
    """A directional request for access to the target's profile.

    ``pending``, ``approved`` and ``blocked`` are live (at most one per directed pair); a
    ``declined``, ``cancelled`` or ``revoked`` request leaves room for a new one.
    """

    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    REVOKED = "revoked"


LIVE_CONNECTION_REQUEST_STATUSES: tuple[ConnectionRequestStatus, ...] = (
    ConnectionRequestStatus.PENDING,
    ConnectionRequestStatus.APPROVED,
    ConnectionRequestStatus.BLOCKED,
)


class ConnectionTier(StrEnum):
    """Tier the target grants an approved requester (OpenFGA is authoritative for membership)."""

    CONNECTIONS = "connections"
    TRUSTED_CIRCLE = "trusted_circle"


class QuestionStatus(StrEnum):
    """Requester's question: open -> answered -> closed; terminal declined/expired/cancelled."""

    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class AssignmentStatus(StrEnum):
    """Per-respondent lease: available -> claimed -> (waiting_for_user) -> completed.

    Other exits: released, declined, lease_expired.
    """

    AVAILABLE = "available"
    CLAIMED = "claimed"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETED = "completed"
    RELEASED = "released"
    DECLINED = "declined"
    LEASE_EXPIRED = "lease_expired"


class QuestionEventType(StrEnum):
    """Append-only evidence of what happened to a question (not its state)."""

    MADE_AVAILABLE = "made_available"
    RETRIEVED = "retrieved"
    CLAIMED = "claimed"
    RENEWED = "renewed"
    RELEASED = "released"
    LEASE_EXPIRED = "lease_expired"
    USER_CONTACT_REPORTED = "user_contact_reported"
    ANSWERED = "answered"
    DECLINED = "declined"
    ANSWER_RETRIEVED = "answer_retrieved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class AnswerStatus(StrEnum):
    ACCEPTED = "accepted"
    WITHDRAWN = "withdrawn"
