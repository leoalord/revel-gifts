"""Shared authorization service. Owned by F4 (authorization).

Every profile access decision in MCP and HTTP goes through this module; transport adapters
stay thin. It implements model v1 (``authorization/model.fga``):

- Object IDs: ``user:<users.id>``, ``profile:<profiles.id>``, ``agent:<agent_delegations.id>``.
  Never ``agents.id``: agent rows are global per OAuth client, so two users' Muse share one row.
- Subject of a check: ``agent:<delegation_id>`` when the principal acts through an active
  delegation, else ``user:<user_id>``.
- Agent connect writes ``agent:<d>#acts_for@user:<u>`` and ``user:<u>#delegate@agent:<d>``;
  ``profile#owner_agent`` is computed (``delegate from owner``) and never written. Profile
  creation writes only ``profile:<p>#owner@user:<u>``.

Tuple hooks (module-level ``on_*`` functions over a process-wide ``TupleHooks`` installed by
``configure_authz``) are called by F2 on sign-in, agent connect and disconnect. F2 awaits them
INSIDE its inserting database transaction, BEFORE commit: a raise rolls the insert back and the
next request retries both, so they are idempotent (retries, duplicate writes and deletes of
missing tuples are safe). A commit failure after a successful hook can leave orphan tuples whose
IDs match no row; they are unreachable (no principal carries those IDs) and are pruned by
reconciliation. Identity hooks never touch the database (no second pooled connection or worker
thread while F2 holds its transaction), and every OpenFGA call they make is bounded by
``HOOK_TIMEOUT_SECONDS``: on timeout they raise, so F2 rolls back promptly instead of holding its
pooled connection.

Hook failure rule (follow it for every new hook, e.g. v2 revoke, block, tier downgrade):

- GRANT hooks (``on_user_created``, ``on_agent_delegated``, ``visibility.on_profile_created``)
  log one warning and return ``None``
  only while OpenFGA is unconfigured (``configure_authz`` not called, or store/model IDs unset):
  a skipped grant only denies.
- REVOKE hooks (anything that deletes tuples: ``on_agent_disconnected``) raise
  ``FgaConfigurationError`` when unconfigured: a skipped delete would leave access in place.
- Once configured, any OpenFGA error on a grant or a revoke propagates; never swallow it.
- Checks (``can_contribute``, ``require_*``, ``resolve_viewer_tier``, later ``visible_claims``)
  raise ``FgaConfigurationError`` when unconfigured; they never allow by default.

Policy lives in Notion (Relationships, Discovery & Privacy); this module implements it.
"""

import logging
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

import anyio

from revel.auth.fga import FgaClient, FgaConfigurationError, RelationTuple

logger = logging.getLogger(__name__)

# Model v1 relation names (authorization/model.fga).
ACTS_FOR = "acts_for"
OWNER = "owner"
OWNER_AGENT = "owner_agent"
DELEGATE = "delegate"
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
    # Defence in depth (F2's Principal enforces the same): refuse inconsistent principals rather
    # than silently falling back to the user. MCP and API access always acts through an active
    # delegation; the human UI never does.
    if (principal.agent_id is None) != (principal.delegation_id is None):
        raise AuthorizationDenied()
    if principal.via in ("mcp", "api") and principal.delegation_id is None:
        raise AuthorizationDenied()
    if principal.via == "ui" and principal.delegation_id is not None:
        raise AuthorizationDenied()
    if principal.delegation_id is not None:
        return agent_object(principal.delegation_id)
    return user_object(principal.user_id)


def owner_tuple(user_id: UUID, profile_id: UUID) -> RelationTuple:
    return RelationTuple(user_object(user_id), OWNER, profile_object(profile_id))


def acts_for_tuple(user_id: UUID, delegation_id: UUID) -> RelationTuple:
    return RelationTuple(user_object(user_id), ACTS_FOR, agent_object(delegation_id))


def delegate_tuple(user_id: UUID, delegation_id: UUID) -> RelationTuple:
    """``user:<u>#delegate@agent:<d>``: grants owner_agent on every profile the user owns."""
    return RelationTuple(agent_object(delegation_id), DELEGATE, user_object(user_id))


def delegation_tuples(user_id: UUID, delegation_id: UUID) -> tuple[RelationTuple, RelationTuple]:
    """Both tuples an active delegation needs: ``acts_for`` and ``delegate``."""
    return acts_for_tuple(user_id, delegation_id), delegate_tuple(user_id, delegation_id)


