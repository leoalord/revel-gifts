"""Claim visibility: the only read paths for profile facts. Owned by F4 (authorization).

Every MCP/HTTP read of claims goes through one of these (a static test guards ``revel.api``
and ``revel.mcp``):

- ``visible_claims``: SHARED viewing. One OpenFGA call resolves the viewer tier, then SQL keeps
  only live facts (``LIVE_CLAIM_STATUSES``) whose audience rank is at most the tier's rank.
  ``pending_review``, ``superseded`` and ``removed`` are never returned. Provisional facts carry
  a caveat flag, and are hidden from non-owner viewers when the profile's current contribution
  policy (highest version) has ``share_provisional = false``. A viewer with tier ``none``
  raises ``AuthorizationDenied`` (v1 has no other non-owner tier, so "no access" and "nothing
  visible" are deliberately not distinguishable to the caller as an empty result).
- ``owner_claims``: MANAGEMENT reads (all statuses) for the owner and the owner's agents, e.g.
  F5's contribution summary and corrections. Requires ``view_only_me``.

The database layer is synchronous SQLAlchemy; queries run on the caller's ``Session`` in a
worker thread (the caller awaits, so the session is never used concurrently). Unconfigured
OpenFGA raises ``FgaConfigurationError`` before any claim is read.

v1 returns ORM ``Claim`` objects because the only viewers are the owner and the owner's agents.
v2 non-owner reads must return a projection (only the fields a shared viewer may see), not the
ORM ``Claim``.

Also here: ``on_profile_created``, the grant hook F5 calls when it creates a profile.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from uuid import UUID

import anyio.to_thread
from sqlalchemy import ColumnElement, Select, and_, false, func, or_, select, true
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
    claim: Claim
    # Provisional facts are shown with a caveat ("not yet confirmed by the owner").
    provisional: bool


@dataclass(frozen=True, slots=True)
class VisibleClaims:
    profile_id: UUID
    tier: ViewerTier
    claims: tuple[VisibleClaim, ...]

    @property
    def has_provisional(self) -> bool:
        """Whether any returned fact needs the provisional caveat."""
        return any(c.provisional for c in self.claims)


_LIVE_VALUES = [s.value for s in LIVE_CLAIM_STATUSES]


def _current_share_provisional(profile_id: UUID) -> ColumnElement[bool]:
    """Scalar subquery: the latest policy's ``share_provisional`` (column default true if none)."""
    latest = (
        select(ContributionPolicy.share_provisional)
        .where(ContributionPolicy.profile_id == profile_id)
        .order_by(ContributionPolicy.version.desc())
        .limit(1)
        .scalar_subquery()
    )
    return func.coalesce(latest, true())


def _visible_claims_query(profile_id: UUID, tier: ViewerTier) -> Select[tuple[Claim]]:
    """SQL for the shared read path at ``tier``. Filtering happens entirely in the database."""
    audiences = [a.value for a in audiences_for_tier(tier)]
    provisional_allowed = true() if is_owner_tier(tier) else _current_share_provisional(profile_id)
    # Only live statuses; provisional additionally gated by policy for non-owners.
    status_filter = or_(
        and_(Claim.status.in_(_LIVE_VALUES), Claim.status != ClaimStatus.PROVISIONAL.value),
        and_(Claim.status == ClaimStatus.PROVISIONAL.value, provisional_allowed),
    )
    return (
        select(Claim)
        .where(
            Claim.profile_id == profile_id,
            Claim.status.in_(_LIVE_VALUES),
            status_filter,
            Claim.audience.in_(audiences) if audiences else false(),
        )
        .order_by(Claim.category, Claim.created_at, Claim.id)
    )


async def _visible_claims_for_tier(
    session: Session, profile_id: UUID, tier: ViewerTier
) -> VisibleClaims:
    """Claims visible at an already-resolved tier. Private: adapters use ``visible_claims``."""
    if tier is ViewerTier.none:
        raise AuthorizationDenied()
    stmt = _visible_claims_query(profile_id, tier)
    rows = await _in_thread(lambda: list(session.execute(stmt).scalars()))
    return VisibleClaims(
        profile_id=profile_id,
        tier=tier,
        claims=tuple(
            VisibleClaim(claim=row, provisional=row.status == ClaimStatus.PROVISIONAL.value)
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

    ``fga=None`` uses the client installed by ``configure_authz``; unconfigured raises
    ``FgaConfigurationError``. v2 adds tiers inside ``resolve_viewer_tier``; this signature and
    the SQL filter (``TIER_RANK``) already cover them.
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
