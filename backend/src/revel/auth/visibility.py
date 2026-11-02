"""Claim visibility: the only read paths for profile facts. Owned by F4 (authorization).

Every MCP/HTTP read of claims goes through one of these (a static test guards ``revel.api``
and ``revel.mcp``):

- ``visible_claims``: SHARED viewing, for the owner and for others. OpenFGA resolves the viewer
  tier (``authz.resolve_viewer_tier``: owner/owner's agents ``only_me``, then Trusted Circle,
  Connections, Public; a user's agents inherit the user's tier; blocks remove every tier), then
  SQL keeps only live facts (``LIVE_CLAIM_STATUSES``) whose audience rank is at most the tier's
  rank, so Only Me facts never reach a non-owner. ``pending_review``, ``superseded`` and
  ``removed`` are never returned. A viewer with tier ``none`` raises ``AuthorizationDenied``.
  Non-owner viewers additionally follow the profile's CURRENT contribution policy (the highest
  ``version``), read in the same query, so tightening takes effect on the next read:

  - provisional facts are returned (flagged, so callers show a caveat) only while
    ``share_provisional`` is true;
  - a viewer whose tier is only ``public`` sees Public facts only while ``public_opt_in`` is
    true (Privacy: without the opt-in they stay at Connections). This is defence in depth on
    top of the ``public_viewer`` tuples that ``on_public_opt_in_changed`` maintains.

  Owners get ``VisibleClaim`` (the ORM ``Claim``); everyone else gets ``SharedClaim``, a
  projection selected column by column that omits contributor, provenance and internal fields.
- ``owner_claims``: MANAGEMENT reads (all statuses) for the owner and the owner's agents, e.g.
  F5's contribution summary and corrections. Requires ``view_only_me``.

The database layer is synchronous SQLAlchemy; queries run on the caller's ``Session`` in a
worker thread (the caller awaits, so the session is never used concurrently). Unconfigured
OpenFGA raises ``FgaConfigurationError`` before any claim is read.

Also here: ``on_profile_created``, the grant hook F5 calls when it creates a profile.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import anyio.to_thread
from sqlalchemy import ColumnElement, Select, and_, false, func, literal, or_, select, true
from sqlalchemy.orm import Session

from revel.auth import authz
from revel.auth.authz import (
    AuthorizationDenied,
    PrincipalLike,
    TupleSync,
    ViewerTier,
    owner_tuple,
    resolve_viewer_tier,
)
from revel.auth.fga import FgaClient
from revel.db.enums import LIVE_CLAIM_STATUSES, Audience, ClaimStatus
from revel.db.models import Claim, ContributionPolicy, Profile

logger = logging.getLogger(__name__)

# Viewer tier -> highest audience rank visible. ``none`` sees nothing (below public's 0).
TIER_RANK: dict[ViewerTier, int] = {
    ViewerTier.none: -1,
    ViewerTier.public: Audience.PUBLIC.rank,
    ViewerTier.connections: Audience.CONNECTIONS.rank,
    ViewerTier.trusted_circle: Audience.TRUSTED_CIRCLE.rank,
    ViewerTier.only_me: Audience.ONLY_ME.rank,
}


def audiences_for_tier(tier: ViewerTier) -> tuple[Audience, ...]:
    """Audience values a tier may see (``audience.rank <= tier rank``), widest first."""
    rank = TIER_RANK[tier]
    return tuple(a for a in Audience if a.rank <= rank)


def is_owner_tier(tier: ViewerTier) -> bool:
    """``only_me`` is held exactly by the owner and the owner's agents (``view_only_me``)."""
    return tier is ViewerTier.only_me


async def _in_thread[T](fn: Callable[[], T]) -> T:
    return await anyio.to_thread.run_sync(fn)


# --- Shared viewing ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VisibleClaim:
    """An owner's (or owner's agent's) view of a fact: the full ORM ``Claim``."""

    claim: Claim
    # Provisional facts are shown with a caveat ("not yet confirmed by the owner").
    provisional: bool


