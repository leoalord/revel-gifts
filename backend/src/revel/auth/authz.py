"""Shared authorization service. Owned by F4 (authorization).

Every profile access decision in MCP and HTTP goes through this module; transport adapters
stay thin. It implements model v2 (``authorization/model.fga``; additive over v1):

- Object IDs: ``user:<users.id>``, ``profile:<profiles.id>``, ``agent:<agent_delegations.id>``,
  ``question:<questions.id>`` and the single ``platform:revel``. Never ``agents.id``: agent rows
  are global per OAuth client, so two users' Muse share one row.
- Subject of a check: ``agent:<delegation_id>`` when the principal acts through an active
  delegation, else ``user:<user_id>``.
- Agent connect writes ``agent:<d>#acts_for@user:<u>``, ``user:<u>#delegate@agent:<d>`` and
  ``platform:revel#connected_agent@agent:<d>`` (the last lets connected agents reach a Public
  profile; OpenFGA cannot follow ``user:*`` through ``delegate``). ``profile#owner_agent`` and
  every ``view_*`` relation are computed and never written. Profile creation writes only
  ``profile:<p>#owner@user:<u>``.
- Tiers (v2): ``view_only_me`` (owner and owner's agents) > ``view_trusted`` >
  ``view_connections`` > ``view_public``; a user's agents inherit the user's tier. A
  ``profile:<B>#blocked@user:<A>`` tuple removes A and A's agents from every tier on B's profile
  (directional; never the owner). Grants come from F6 (``on_connection_granted``) and F5
  (``on_public_opt_in_changed``); F7 writes ``question`` tuples (``on_question_created``).

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

- GRANT hooks (``on_user_created``, ``on_agent_delegated``, ``visibility.on_profile_created``,
  ``on_connection_granted`` with ``trusted_circle``, ``on_unblocked``,
  ``on_public_opt_in_changed(enabled=True)``, ``on_question_created``) log one warning and
  return ``None`` only while OpenFGA is unconfigured (``configure_authz`` not called, or
  store/model IDs unset): a skipped grant only denies.
- REVOKE hooks (anything whose skipping would leave access in place: ``on_agent_disconnected``,
  ``on_connection_revoked``, ``on_blocked``, ``on_public_opt_in_changed(enabled=False)`` and
  ``on_connection_granted`` with ``connections``, which may be a downgrade from Trusted Circle)
  raise ``FgaConfigurationError`` when unconfigured.
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
from revel.db.enums import ConnectionTier

logger = logging.getLogger(__name__)

# Model v1 relation names (authorization/model.fga).
ACTS_FOR = "acts_for"
OWNER = "owner"
OWNER_AGENT = "owner_agent"
DELEGATE = "delegate"
CONTRIBUTE = "contribute"
VIEW_ONLY_ME = "view_only_me"

# Model v2 relation names.
CONNECTED_AGENT = "connected_agent"
TRUSTED = "trusted"
CONNECTION = "connection"
PUBLIC_VIEWER = "public_viewer"
BLOCKED = "blocked"
VIEW_TRUSTED = "view_trusted"
VIEW_CONNECTIONS = "view_connections"
VIEW_PUBLIC = "view_public"
ABOUT = "about"
REQUESTER = "requester"
RESPOND = "respond"
READ_ANSWER = "read_answer"

# The single platform object; every active delegation is its connected_agent.
PLATFORM_OBJECT = "platform:revel"
# Public opt-in subjects: any user, and every connected agent (a userset).
PUBLIC_USERS = "user:*"
PUBLIC_AGENTS = f"{PLATFORM_OBJECT}#{CONNECTED_AGENT}"


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
    """Audience tier a viewer holds on a profile, lowest to highest (model v2 resolves all)."""

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


def question_object(question_id: UUID) -> str:
    return f"question:{question_id}"


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


def connected_agent_tuple(delegation_id: UUID) -> RelationTuple:
    """``platform:revel#connected_agent@agent:<d>``: lets the agent reach Public profiles."""
    return RelationTuple(agent_object(delegation_id), CONNECTED_AGENT, PLATFORM_OBJECT)