# --- Tuple hooks ------------------------------------------------------------------------------

# Upper bound for the OpenFGA calls of one hook invocation (F2 holds a pooled DB connection).
HOOK_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class TupleSync:
    """Outcome of a hook: the tuples written and deleted."""

    written: tuple[RelationTuple, ...] = ()
    deleted: tuple[RelationTuple, ...] = ()


class TupleHooks:
    """Tuple writes for identity lifecycle events, bound to one client. No database access.

    Every method is idempotent (F2 retries after a rollback).
    """

    def __init__(self, fga: FgaClient):
        self._fga = fga

    async def on_user_created(self, *, user_id: UUID) -> TupleSync:
        """First sign-in: nothing to write (the owner tuple comes with the profile)."""
        del user_id
        return TupleSync()

    async def on_agent_delegated(
        self, *, user_id: UUID, agent_id: UUID, delegation_id: UUID
    ) -> TupleSync:
        """Agent connect: ``acts_for`` and ``delegate``; owner_agent then follows from ownership.

        ``agent_id`` (the global ``agents.id``) is accepted for the caller's convenience but never
        becomes an OpenFGA object.
        """
        del agent_id
        writes = delegation_tuples(user_id, delegation_id)
        await self._fga.write_tuples(writes)
        return TupleSync(written=writes)

    async def on_agent_disconnected(
        self, *, user_id: UUID, delegation_id: UUID, agent_id: UUID | None = None
    ) -> TupleSync:
        """Agent disconnect: delete every ``acts_for`` on ``agent:<d>`` and every ``delegate``
        tuple whose user is ``agent:<d>``, whichever user they name (read from OpenFGA)."""
        del agent_id
        agent = agent_object(delegation_id)
        deletes: list[RelationTuple] = list(delegation_tuples(user_id, delegation_id))
        deletes += await self._fga.read_tuples(relation=ACTS_FOR, object=agent)
        deletes += await self._fga.read_tuples(user=agent, relation=DELEGATE, object="user:")
        deletes = list(dict.fromkeys(deletes))
        await self._fga.delete_tuples(deletes)
        return TupleSync(deleted=tuple(deletes))


# --- Process-wide configuration and module-level hooks ----------------------------------------
#
# The app lifespan calls configure_authz() at startup and reset_authz() at shutdown. F2 looks the
# hooks up by name (getattr(importlib.import_module("revel.auth.authz"), name)).


@dataclass(slots=True)
class _AuthzState:
    fga: FgaClient
    hooks: TupleHooks


_state: _AuthzState | None = None
_warned_unconfigured = False


def configure_authz(fga: FgaClient) -> None:
    """Install the process-wide client. No network calls or validation."""
    global _state, _warned_unconfigured
    _state = _AuthzState(fga=fga, hooks=TupleHooks(fga))
    _warned_unconfigured = False


def reset_authz(fga: FgaClient | None = None) -> None:
    """Forget the process-wide client (app shutdown, tests).

    With ``fga``, reset only if that client is the one installed, so one app's shutdown cannot
    clear another app's configuration.
    """
    global _state, _warned_unconfigured
    if fga is not None and (_state is None or _state.fga is not fga):
        return
    _state = None
    _warned_unconfigured = False


def _grant_state() -> _AuthzState | None:
    """Configured state for a granting operation, or ``None`` (one warning) when unconfigured.

    Skipping a grant never grants access, so sign-in keeps working before bootstrap.
    """
    global _warned_unconfigured
    if _state is not None and _state.fga.configured:
        return _state
    if not _warned_unconfigured:
        logger.warning("OpenFGA is not configured; skipping authorization tuple writes")
        _warned_unconfigured = True
    return None


def _hooks_or_none() -> TupleHooks | None:
    state = _grant_state()
    return None if state is None else state.hooks


def configured_client_or_none() -> FgaClient | None:
    """The configured client for a GRANT hook, or ``None`` (one warning) while unconfigured."""
    state = _grant_state()
    return None if state is None else state.fga


def _revoke_hooks() -> TupleHooks:
    """Configured hooks for a revoking operation; raises when unconfigured (fail closed)."""
    if _state is None or not _state.fga.configured:
        raise FgaConfigurationError("OpenFGA is not configured; cannot revoke authorization")
    return _state.hooks


async def bounded[T](call: Awaitable[T]) -> T:
    """Await a hook's OpenFGA work, raising ``TimeoutError`` after ``HOOK_TIMEOUT_SECONDS``."""
    with anyio.fail_after(HOOK_TIMEOUT_SECONDS):
        return await call