@dataclass(frozen=True, slots=True)
class SharedClaim:
    """What a non-owner viewer may see of a fact. Never the ORM ``Claim``.

    Omitted on purpose: ``contributor_user_id``, ``contributor_delegation_id`` and
    ``contributed_via`` (who or which assistant contributed is the owner's provenance, and may
    name a third party); ``policy_version`` and ``supersedes_id`` (internal policy and edit
    history); ``status`` (only ``provisional`` matters to a viewer); ``profile_id`` (on
    ``VisibleClaims``); ``created_at`` / ``updated_at`` (when the owner or their assistant was
    active). ``id`` is an opaque reference for answer evidence (Privacy: answers cite claims).
    """

    id: UUID
    category: str
    statement: str
    details: dict[str, Any] | None
    audience: str
    evidence_type: str
    confidence: str
    observed_at: date | None
    observed_precision: str
    # Shown with a caveat ("not yet confirmed by the owner").
    provisional: bool


# Columns read for a SharedClaim (explicit, so no other column is ever loaded).
_SHARED_COLUMNS = (
    Claim.id,
    Claim.category,
    Claim.statement,
    Claim.details,
    Claim.audience,
    Claim.evidence_type,
    Claim.confidence,
    Claim.observed_at,
    Claim.observed_precision,
    Claim.status,
)


@dataclass(frozen=True, slots=True)
class VisibleClaims:
    profile_id: UUID
    tier: ViewerTier
    # ``VisibleClaim`` for the owner tier, ``SharedClaim`` for every other tier.
    claims: tuple[VisibleClaim, ...] | tuple[SharedClaim, ...]

    @property
    def has_provisional(self) -> bool:
        """Whether any returned fact needs the provisional caveat."""
        return any(c.provisional for c in self.claims)


_LIVE_VALUES = [s.value for s in LIVE_CLAIM_STATUSES]


def _current_policy_flag(profile_id: UUID, column: Any, default: bool) -> ColumnElement[bool]:
    """Scalar subquery: ``column`` of the profile's current policy (highest ``version``), or
    the column's server default when the profile has no policy yet."""
    latest = (
        select(column)
        .where(ContributionPolicy.profile_id == profile_id)
        .order_by(ContributionPolicy.version.desc())
        .limit(1)
        .scalar_subquery()
    )
    return func.coalesce(latest, literal(default))


def _current_share_provisional(profile_id: UUID) -> ColumnElement[bool]:
    """The latest policy's ``share_provisional`` (column default true if none)."""
    return _current_policy_flag(profile_id, ContributionPolicy.share_provisional, True)


def _current_public_opt_in(profile_id: UUID) -> ColumnElement[bool]:
    """The latest policy's ``public_opt_in`` (column default false if none)."""
    return _current_policy_flag(profile_id, ContributionPolicy.public_opt_in, False)


def _visible_claims_query(profile_id: UUID, tier: ViewerTier) -> Select[Any]:
    """SQL for the shared read path at ``tier``. Filtering happens entirely in the database.

    The owner tier selects the ORM ``Claim``; other tiers select only ``_SHARED_COLUMNS``.
    """
    audiences = [a.value for a in audiences_for_tier(tier)]
    owner = is_owner_tier(tier)
    provisional_allowed = true() if owner else _current_share_provisional(profile_id)
    # Only live statuses; provisional additionally gated by policy for non-owners.
    status_filter = or_(
        and_(Claim.status.in_(_LIVE_VALUES), Claim.status != ClaimStatus.PROVISIONAL.value),
        and_(Claim.status == ClaimStatus.PROVISIONAL.value, provisional_allowed),
    )
    # A Public-only viewer sees Public facts only while the current policy opts in.
    public_allowed = _current_public_opt_in(profile_id) if tier is ViewerTier.public else true()
    stmt = select(Claim) if owner else select(*_SHARED_COLUMNS)
    return stmt.where(
        Claim.profile_id == profile_id,
        Claim.status.in_(_LIVE_VALUES),
        status_filter,
        Claim.audience.in_(audiences) if audiences else false(),
        public_allowed,
    ).order_by(Claim.category, Claim.created_at, Claim.id)