def delegation_tuples(
    user_id: UUID, delegation_id: UUID
) -> tuple[RelationTuple, RelationTuple, RelationTuple]:
    """Every tuple an active delegation needs: ``acts_for``, ``delegate``, ``connected_agent``."""
    return (
        acts_for_tuple(user_id, delegation_id),
        delegate_tuple(user_id, delegation_id),
        connected_agent_tuple(delegation_id),
    )


# Granted tier (``connection_requests.granted_tier``) -> the profile relation holding it.
TIER_RELATION: dict[ConnectionTier, str] = {
    ConnectionTier.CONNECTIONS: CONNECTION,
    ConnectionTier.TRUSTED_CIRCLE: TRUSTED,
}


def tier_tuple(profile_id: UUID, viewer_user_id: UUID, tier: ConnectionTier) -> RelationTuple:
    """``profile:<p>#trusted|connection@user:<viewer>`` for a granted tier."""
    return RelationTuple(
        user_object(viewer_user_id), TIER_RELATION[tier], profile_object(profile_id)
    )


def tier_tuples(profile_id: UUID, viewer_user_id: UUID) -> tuple[RelationTuple, ...]:
    """Both tier tuples a viewer could hold on a profile (revocation deletes both)."""
    return tuple(tier_tuple(profile_id, viewer_user_id, t) for t in ConnectionTier)


def blocked_tuple(profile_id: UUID, blocked_user_id: UUID) -> RelationTuple:
    """``profile:<p>#blocked@user:<u>``: the profile's owner blocked ``u`` (directional)."""
    return RelationTuple(user_object(blocked_user_id), BLOCKED, profile_object(profile_id))


def public_tuples(profile_id: UUID) -> tuple[RelationTuple, RelationTuple]:
    """The Public opt-in: ``public_viewer`` for ``user:*`` and every connected agent."""
    profile = profile_object(profile_id)
    return (
        RelationTuple(PUBLIC_USERS, PUBLIC_VIEWER, profile),
        RelationTuple(PUBLIC_AGENTS, PUBLIC_VIEWER, profile),
    )


