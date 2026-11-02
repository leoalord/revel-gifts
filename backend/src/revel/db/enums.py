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
