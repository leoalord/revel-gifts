"""Shared authorization service. Owned by F4 (authorization).

Every profile access decision in MCP and HTTP goes through this module; transport adapters
stay thin. It implements model v1 (``authorization/model.fga``):

- Object IDs: ``user:<users.id>``, ``profile:<profiles.id>``, ``agent:<agent_delegations.id>``.
  Never ``agents.id``: agent rows are global per OAuth client, so two users' Muse share one row.
- Subject of a check: ``agent:<delegation_id>`` when the principal acts through an active
  delegation, else ``user:<user_id>``.

Tuple hooks (``TupleHooks``) are called by F2 on sign-in, agent connect and disconnect. They
are idempotent; retries, duplicate writes and deletes of missing tuples are safe. Profiles are
resolved through an injectable async resolver; when it returns ``None`` the profile tuples are
deferred and later backfilled with ``reconcile_owner_tuples`` / ``reconcile_agent_tuples``.

Policy lives in Notion (Relationships, Discovery & Privacy); this module implements it.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from revel.auth.fga import FgaClient, RelationTuple

logger = logging.getLogger(__name__)

# Model v1 relation names (authorization/model.fga).
ACTS_FOR = "acts_for"
OWNER = "owner"
OWNER_AGENT = "owner_agent"
CONTRIBUTE = "contribute"
VIEW_ONLY_ME = "view_only_me"


class PrincipalLike(Protocol):
    """Shape of F2's ``revel.auth.identity.Principal`` (frozen dataclass), typed structurally.

    ``agent_id`` and ``delegation_id`` are both set (an active delegation) or both ``None``.
    """

    @property
    def user_id(self) -> UUID: ...

    @property
    def agent_id(self) -> UUID | None: ...

    @property
    def delegation_id(self) -> UUID | None: ...

    @property
    def via(self) -> Literal["mcp", "api", "ui"]: ...


class ViewerTier(StrEnum):
    """Audience tier a viewer holds on a profile, lowest to highest.

    v1 resolves only ``none`` and ``only_me``; the intermediate tiers arrive with model v2.
    """

    none = "none"
    public = "public"
    connections = "connections"
    trusted_circle = "trusted_circle"
    only_me = "only_me"


class AuthorizationDenied(Exception):
    """The principal may not perform the operation. Carries no detail about why."""

    def __init__(self) -> None:
        super().__init__("Not authorized")


# --- Object IDs -------------------------------------------------------------------------------


def user_object(user_id: UUID) -> str:
    return f"user:{user_id}"


def profile_object(profile_id: UUID) -> str:
    return f"profile:{profile_id}"


def agent_object(delegation_id: UUID) -> str:
    """OpenFGA agent object for one delegation (``agent_delegations.id``, not ``agents.id``)."""
    return f"agent:{delegation_id}"


def subject_for(principal: PrincipalLike) -> str:
    """The OpenFGA user a check runs as: the delegation if active, else the human user."""
    if (principal.agent_id is None) != (principal.delegation_id is None):
        # Inconsistent principal: refuse rather than silently fall back to the user.
        raise AuthorizationDenied()
    if principal.delegation_id is not None:
        return agent_object(principal.delegation_id)
    return user_object(principal.user_id)


def owner_tuple(user_id: UUID, profile_id: UUID) -> RelationTuple:
    return RelationTuple(user_object(user_id), OWNER, profile_object(profile_id))


def acts_for_tuple(user_id: UUID, delegation_id: UUID) -> RelationTuple:
    return RelationTuple(user_object(user_id), ACTS_FOR, agent_object(delegation_id))


def owner_agent_tuple(delegation_id: UUID, profile_id: UUID) -> RelationTuple:
    return RelationTuple(agent_object(delegation_id), OWNER_AGENT, profile_object(profile_id))


# --- Tuple hooks ------------------------------------------------------------------------------

ProfileResolver = Callable[[UUID], Awaitable[UUID | None]]
"""Async ``user_id -> profiles.id`` for the user's own profile, or ``None`` if none exists yet."""


async def no_profile_resolver(user_id: UUID) -> UUID | None:
    """Default until F3's profiles table (#7) exists: every profile tuple is deferred."""
    return None


@dataclass(frozen=True, slots=True)
class TupleSync:
    """Outcome of a hook: tuples written/deleted, and whether profile tuples were deferred."""

    written: tuple[RelationTuple, ...] = ()
    deleted: tuple[RelationTuple, ...] = ()
    profile_deferred: bool = False


