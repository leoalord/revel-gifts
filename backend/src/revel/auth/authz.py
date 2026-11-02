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
  ``question#read_answer`` does not follow the requester's tier: only a block removes it, so
  F7 (#20) must re-check that the requester can still see the facts an answer relies on.

Tuple hooks are module-level ``on_*`` functions over a process-wide ``TupleHooks`` installed by
``configure_authz``. Every hook is keyword-only, idempotent (retries, duplicate writes and deletes
of missing tuples are safe), OpenFGA-only (no database access: no second pooled connection or
worker thread while the caller holds its transaction) and bounded by ``HOOK_TIMEOUT_SECONDS``.

WHEN to call a hook, relative to the caller's commit, is part of its contract:

- PRE-COMMIT, raising (await inside the changing transaction; a raise rolls the row back):

  - every REVOKE: ``on_agent_disconnected``, ``on_connection_tier_changing``,
    ``on_connection_revoked``, ``on_blocked``, ``on_public_opt_in_changed(enabled=False)``.
    A rollback after a successful revoke only over-denies (reconcile repairs it); a revoke
    after commit would leave access live in between, or forever if it failed.

    ROW WRITE FIRST: in that transaction, run the changing UPDATE/INSERT on
    ``agent_delegations``, ``connection_requests`` or ``contribution_policies`` BEFORE awaiting
    the revoke hook (flush it if the ORM would defer it), so the transaction holds ROW
    EXCLUSIVE on the table before the tuple is deleted. ``fga_reconcile``'s barrier waits only
    for transactions holding a lock on those tables; a writer that deletes the tuple first and
    writes the row afterwards can be overtaken by a reconcile that re-grants from its earlier
    snapshot, reads the row as unchanged and leaves the revoked access live until the next
    reconcile. ``SELECT ... FOR UPDATE`` before the hook is also waited for (the barrier is
    EXCLUSIVE), but an advisory lock, or no lock, is not: neither orders a writer against
    reconcile. Required order:

    - F6 approve or tier change (``connection_requests``): UPDATE the row (status, tier) ->
      ``on_connection_tier_changing(tier=new)`` -> commit -> ``on_connection_granted(tier=new)``.
    - F6 revoke: UPDATE the row -> ``on_connection_revoked`` -> commit.
    - F6 block: UPDATE (or INSERT) the row -> ``on_blocked`` -> commit.
    - F5 Public opt-out: INSERT the new ``contribution_policies`` version ->
      ``on_public_opt_in_changed(enabled=False)`` -> commit. (Opt-in: INSERT -> commit ->
      ``on_public_opt_in_changed(enabled=True)``.)
    - F2 disconnect (already conforming): UPDATE ``agent_delegations`` ->
      ``on_agent_disconnected`` -> commit.
  - grants that name a NEW, uncommitted row's random ID: F2's ``on_user_created`` /
    ``on_agent_delegated`` (``agent:<new delegation id>``), ``visibility.on_profile_created``
    (``profile:<new profile id>``) and ``on_question_created`` (``question:<new question
    id>``). If the commit then fails, the tuples name an ID no row, token or principal carries,
    so nothing can reach them; ``fga_reconcile --prune`` deletes them.

- POST-COMMIT, never raising (await only after the row is committed): grants on EXISTING users
  and profiles, ``on_connection_granted``, ``on_unblocked`` and
  ``on_public_opt_in_changed(enabled=True)``. Run before commit, a later rollback (a failed
  statement, a failed commit, a cancelled call the server had applied) would leave a reachable
  grant: a declined user with access, a blocked user back on Public, a private profile
  public. After commit, a failure only denies: the hook logs an ERROR and returns ``None``,
  the request still succeeds and ``fga_reconcile`` restores the grant. A rolled-back
  transaction never calls them, so it never grants.

- Tier changes (F6) are split: after the row UPDATE and before commit
  ``on_connection_tier_changing(tier=new)`` deletes any HIGHER tier (the revoke part of a
  downgrade; a no-op for an upgrade or first approval); after commit
  ``on_connection_granted(tier=new)`` writes the new tier and deletes the other.

Unconfigured OpenFGA (``configure_authz`` not called, or store/model IDs unset): grant hooks log
one warning and return ``None`` (a skipped grant only denies); revoke hooks raise
``FgaConfigurationError`` (a skipped delete would leave access in place). Pre-commit hooks
propagate every configured OpenFGA error and ``TimeoutError``; post-commit hooks log them.
Checks (``can_contribute``, ``require_*``, ``resolve_viewer_tier``, ``visible_claims``) raise
``FgaConfigurationError`` when unconfigured; they never allow by default. They are bounded:
each OpenFGA check by ``CHECK_TIMEOUT_SECONDS`` and a whole tier resolution by
``TIER_RESOLUTION_TIMEOUT_SECONDS``; past either they fail CLOSED with
``AuthorizationCheckTimeout`` (a ``TimeoutError``, never a grant or a lower tier).

Known residual: a post-commit grant delayed past a LATER revoke of the same row (approve, then
revoke within the grant's latency) can land after the revoke's delete. Serialize changes to the
same row: F6 per (requester, target) pair, F5 Public opt-in changes per profile (e.g. run the
post-commit grant before releasing a per-row lock, or re-read the row after it);
``fga_reconcile`` repairs it either way.

Policy lives in Notion (Relationships, Discovery & Privacy); this module implements it.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable
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


class AuthorizationCheckTimeout(TimeoutError):
    """An OpenFGA check did not answer in time. Fail closed: map to a generic 5xx, never allow.

    Carries no object IDs."""

    def __init__(self) -> None:
        super().__init__("OpenFGA authorization check timed out; access not granted")


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


# Granted tiers, lowest first.
TIER_ORDER: tuple[ConnectionTier, ...] = (ConnectionTier.CONNECTIONS, ConnectionTier.TRUSTED_CIRCLE)

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

    async def on_connection_tier_changing(
        self, *, profile_id: UUID, viewer_user_id: UUID, tier: ConnectionTier | str
    ) -> TupleSync:
        """Pre-commit half of an approval or tier change: delete every tier above ``tier``."""
        rank = TIER_ORDER.index(ConnectionTier(tier))
        deletes = tuple(tier_tuple(profile_id, viewer_user_id, t) for t in TIER_ORDER[rank + 1 :])
        await self._fga.delete_tuples(deletes)
        return TupleSync(deleted=deletes)

    async def on_connection_granted(
        self, *, profile_id: UUID, viewer_user_id: UUID, tier: ConnectionTier | str
    ) -> TupleSync:
        """Post-commit half: write ``tier``, then delete the other tier (never a widening)."""
        tier = ConnectionTier(tier)
        writes = (tier_tuple(profile_id, viewer_user_id, tier),)
        deletes = tuple(
            tier_tuple(profile_id, viewer_user_id, t) for t in ConnectionTier if t is not tier
        )
        await self._fga.write_tuples(writes)
        await self._fga.delete_tuples(deletes)
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


async def _post_commit(
    name: str, call: Callable[[TupleHooks], Awaitable[TupleSync]]
) -> TupleSync | None:
    """Run a post-commit grant: ``None`` while unconfigured (one warning) or on failure.

    The row is already committed, so raising would only fail a request that succeeded; the
    grant stays missing (access denied) until a retry or ``fga_reconcile``. Cancellation still
    propagates.
    """
    hooks = _hooks_or_none()
    if hooks is None:
        return None
    try:
        return await bounded(call(hooks))
    except Exception as exc:  # logged; a missing grant only denies
        # The exception text may carry object IDs; the class name is enough here.
        logger.error(
            "OpenFGA post-commit grant %s failed (%s); access stays denied until reconcile "
            "(python -m revel.auth.fga_reconcile) restores it",
            name,
            type(exc).__name__,
        )
        return None


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


# v2 hooks for F5, F6 and F7. Pre-commit (revoke-class, raising) or post-commit (grant-class,
# logging); see the module docstring. Every call is keyword-only and idempotent.


async def on_connection_tier_changing(
    *, profile_id: UUID, viewer_user_id: UUID, tier: ConnectionTier | str
) -> TupleSync:
    """F6, PRE-COMMIT, on every approval or tier change to ``tier``, AFTER the row UPDATE in the
    same transaction: deletes any tier above it (the revoke part of a downgrade; a no-op
    otherwise). Revoke-class: raises when unconfigured. Pair it with ``on_connection_granted``
    after commit."""
    tier = ConnectionTier(tier)
    hooks = _revoke_hooks()
    return await bounded(
        hooks.on_connection_tier_changing(
            profile_id=profile_id, viewer_user_id=viewer_user_id, tier=tier
        )
    )


async def on_connection_granted(
    *, profile_id: UUID, viewer_user_id: UUID, tier: ConnectionTier | str
) -> TupleSync | None:
    """F6, POST-COMMIT: the profile's owner approved ``viewer_user_id`` at ``tier``
    (``connections`` or ``trusted_circle``), or changed their tier. Writes ``tier`` and deletes
    the other tier. Never raises for OpenFGA failures (logs, returns ``None``); an invalid
    ``tier`` raises ``ValueError`` before any call."""
    tier = ConnectionTier(tier)
    return await _post_commit(
        "on_connection_granted",
        lambda hooks: hooks.on_connection_granted(
            profile_id=profile_id, viewer_user_id=viewer_user_id, tier=tier
        ),
    )


async def on_connection_revoked(*, profile_id: UUID, viewer_user_id: UUID) -> TupleSync:
    """F6, PRE-COMMIT, AFTER the row UPDATE: the owner revoked the viewer's access to the
    profile (both tiers). Revoke-class."""
    hooks = _revoke_hooks()
    return await bounded(
        hooks.on_connection_revoked(profile_id=profile_id, viewer_user_id=viewer_user_id)
    )


async def on_blocked(*, profile_id: UUID, blocked_user_id: UUID) -> TupleSync:
    """F6, PRE-COMMIT, AFTER the row UPDATE/INSERT: the profile's owner blocked
    ``blocked_user_id``: that user and their agents lose every tier on this profile only, and
    the tier tuples are deleted, so an unblock never restores a tier without a new approval.
    Revoke-class."""
    hooks = _revoke_hooks()
    return await bounded(hooks.on_blocked(profile_id=profile_id, blocked_user_id=blocked_user_id))


async def on_unblocked(*, profile_id: UUID, blocked_user_id: UUID) -> TupleSync | None:
    """F6, POST-COMMIT: the owner lifted a block (deletes ``blocked``; restores no tier).
    Grant-class: logs and returns ``None`` on failure."""
    return await _post_commit(
        "on_unblocked",
        lambda hooks: hooks.on_unblocked(profile_id=profile_id, blocked_user_id=blocked_user_id),
    )


async def on_public_opt_in_changed(*, profile_id: UUID, enabled: bool) -> TupleSync | None:
    """F5: a new contribution-policy version changed ``public_opt_in``.

    ``enabled=False`` (withdrawal) is PRE-COMMIT, AFTER the policy INSERT, and revoke-class: it
    raises. ``enabled=True``
    is POST-COMMIT and grant-class: it logs and returns ``None`` on failure.
    """
    if not enabled:
        hooks = _revoke_hooks()
        return await bounded(hooks.on_public_opt_in_changed(profile_id=profile_id, enabled=False))
    return await _post_commit(
        "on_public_opt_in_changed",
        lambda hooks: hooks.on_public_opt_in_changed(profile_id=profile_id, enabled=True),
    )


async def on_question_created(
    *, question_id: UUID, profile_id: UUID, requester_user_id: UUID
) -> TupleSync | None:
    """F7, PRE-COMMIT in the inserting transaction: a question about ``profile_id`` by
    ``requester_user_id`` was created. Grant-class (warns while unconfigured) but raising when
    configured: on rollback the tuples name a question ID no row has, which F7 never serves."""
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


# Bounds for read-path checks (the SDK default is 300s per request, with retries). A single
# check, and a whole tier resolution (up to four checks), fail closed past these.
CHECK_TIMEOUT_SECONDS = 3.0
TIER_RESOLUTION_TIMEOUT_SECONDS = 5.0


async def _bounded_check(client: FgaClient, *, user: str, relation: str, object: str) -> bool:
    """One OpenFGA check, raising ``AuthorizationCheckTimeout`` after ``CHECK_TIMEOUT_SECONDS``."""
    try:
        with anyio.fail_after(CHECK_TIMEOUT_SECONDS):
            return await client.check(user=user, relation=relation, object=object)
    except TimeoutError as exc:
        raise AuthorizationCheckTimeout() from exc


def _check_client(fga: FgaClient | None) -> FgaClient:
    """The client for a check: explicit, else the configured one. Never allow when unconfigured."""
    client = fga if fga is not None else (_state.fga if _state is not None else None)
    if client is None or not client.configured:
        raise FgaConfigurationError("OpenFGA is not configured; authorization checks cannot run")
    return client


async def can_contribute(fga: FgaClient | None, principal: PrincipalLike, profile_id: UUID) -> bool:
    """Whether the principal may contribute to the profile. ``fga=None`` uses the configured one."""
    return await _bounded_check(
        _check_client(fga),
        user=subject_for(principal),
        relation=CONTRIBUTE,
        object=profile_object(profile_id),
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

    Bounded: each check by ``CHECK_TIMEOUT_SECONDS`` and the whole resolution by
    ``TIER_RESOLUTION_TIMEOUT_SECONDS``; a timeout raises ``AuthorizationCheckTimeout`` (fail
    closed: never a tier, not even a lower one already passed over).
    """
    client = _check_client(fga)
    subject = subject_for(principal)
    profile = profile_object(profile_id)
    try:
        with anyio.fail_after(TIER_RESOLUTION_TIMEOUT_SECONDS):
            for relation, tier in TIER_CHECKS:
                if await _bounded_check(client, user=subject, relation=relation, object=profile):
                    return tier
    except AuthorizationCheckTimeout:
        raise
    except TimeoutError as exc:
        raise AuthorizationCheckTimeout() from exc
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
# Adapters (MCP/HTTP) must map AuthorizationDenied to a generic denial and OpenFGA SDK,
# FgaConfigurationError or AuthorizationCheckTimeout exceptions to a generic 5xx, and never
# return their text: SDK errors contain object IDs (UUIDs).
