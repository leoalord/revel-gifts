"""Demo personas and the prepared (synthetic) facts the reset re-seeds on the recipient profile.

Everything here is a code constant: the handles of the three demo personas, the required
baseline connection and the approved synthetic seed facts. Seed keys are stable identifiers
that may be printed; statements are generic and obviously synthetic. Facts a connected
assistant contributes later in the recording are "live" and never listed here. In particular
there is deliberately NO binoculars-ownership seed: that fact must arrive live in the demo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from revel.db.enums import Audience, ConnectionTier, EvidenceType

# Recipient / profile owner and the two other demo personas (lowercase, as stored in handles).
RECIPIENT_HANDLE = "leo_walker"
SAM_HANDLE = "sam_demo"
JORDAN_HANDLE = "jordan_demo"
DEMO_HANDLES: tuple[str, ...] = (RECIPIENT_HANDLE, SAM_HANDLE, JORDAN_HANDLE)

# Required baseline: Jordan (requester) approved by Leo (target) at Trusted Circle, so Jordan
# reads Leo's profile at trusted_circle. It needs a live approval and is never created here.
BASELINE_REQUESTER = JORDAN_HANDLE
BASELINE_TARGET = RECIPIENT_HANDLE
BASELINE_TIER = ConnectionTier.TRUSTED_CIRCLE

# Approved connections kept as part of the "Leo <-> Jordan at trusted_circle" baseline:
# (requester, target, tier). Only the first is required; the reverse direction is kept if present.
KEPT_CONNECTIONS: frozenset[tuple[str, str, str]] = frozenset(
    {
        (BASELINE_REQUESTER, BASELINE_TARGET, BASELINE_TIER.value),
        (BASELINE_TARGET, BASELINE_REQUESTER, BASELINE_TIER.value),
    }
)

PREPARED_LABEL = "prepared"


@dataclass(frozen=True, slots=True)
class SeedFact:
    """One prepared fact, added through F5's ``add_facts`` as the recipient's owner UI."""

    key: str
    category: str
    statement: str
    audience: Audience
    evidence_type: EvidenceType
    details: dict[str, Any] | None = None

    def as_fact_input(self) -> dict[str, Any]:
        fact: dict[str, Any] = {
            "category": self.category,
            "statement": self.statement,
            "audience": self.audience.value,
            "evidence_type": self.evidence_type.value,
        }
        if self.details is not None:
            fact["details"] = dict(self.details)
        return fact


# Sizes are things the persona states (explicit); the rest are observed.
SEED_FACTS: tuple[SeedFact, ...] = (
    SeedFact(
        "birdwatching",
        "interest",
        "Enjoys birdwatching on weekends",
        Audience.CONNECTIONS,
        EvidenceType.OBSERVED,
    ),
    SeedFact(
        "compact_gear",
        "style",
        "Prefers compact, lightweight outdoor equipment",
        Audience.CONNECTIONS,
        EvidenceType.OBSERVED,
    ),
    SeedFact(
        "candles",
        "style",
        "Likes scented candles",
        Audience.CONNECTIONS,
        EvidenceType.OBSERVED,
    ),
    SeedFact(
        "ring_size",
        "size",
        "Ring size 7",
        Audience.CONNECTIONS,
        EvidenceType.EXPLICIT,
        {"item": "ring", "size": "7"},
    ),
    SeedFact(
        "documentaries",
        "interest",
        "Enjoys watching documentaries",
        Audience.CONNECTIONS,
        EvidenceType.OBSERVED,
    ),
    SeedFact(
        "shoe_size",
        "size",
        "Shoe size US 9",
        Audience.TRUSTED_CIRCLE,
        EvidenceType.EXPLICIT,
        {"item": "shoes", "size": "US 9"},
    ),
)

# The seed only Trusted Circle sees (Jordan yes, Sam never).
TRUSTED_ONLY_SEED = "shoe_size"