async def on_user_created(*, user_id: UUID) -> TupleSync | None:
    hooks = _hooks_or_none()
    return None if hooks is None else await bounded(hooks.on_user_created(user_id=user_id))


async def on_agent_delegated(
    *, user_id: UUID, agent_id: UUID, delegation_id: UUID
) -> TupleSync | None:
    hooks = _hooks_or_none()
    if hooks is None:
        return None
    return await bounded(
        hooks.on_agent_delegated(user_id=user_id, agent_id=agent_id, delegation_id=delegation_id)
    )


async def on_agent_disconnected(
    *, user_id: UUID, delegation_id: UUID, agent_id: UUID | None = None
) -> TupleSync:
    # Revoke hook: a skipped delete leaves access in place, so unconfigured raises.
    hooks = _revoke_hooks()
    return await bounded(
        hooks.on_agent_disconnected(user_id=user_id, delegation_id=delegation_id, agent_id=agent_id)
    )


async def reconcile_owner_tuples(fga: FgaClient, pairs: Iterable[tuple[UUID, UUID]]) -> int:
    """Backfill ``owner`` tuples from explicit ``(user_id, profile_id)`` pairs. Idempotent."""
    writes = [owner_tuple(user_id, profile_id) for user_id, profile_id in pairs]
    await fga.write_tuples(writes)
    return len(writes)


async def reconcile_agent_tuples(fga: FgaClient, pairs: Iterable[tuple[UUID, UUID]]) -> int:
    """Backfill active delegations from ``(user_id, delegation_id)`` pairs. Idempotent.

    Pass only active delegations; revoked ones are removed by ``on_agent_disconnected``.
    """
    writes: list[RelationTuple] = []
    for user_id, delegation_id in pairs:
        writes += delegation_tuples(user_id, delegation_id)
    await fga.write_tuples(writes)
    return len(writes)


# --- Checks -----------------------------------------------------------------------------------


def _check_client(fga: FgaClient | None) -> FgaClient:
    """The client for a check: explicit, else the configured one. Never allow when unconfigured."""
    client = fga if fga is not None else (_state.fga if _state is not None else None)
    if client is None or not client.configured:
        raise FgaConfigurationError("OpenFGA is not configured; authorization checks cannot run")
    return client


async def can_contribute(fga: FgaClient | None, principal: PrincipalLike, profile_id: UUID) -> bool:
    """Whether the principal may contribute to the profile. ``fga=None`` uses the configured one."""
    return await _check_client(fga).check(
        user=subject_for(principal), relation=CONTRIBUTE, object=profile_object(profile_id)
    )


async def require_contribute(
    fga: FgaClient | None, principal: PrincipalLike, profile_id: UUID
) -> None:
    if not await can_contribute(fga, principal, profile_id):
        raise AuthorizationDenied()


async def resolve_viewer_tier(
    fga: FgaClient | None, principal: PrincipalLike, profile_id: UUID
) -> ViewerTier:
    """Highest tier the principal holds on the profile, in one OpenFGA call.

    Model v1 only has ``view_only_me``, so the result is ``only_me`` or ``none``. v2 adds the
    intermediate tiers (one batch check over all ``view_*`` relations).
    """
    allowed = await _check_client(fga).check(
        user=subject_for(principal), relation=VIEW_ONLY_ME, object=profile_object(profile_id)
    )
    return ViewerTier.only_me if allowed else ViewerTier.none


async def require_viewer(
    fga: FgaClient | None, principal: PrincipalLike, profile_id: UUID
) -> ViewerTier:
    """Resolve the viewer tier, raising ``AuthorizationDenied`` when it is ``none``."""
    tier = await resolve_viewer_tier(fga, principal, profile_id)
    if tier is ViewerTier.none:
        raise AuthorizationDenied()
    return tier


# --- Claim visibility ------------------------------------------------------------------------
#
# visible_claims / owner_claims live in revel.auth.visibility (they need the database models);
# do not read claims anywhere else. Like the checks above, they raise FgaConfigurationError when
# OpenFGA is unconfigured (via resolve_viewer_tier -> _check_client).
#
# Adapters (MCP/HTTP) must map AuthorizationDenied to a generic denial and OpenFGA SDK or
# FgaConfigurationError exceptions to a generic 5xx, and never return their text: SDK errors
# contain object IDs (UUIDs).