def question_tuples(
    question_id: UUID, profile_id: UUID, requester_user_id: UUID
) -> tuple[RelationTuple, RelationTuple]:
    """``question:<q>#about@profile:<p>`` and ``question:<q>#requester@user:<r>``."""
    question = question_object(question_id)
    return (
        RelationTuple(profile_object(profile_id), ABOUT, question),
        RelationTuple(user_object(requester_user_id), REQUESTER, question),
    )


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
        """Agent disconnect: delete every ``acts_for`` on ``agent:<d>``, every ``delegate``
        tuple whose user is ``agent:<d>`` (whichever user they name; read from OpenFGA) and its
        ``platform:revel#connected_agent`` tuple, so it loses every relation, Public included."""
        del agent_id
        agent = agent_object(delegation_id)
        deletes: list[RelationTuple] = list(delegation_tuples(user_id, delegation_id))
        deletes += await self._fga.read_tuples(relation=ACTS_FOR, object=agent)
        deletes += await self._fga.read_tuples(user=agent, relation=DELEGATE, object="user:")
        deletes = list(dict.fromkeys(deletes))
        await self._fga.delete_tuples(deletes)
        return TupleSync(deleted=tuple(deletes))

    # --- v2: connections, blocks, Public opt-in, questions (F5, F6, F7) ---

    async def on_connection_granted(
        self, *, profile_id: UUID, viewer_user_id: UUID, tier: ConnectionTier | str
    ) -> TupleSync:
        """Approval or tier change: the viewer holds exactly ``tier`` on the profile.

        Deletes the other tier's tuple FIRST, then writes ``tier``'s: a failure in between
        leaves no tier (fails closed) and raises, so the caller rolls back and retries.
        """
        tier = ConnectionTier(tier)
        other = next(t for t in ConnectionTier if t is not tier)
        deletes = (tier_tuple(profile_id, viewer_user_id, other),)
        writes = (tier_tuple(profile_id, viewer_user_id, tier),)
        await self._fga.delete_tuples(deletes)
        await self._fga.write_tuples(writes)
        return TupleSync(written=writes, deleted=deletes)

    async def on_connection_revoked(self, *, profile_id: UUID, viewer_user_id: UUID) -> TupleSync:
        """Revocation: delete both tier tuples."""
        deletes = tier_tuples(profile_id, viewer_user_id)
        await self._fga.delete_tuples(deletes)
        return TupleSync(deleted=deletes)

    async def on_blocked(self, *, profile_id: UUID, blocked_user_id: UUID) -> TupleSync:
        """Block: write ``blocked`` first (cuts every tier at once), then delete both tier
        tuples so a later unblock never resurrects the old tier without a new approval."""
        writes = (blocked_tuple(profile_id, blocked_user_id),)
        deletes = tier_tuples(profile_id, blocked_user_id)
        await self._fga.write_tuples(writes)
        await self._fga.delete_tuples(deletes)
        return TupleSync(written=writes, deleted=deletes)

    async def on_unblocked(self, *, profile_id: UUID, blocked_user_id: UUID) -> TupleSync:
        """Unblock: delete ``blocked``. Grants nothing by itself (no tier tuple remains)."""
        deletes = (blocked_tuple(profile_id, blocked_user_id),)
        await self._fga.delete_tuples(deletes)
        return TupleSync(deleted=deletes)

    async def on_public_opt_in_changed(self, *, profile_id: UUID, enabled: bool) -> TupleSync:
        """Public opt-in: write (enabled) or delete (withdrawn) both ``public_viewer`` tuples."""
        tuples = public_tuples(profile_id)
        if enabled:
            await self._fga.write_tuples(tuples)
            return TupleSync(written=tuples)
        await self._fga.delete_tuples(tuples)
        return TupleSync(deleted=tuples)

    async def on_question_created(
        self, *, question_id: UUID, profile_id: UUID, requester_user_id: UUID
    ) -> TupleSync:
        """Question creation: ``about`` and ``requester`` (respond/read_answer are computed)."""
        writes = question_tuples(question_id, profile_id, requester_user_id)
        await self._fga.write_tuples(writes)
        return TupleSync(written=writes)


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


# v2 hooks for F5, F6 and F7. Same contract as the identity hooks: keyword-only, idempotent,
# OpenFGA-only (no database access), each invocation bounded by HOOK_TIMEOUT_SECONDS. Call them
# inside the transaction that changes the matching row, before commit, so a raise rolls the row
# back. Grant-class calls warn and return None while unconfigured; revoke-class calls raise.


async def on_connection_granted(
    *, profile_id: UUID, viewer_user_id: UUID, tier: ConnectionTier | str
) -> TupleSync | None:
    """F6: the profile's owner approved ``viewer_user_id`` at ``tier`` (``connections`` or
    ``trusted_circle``) or changed their tier; the other tier's tuple is deleted, so one call
    sets the tier. ``connections`` may be a downgrade, so it is revoke-class (raises while
    unconfigured); ``trusted_circle`` only widens and is grant-class."""
    tier = ConnectionTier(tier)
    if tier is ConnectionTier.CONNECTIONS:
        hooks: TupleHooks | None = _revoke_hooks()
    else:
        hooks = _hooks_or_none()
    if hooks is None:
        return None
    return await bounded(
        hooks.on_connection_granted(profile_id=profile_id, viewer_user_id=viewer_user_id, tier=tier)
    )


async def on_connection_revoked(*, profile_id: UUID, viewer_user_id: UUID) -> TupleSync:
    """F6: the owner revoked the viewer's access to the profile (both tiers). Revoke-class."""
    hooks = _revoke_hooks()
    return await bounded(
        hooks.on_connection_revoked(profile_id=profile_id, viewer_user_id=viewer_user_id)
    )