class TupleHooks:
    """Lifecycle hooks F2 calls after the corresponding database change commits."""

    def __init__(self, fga: FgaClient, resolve_profile: ProfileResolver = no_profile_resolver):
        self._fga = fga
        self._resolve_profile = resolve_profile

    async def on_user_created(self, *, user_id: UUID) -> TupleSync:
        """First sign-in: ``user:<u> owner profile:<p>`` once the profile exists."""
        profile_id = await self._resolve_profile(user_id)
        if profile_id is None:
            return TupleSync(profile_deferred=True)
        writes = (owner_tuple(user_id, profile_id),)
        await self._fga.write_tuples(writes)
        return TupleSync(written=writes)

    async def on_agent_delegated(
        self, *, user_id: UUID, agent_id: UUID, delegation_id: UUID
    ) -> TupleSync:
        """Agent connect: ``acts_for`` always; ``owner_agent`` and ``owner`` once a profile exists.

        ``agent_id`` (the global ``agents.id``) is accepted for the caller's convenience but never
        becomes an OpenFGA object.
        """
        del agent_id
        writes: list[RelationTuple] = [acts_for_tuple(user_id, delegation_id)]
        profile_id = await self._resolve_profile(user_id)
        if profile_id is not None:
            # Re-asserting the owner tuple is idempotent and repairs a deferred sign-in.
            writes += [
                owner_tuple(user_id, profile_id),
                owner_agent_tuple(delegation_id, profile_id),
            ]
        await self._fga.write_tuples(writes)
        return TupleSync(written=tuple(writes), profile_deferred=profile_id is None)

    async def on_agent_disconnected(
        self, *, user_id: UUID, delegation_id: UUID, agent_id: UUID | None = None
    ) -> TupleSync:
        """Agent disconnect: delete ``acts_for`` and every ``owner_agent`` tuple of the delegation.

        Existing ``owner_agent`` tuples are read from OpenFGA, so deletion does not depend on the
        profile resolver; the resolved profile is included too in case a read is stale.
        """
        del agent_id
        agent = agent_object(delegation_id)
        deletes: list[RelationTuple] = [acts_for_tuple(user_id, delegation_id)]
        deletes += await self._fga.read_tuples(
            user=agent, relation=OWNER_AGENT, object_type="profile"
        )
        profile_id = await self._resolve_profile(user_id)
        if profile_id is not None:
            deletes.append(owner_agent_tuple(delegation_id, profile_id))
        deletes = list(dict.fromkeys(deletes))
        await self._fga.delete_tuples(deletes)
        return TupleSync(deleted=tuple(deletes))


async def reconcile_owner_tuples(fga: FgaClient, pairs: Iterable[tuple[UUID, UUID]]) -> int:
    """Backfill ``owner`` tuples from explicit ``(user_id, profile_id)`` pairs. Idempotent."""
    writes = [owner_tuple(user_id, profile_id) for user_id, profile_id in pairs]
    await fga.write_tuples(writes)
    return len(writes)


async def reconcile_agent_tuples(fga: FgaClient, triples: Iterable[tuple[UUID, UUID, UUID]]) -> int:
    """Backfill active delegations from ``(user_id, delegation_id, profile_id)``. Idempotent.

    Pass only active delegations; revoked ones are removed by ``on_agent_disconnected``.
    """
    writes: list[RelationTuple] = []
    for user_id, delegation_id, profile_id in triples:
        writes += [
            acts_for_tuple(user_id, delegation_id),
            owner_agent_tuple(delegation_id, profile_id),
        ]
    await fga.write_tuples(writes)
    return len(writes)


# --- Checks -----------------------------------------------------------------------------------


async def can_contribute(fga: FgaClient, principal: PrincipalLike, profile_id: UUID) -> bool:
    return await fga.check(
        user=subject_for(principal), relation=CONTRIBUTE, object=profile_object(profile_id)
    )


async def require_contribute(fga: FgaClient, principal: PrincipalLike, profile_id: UUID) -> None:
    if not await can_contribute(fga, principal, profile_id):
        raise AuthorizationDenied()


async def resolve_viewer_tier(
    fga: FgaClient, principal: PrincipalLike, profile_id: UUID
) -> ViewerTier:
    """Highest tier the principal holds on the profile, in one OpenFGA call.

    Model v1 only has ``view_only_me``, so the result is ``only_me`` or ``none``. v2 adds the
    intermediate tiers (one batch check over all ``view_*`` relations).
    """
    allowed = await fga.check(
        user=subject_for(principal), relation=VIEW_ONLY_ME, object=profile_object(profile_id)
    )
    return ViewerTier.only_me if allowed else ViewerTier.none


async def require_viewer(fga: FgaClient, principal: PrincipalLike, profile_id: UUID) -> ViewerTier:
    """Resolve the viewer tier, raising ``AuthorizationDenied`` when it is ``none``."""
    tier = await resolve_viewer_tier(fga, principal, profile_id)
    if tier is ViewerTier.none:
        raise AuthorizationDenied()
    return tier


# --- Claim visibility (pending #7) ------------------------------------------------------------
#
# visible_claims(fga, session, principal, profile_id) belongs here: it resolves the viewer tier
# above and filters claims in PostgreSQL by status (pending_review, superseded and removed are
# hidden) and audience <= tier. It is intentionally not implemented until F3's claims schema
# (#7) is merged; do not read claims anywhere else.