async def _visible_claims_for_tier(
    session: Session, profile_id: UUID, tier: ViewerTier
) -> VisibleClaims:
    """Claims visible at an already-resolved tier. Private: adapters use ``visible_claims``."""
    if tier is ViewerTier.none:
        raise AuthorizationDenied()
    stmt = _visible_claims_query(profile_id, tier)
    if is_owner_tier(tier):
        owned = await _in_thread(lambda: list(session.execute(stmt).scalars()))
        return VisibleClaims(
            profile_id=profile_id,
            tier=tier,
            claims=tuple(
                VisibleClaim(claim=row, provisional=row.status == ClaimStatus.PROVISIONAL.value)
                for row in owned
            ),
        )
    rows = await _in_thread(lambda: list(session.execute(stmt).all()))
    return VisibleClaims(
        profile_id=profile_id,
        tier=tier,
        claims=tuple(
            SharedClaim(
                id=row.id,
                category=row.category,
                statement=row.statement,
                details=row.details,
                audience=row.audience,
                evidence_type=row.evidence_type,
                confidence=row.confidence,
                observed_at=row.observed_at,
                observed_precision=row.observed_precision,
                provisional=row.status == ClaimStatus.PROVISIONAL.value,
            )
            for row in rows
        ),
    )


async def visible_claims(
    session: Session,
    principal: PrincipalLike,
    profile_id: UUID,
    *,
    fga: FgaClient | None = None,
) -> VisibleClaims:
    """THE shared read path for a profile's facts. Raises ``AuthorizationDenied`` for tier none.

    The owner and the owner's agents get ``VisibleClaim`` items; any other principal (a user,
    or an agent through its active delegation) gets ``SharedClaim`` projections at the tier
    OpenFGA grants. ``fga=None`` uses the client installed by ``configure_authz``; unconfigured
    raises ``FgaConfigurationError``.
    """
    tier = await resolve_viewer_tier(fga, principal, profile_id)
    return await _visible_claims_for_tier(session, profile_id, tier)


# --- Owner management reads -------------------------------------------------------------------


async def owner_claims(
    session: Session,
    principal: PrincipalLike,
    profile_id: UUID,
    *,
    statuses: Iterable[ClaimStatus] | None = None,
    fga: FgaClient | None = None,
) -> list[Claim]:
    """All of the profile's facts (optionally only ``statuses``) for the owner or owner's agent.

    Raises ``AuthorizationDenied`` unless the principal holds ``view_only_me``.
    """
    tier = await resolve_viewer_tier(fga, principal, profile_id)
    if not is_owner_tier(tier):
        raise AuthorizationDenied()
    stmt = select(Claim).where(Claim.profile_id == profile_id)
    if statuses is not None:
        stmt = stmt.where(Claim.status.in_([ClaimStatus(s).value for s in statuses]))
    stmt = stmt.order_by(Claim.category, Claim.created_at, Claim.id)
    return await _in_thread(lambda: list(session.execute(stmt).scalars()))


# --- Profile creation hook (F5) ---------------------------------------------------------------


async def on_profile_created(
    session: Session, *, profile_id: UUID, user_id: UUID
) -> TupleSync | None:
    """Grant hook F5 calls in its profile-creating transaction, with that transaction's session.

    Verifies through the caller's session (no second connection) that the profile belongs to
    ``user_id``, then writes only ``profile:<p>#owner@user:<u>``: every current and future
    delegation of the user reaches it through ``user#delegate`` (owner_agent is computed).
    Grant semantics: warns and returns ``None`` only while OpenFGA is unconfigured; raises
    otherwise, including on a wrong owner or after ``authz.HOOK_TIMEOUT_SECONDS``.
    """
    fga = authz.configured_client_or_none()
    if fga is None:
        return None

    def owner_of_profile() -> UUID | None:
        return session.execute(
            select(Profile.user_id).where(Profile.id == profile_id)
        ).scalar_one_or_none()

    if await _in_thread(owner_of_profile) != user_id:
        raise ValueError("profile does not belong to user")
    writes = (owner_tuple(user_id, profile_id),)
    await authz.bounded(fga.write_tuples(writes))
    return TupleSync(written=writes)