async def on_blocked(*, profile_id: UUID, blocked_user_id: UUID) -> TupleSync:
    """F6: the profile's owner blocked ``blocked_user_id``: that user and their agents lose
    every tier on this profile only, and the tier tuples are deleted. Revoke-class."""
    hooks = _revoke_hooks()
    return await bounded(hooks.on_blocked(profile_id=profile_id, blocked_user_id=blocked_user_id))


async def on_unblocked(*, profile_id: UUID, blocked_user_id: UUID) -> TupleSync | None:
    """F6: the owner lifted a block. Grant-class (a skipped delete only keeps denying)."""
    hooks = _hooks_or_none()
    if hooks is None:
        return None
    return await bounded(hooks.on_unblocked(profile_id=profile_id, blocked_user_id=blocked_user_id))


async def on_public_opt_in_changed(*, profile_id: UUID, enabled: bool) -> TupleSync | None:
    """F5: a new contribution-policy version changed ``public_opt_in``. Enabling is
    grant-class; withdrawing is revoke-class (raises while unconfigured)."""
    hooks = _hooks_or_none() if enabled else _revoke_hooks()
    if hooks is None:
        return None
    return await bounded(hooks.on_public_opt_in_changed(profile_id=profile_id, enabled=enabled))


async def on_question_created(
    *, question_id: UUID, profile_id: UUID, requester_user_id: UUID
) -> TupleSync | None:
    """F7: a question about ``profile_id`` by ``requester_user_id`` was created. Grant-class."""
    hooks = _hooks_or_none()
    if hooks is None:
        return None
    return await bounded(
        hooks.on_question_created(
            question_id=question_id, profile_id=profile_id, requester_user_id=requester_user_id
        )
    )


async def reconcile_owner_tuples(fga: FgaClient, pairs: Iterable[tuple[UUID, UUID]]) -> int:
    """Backfill ``owner`` tuples from explicit ``(user_id, profile_id)`` pairs. Idempotent."""
    writes = [owner_tuple(user_id, profile_id) for user_id, profile_id in pairs]
    await fga.write_tuples(writes)
    return len(writes)


async def reconcile_agent_tuples(fga: FgaClient, pairs: Iterable[tuple[UUID, UUID]]) -> int:
    """Backfill active delegations from ``(user_id, delegation_id)`` pairs. Idempotent.

    Pass only active delegations; revoked ones are removed by ``on_agent_disconnected``.
    Writes ``acts_for``, ``delegate`` and ``connected_agent`` for each.
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


# Highest first; resolve_viewer_tier stops at the first relation the subject holds.
TIER_CHECKS: tuple[tuple[str, ViewerTier], ...] = (
    (VIEW_ONLY_ME, ViewerTier.only_me),
    (VIEW_TRUSTED, ViewerTier.trusted_circle),
    (VIEW_CONNECTIONS, ViewerTier.connections),
    (VIEW_PUBLIC, ViewerTier.public),
)


async def resolve_viewer_tier(
    fga: FgaClient | None, principal: PrincipalLike, profile_id: UUID
) -> ViewerTier:
    """Highest tier the principal holds on the profile.

    Checks ``view_only_me``, ``view_trusted``, ``view_connections`` then ``view_public``
    (HIGHER_CONSISTENCY, so revocation and blocks apply on the next read) and stops at the
    first allowed: one call for the owner and the owner's agents, at most four otherwise. Each
    tier relation includes the higher ones, so the order cannot under-rank a viewer. ``none``
    when nothing is held (blocked, revoked, disconnected or a stranger).
    """
    client = _check_client(fga)
    subject = subject_for(principal)
    profile = profile_object(profile_id)
    for relation, tier in TIER_CHECKS:
        if await client.check(user=subject, relation=relation, object=profile):
            return tier
    return ViewerTier.none


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
