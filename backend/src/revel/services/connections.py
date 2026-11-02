"""Connections (F6): handles, exact-handle lookup, directional requests and their management.

Policy: Notion "Relationships, Discovery & Privacy" (MVP connection mechanism). All
enforcement lives here; the MCP tools (``revel.mcp.connection_tools``) and the HTTP routes
(``revel.api.connections``) are thin adapters over these functions.

Public functions (each takes the acting ``Principal``; F10's demo reset uses the management
ones directly):

- ``get_my_handle`` / ``set_handle``: the caller's own handle, discoverability and avatar URL.
- ``resolve_handle(session, principal, handle=)``: the shared exact-handle resolver (also used
  by F5 for other-person reads). Never creates a request.
- ``lookup_and_request(principal, handle=)``: resolve, then create or return the live request.
- ``list_incoming`` / ``list_outgoing`` / ``list_connections`` / ``list_blocked``.
- Target side: ``approve``, ``decline``, ``block``, ``unblock``, ``change_tier``, ``revoke``.
- Requester side: ``cancel_request``.

Hidden targets. ``resolve_handle`` and ``lookup_and_request`` raise the same ``TargetNotFound``
(one message, one adapter mapping) for a nonexistent handle, a malformed handle, an
undiscoverable target the caller holds no approved connection to, a target who blocked the
caller and (for ``lookup_and_request`` only) the caller's own handle. All of these are decided
by ONE SQL statement (``_resolve_sync``), so no hidden case skips database work the others do.
Rate limits are checked before the handle is resolved, so being rate limited never depends on
whether the handle exists. A connected viewer (approved row, caller -> target) still resolves
a target who has since turned discovery off: discoverability governs finding, not access.

Directionality. A request (requester -> target) asks for access to the TARGET's profile; only
the target (directly, or through one of the target's own active delegations) responds. Approval
grants the requester ``connections`` or ``trusted_circle`` on the target's profile only;
reverse access needs its own request.

Status machine (F3's ``connection_requests``; at most one live pending/approved/blocked row per
directed pair): pending -> approved | declined | blocked | cancelled (requester);
approved -> approved (tier change) | revoked | blocked; blocked -> revoked (unblock). A new
request after decline, cancel, revoke or unblock creates a new row (subject to the rate limit).
Blocking with no live row (e.g. after a decline) inserts a new ``blocked`` row.

Block semantics. A block is directional at the database level (target blocked requester) and in
OpenFGA (``profile:<target>#blocked@user:<requester>``). Blocking also CANCELS the blocker's own
pending outgoing request to the blocked user (it would otherwise still ask them for access), in a
separate short transaction after the block commits (never two pair locks in one transaction). It
does not touch access the blocked user previously granted the blocker: that is the blocked
user's own decision. The blocked requester sees the target exactly as a nonexistent handle; their
outgoing list never shows declined, cancelled, revoked or blocked rows, so a block is
indistinguishable from a decline. Unblocking restores no tier (``on_blocked`` deleted the tier
tuples); the old requester must request again.

OpenFGA writes (F4 hook contract, ``revel.auth.authz`` docstring) and serialization. Every write
to a directed pair runs under a per-pair transaction-scoped advisory lock
(``pg_advisory_xact_lock`` on a hash of requester and target), taken FIRST, before any table
lock. A lock cannot be held across COMMIT, so grant-class changes use two transactions:

- approve / tier change: Tx1 = pair lock -> ``SELECT ... FOR UPDATE`` -> UPDATE ->
  ``on_connection_tier_changing`` (pre-commit, raising: a failure rolls the row back) -> COMMIT;
  Tx2 = pair lock -> re-read the live row -> ONLY if it is still approved with that tier,
  ``on_connection_granted`` (never raises) -> COMMIT.
- revoke / block: pair lock -> ``SELECT ... FOR UPDATE`` -> UPDATE (or INSERT) -> revoke hook
  (``on_connection_revoked`` / ``on_blocked``; raising) -> COMMIT.
- unblock: Tx1 = pair lock -> UPDATE blocked -> revoked -> COMMIT; Tx2 = pair lock -> only if
  no live blocked row, ``on_unblocked``.

A revoke/downgrade/block therefore either runs entirely before Tx2 (Tx2 re-reads the change and
grants nothing) or waits for Tx2 to finish (its delete lands after the grant): a delayed grant
can never land after a later revoke. The row write always precedes the revoke hook (so the
reconcile barrier, EXCLUSIVE on ``connection_requests``, waits for us), and these transactions
touch no ``contribution_policies``, ``claims`` or ``claim_events`` (lock-order rule). Every
transaction sets ``lock_timeout`` (``LOCK_TIMEOUT_MS``): waiting on a pair lock or on the
reconcile barrier fails CLOSED with the retryable ``ConnectionsUnavailable``. If a grant (Tx2)
fails or times out, Postgres stays approved and ``fga_reconcile`` (scheduled) restores it: a
missing grant only denies. A revoke-hook failure rolls the change back; a commit failure after a
successful revoke hook only over-denies until reconcile re-grants.

Profiles. Tier and block tuples live on ``profile:<target profile id>``. Approval (and tier
change) requires the approver to have a profile (``ProfileNotSetUp`` otherwise; F5's #9 profile
creation is not merged yet). Block and unblock work without one (the row still hides the target
from lookups); with no profile there is nothing to protect in OpenFGA, and reconcile writes the
``blocked`` tuple once the profile exists.

Rate limits (module constants). ``REQUEST_LIMIT`` new requests per requester per
``REQUEST_WINDOW`` are counted in Postgres via ``(requester_user_id, created_at)`` under a
per-requester advisory lock (exact across instances); blocked rows the target inserted are not
the requester's and do not count. ``LOOKUP_LIMIT`` per ``LOOKUP_WINDOW`` is an in-process sliding
window (no table exists for failed lookups; per instance, reset on restart) that slows handle
enumeration: every ``lookup_and_request`` and every ``set_handle`` that supplies a handle (its
"unavailable" answer reveals occupancy) records an attempt before any database work;
``resolve_handle`` checks the limit first but records only not-found results.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import anyio.from_thread
import anyio.to_thread
from pydantic import BaseModel
from sqlalchemy import and_, exists, func, insert, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, aliased, sessionmaker

from revel.auth import authz
from revel.auth.authz import AuthorizationDenied, PrincipalLike, subject_for
from revel.db.enums import (
    LIVE_CONNECTION_REQUEST_STATUSES,
    ConnectionRequestStatus,
    ConnectionTier,
    DelegationStatus,
)
from revel.db.models import Agent, AgentDelegation, ConnectionRequest, Handle, Profile, User
from revel.db.models.connections import AVATAR_URL_MAX_LENGTH, HANDLE_PATTERN

logger = logging.getLogger(__name__)

# --- Constants --------------------------------------------------------------------------------

REQUEST_LIMIT = 20  # new connection requests per requester ...
REQUEST_WINDOW = timedelta(hours=1)  # ... per rolling window (Postgres, exact)
LOOKUP_LIMIT = 60  # handle lookups (found or not) per user ...
LOOKUP_WINDOW = timedelta(hours=1)  # ... per rolling window (in-process, per instance)
LOCK_TIMEOUT_MS = 5000  # pair/requester advisory locks and row locks (fail closed past it)
LIST_LIMIT = 200  # rows per list call

_HANDLE_RE = re.compile(HANDLE_PATTERN)
_PENDING = ConnectionRequestStatus.PENDING.value
_APPROVED = ConnectionRequestStatus.APPROVED.value
_BLOCKED = ConnectionRequestStatus.BLOCKED.value
_LIVE = tuple(s.value for s in LIVE_CONNECTION_REQUEST_STATUSES)

SessionFactory = Callable[[], sessionmaker[Session]]

# --- Errors (adapters map each to one fixed message; never pass ``str(exc)`` through) ----------


class ConnectionsError(Exception):
    """Base class. ``message`` is safe to show to a user or an agent."""

    message = "Connection operation failed"

    def __init__(self) -> None:
        super().__init__(self.message)


class TargetNotFound(ConnectionsError):
    """Nonexistent, malformed, undiscoverable (and unconnected), blocking or own handle."""

    message = "No Revel user with that handle is available to connect with"


class RequestNotFound(ConnectionsError):
    """No such request, or the caller may not act on it (identical on purpose)."""

    message = "Connection request not found"


class InvalidState(ConnectionsError):
    message = "This connection request is not in a state that allows that action"


class RateLimited(ConnectionsError):
    message = "Too many connection requests or lookups; try again later"


class InvalidHandle(ConnectionsError):
    message = "Handles are 3-30 characters: lowercase letters, digits and underscores"


class HandleUnavailable(ConnectionsError):
    message = "That handle is unavailable"


class InvalidAvatarUrl(ConnectionsError):
    message = f"Avatar URL must be an https URL of at most {AVATAR_URL_MAX_LENGTH} characters"


class InvalidTier(ConnectionsError):
    message = "Tier must be 'connections' or 'trusted_circle'"


class ProfileNotSetUp(ConnectionsError):
    message = "Set up your Revel profile before approving connections"


class NotAllowed(ConnectionsError):
    """The principal is inconsistent or its delegation is no longer active."""

    message = "Not authorized"


class ConnectionsUnavailable(ConnectionsError):
    """Lock timeout, reconcile in progress or an OpenFGA failure: nothing changed; retry."""

    message = "Connections are temporarily unavailable; nothing was changed, please retry"


# --- Result models ----------------------------------------------------------------------------


class PublicIdentity(BaseModel):
    """The permitted minimal identity (Privacy: display name, handle, avatar)."""

    display_name: str | None
    handle: str | None
    avatar_url: str | None


class AssistantInfo(BaseModel):
    """The assistant that acted for a person (shown to the other side of a request)."""

    name: str | None
    client_id: str


class Person(PublicIdentity):
    """A requester as shown to the target: verified identity plus the acting assistant."""

    assistant: AssistantInfo | None = None


class RequestInfo(BaseModel):
    id: UUID
    status: str
    created_at: datetime


class RequestResult(BaseModel):
    """``lookup_and_request``: the target's minimal identity and the live request."""

    target: PublicIdentity
    request: RequestInfo
    created: bool


class IncomingRequest(BaseModel):
    id: UUID
    status: str
    created_at: datetime
    requester: Person


class OutgoingRequest(BaseModel):
    """Only pending and approved requests are listed (a block looks like a decline)."""

    id: UUID
    status: str
    created_at: datetime
    target: PublicIdentity


class ConnectionEntry(BaseModel):
    """Someone the caller approved: they see the caller's profile at ``tier``."""

    request_id: UUID
    tier: str
    since: datetime | None
    person: Person


class BlockedEntry(BaseModel):
    request_id: UUID
    blocked_at: datetime | None
    person: PublicIdentity


class ResponseResult(BaseModel):
    """Outcome of a target-side or requester-side action on a request."""

    request_id: UUID
    status: str
    tier: str | None


class HandleInfo(BaseModel):
    handle: str
    discoverable: bool
    avatar_url: str | None


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """``resolve_handle``'s result. ``profile_id`` is ``None`` until the target has a profile."""

    user_id: UUID
    profile_id: UUID | None
    display_name: str | None
    handle: str
    avatar_url: str | None


# --- Helpers ----------------------------------------------------------------------------------


def _default_sessionmaker() -> sessionmaker[Session]:
    from revel.db.session import get_sessionmaker

    return get_sessionmaker()


def _factory(sessions: SessionFactory | None) -> sessionmaker[Session]:
    return (sessions or _default_sessionmaker)()


def normalize_handle(handle: str) -> str | None:
    """Exact-match form (trimmed, lowercased, one leading ``@`` dropped) or ``None`` if invalid."""
    if not isinstance(handle, str):
        return None
    value = handle.strip().lower()
    if value.startswith("@"):
        value = value[1:]
    return value if _HANDLE_RE.fullmatch(value) else None


def _lock_key(namespace: str, *ids: UUID) -> int:
    """Signed 64-bit advisory-lock key for ``namespace`` and the ordered ids."""
    raw = ":".join([namespace, *(str(i) for i in ids)]).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=True)


def pair_lock_key(requester_user_id: UUID, target_user_id: UUID) -> int:
    """The per-directed-pair lock every write to that pair takes first."""
    return _lock_key("revel.connections.pair", requester_user_id, target_user_id)


def _begin(session: Session) -> None:
    session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'"))


def _advisory_lock(session: Session, key: int) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def _lock_pair(session: Session, requester_user_id: UUID, target_user_id: UUID) -> None:
    _advisory_lock(session, pair_lock_key(requester_user_id, target_user_id))


def _check_actor(session: Session, principal: PrincipalLike) -> None:
    """Consistent principal; an agent principal's delegation must still be active."""
    try:
        subject_for(principal)
    except AuthorizationDenied:
        raise NotAllowed() from None
    if principal.delegation_id is None:
        return
    status = session.execute(
        select(AgentDelegation.status).where(
            AgentDelegation.id == principal.delegation_id,
            AgentDelegation.user_id == principal.user_id,
        )
    ).scalar_one_or_none()
    if status != DelegationStatus.ACTIVE.value:
        raise NotAllowed()


def _tier(tier: str | ConnectionTier) -> ConnectionTier:
    try:
        return ConnectionTier(tier)
    except ValueError:
        raise InvalidTier() from None


def _profile_id(session: Session, user_id: UUID) -> UUID | None:
    return session.execute(
        select(Profile.id).where(Profile.user_id == user_id)
    ).scalar_one_or_none()


def _hook(fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Run an async authz hook on the event loop from this worker thread.

    For pre-commit (revoke-class) hooks every failure becomes ``ConnectionsUnavailable``, which
    propagates through ``session.begin()`` and rolls the row change back.
    """
    try:
        return anyio.from_thread.run(partial(fn, **kwargs))
    except Exception as exc:
        logger.warning("connections: authorization hook failed (%s)", type(exc).__name__)
        raise ConnectionsUnavailable() from exc


async def _run[T](fn: Callable[[], T]) -> T:
    """Run blocking DB work in a worker thread; lock timeouts become ``ConnectionsUnavailable``."""
    try:
        return await anyio.to_thread.run_sync(fn)
    except OperationalError as exc:
        logger.warning("connections: database operation failed (%s)", type(exc.orig).__name__)
        raise ConnectionsUnavailable() from exc


# --- In-process lookup limiter ----------------------------------------------------------------


class _SlidingWindow:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[UUID, deque[float]] = {}

    def _current(self, key: UUID, window: timedelta, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        horizon = now - window.total_seconds()
        while hits and hits[0] <= horizon:
            hits.popleft()
        return hits

    def hit(self, key: UUID, limit: int, window: timedelta) -> bool:
        """Record one attempt; ``False`` when ``limit`` attempts already fall in ``window``."""
        now = time.monotonic()
        with self._lock:
            hits = self._current(key, window, now)
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def exceeded(self, key: UUID, limit: int, window: timedelta) -> bool:
        """Whether ``limit`` attempts already fall in ``window`` (records nothing)."""
        with self._lock:
            return len(self._current(key, window, time.monotonic())) >= limit

    def record(self, key: UUID) -> None:
        """Record one attempt without checking (the caller checked ``exceeded`` first)."""
        with self._lock:
            self._hits.setdefault(key, deque()).append(time.monotonic())

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


lookup_limiter = _SlidingWindow()


def _count_lookup(principal: PrincipalLike) -> None:
    if not lookup_limiter.hit(principal.user_id, LOOKUP_LIMIT, LOOKUP_WINDOW):
        raise RateLimited()


# --- Handles ----------------------------------------------------------------------------------


def _valid_avatar(url: str | None) -> str | None:
    if url is None:
        return None
    url = url.strip()
    if not url:
        return None
    parts = urlsplit(url)
    if len(url) > AVATAR_URL_MAX_LENGTH or parts.scheme != "https" or not parts.netloc:
        raise InvalidAvatarUrl()
    return url


_UNSET: Any = object()


async def get_my_handle(
    principal: PrincipalLike, *, sessions: SessionFactory | None = None
) -> HandleInfo | None:
    factory = _factory(sessions)

    def work() -> HandleInfo | None:
        with factory() as session:
            _check_actor(session, principal)
            row = session.get(Handle, principal.user_id)
            if row is None:
                return None
            return HandleInfo(
                handle=row.handle, discoverable=row.discoverable, avatar_url=row.avatar_url
            )

    return await _run(work)


async def set_handle(
    principal: PrincipalLike,
    *,
    handle: str | None = None,
    discoverable: bool | None = None,
    avatar_url: str | None = _UNSET,
    sessions: SessionFactory | None = None,
) -> HandleInfo:
    """Claim or change the caller's own handle, discoverability and avatar (owner only).

    ``handle`` is required the first time. ``avatar_url`` left unset keeps the current value;
    ``None`` or ``""`` clears it. A taken handle raises ``HandleUnavailable``.

    ``HandleUnavailable`` reveals that SOME account holds the handle, including an
    undiscoverable one, so every call that supplies a handle counts against the lookup limit
    BEFORE any validation or database work (``RateLimited`` once exhausted, for taken and free
    handles alike). Calls that change only ``discoverable`` / ``avatar_url`` are not counted.
    """
    normalized = None
    if handle is not None:
        _count_lookup(principal)
        normalized = normalize_handle(handle)
        if normalized is None:
            raise InvalidHandle()
    avatar = _UNSET if avatar_url is _UNSET else _valid_avatar(avatar_url)
    factory = _factory(sessions)

    def work() -> HandleInfo:
        with factory() as session, session.begin():
            _begin(session)
            _check_actor(session, principal)
            row = session.get(Handle, principal.user_id, with_for_update=True)
            if row is None:
                if normalized is None:
                    raise InvalidHandle()
                row = Handle(user_id=principal.user_id, handle=normalized)
                session.add(row)
            elif normalized is not None:
                row.handle = normalized
            if discoverable is not None:
                row.discoverable = discoverable
            if avatar is not _UNSET:
                row.avatar_url = avatar
            try:
                with session.begin_nested():
                    session.flush()
            except IntegrityError:
                raise HandleUnavailable() from None
            return HandleInfo(
                handle=row.handle, discoverable=row.discoverable, avatar_url=row.avatar_url
            )

    return await _run(work)


# --- Resolution -------------------------------------------------------------------------------


def _resolve_sync(
    session: Session, viewer_user_id: UUID, handle: str | None, *, allow_self: bool
) -> ResolvedTarget:
    """THE single not-found path: one statement decides every hidden case."""
    if handle is None:
        # Malformed: run the same statement with a value no row can hold.
        handle = ""
    viewer = viewer_user_id
    blocked_me = exists().where(
        ConnectionRequest.requester_user_id == viewer,
        ConnectionRequest.target_user_id == Handle.user_id,
        ConnectionRequest.status == _BLOCKED,
    )
    connected = exists().where(
        ConnectionRequest.requester_user_id == viewer,
        ConnectionRequest.target_user_id == Handle.user_id,
        ConnectionRequest.status == _APPROVED,
    )
    stmt = (
        select(Handle.user_id, Handle.handle, Handle.avatar_url, User.display_name, Profile.id)
        .join(User, User.id == Handle.user_id)
        .outerjoin(Profile, Profile.user_id == Handle.user_id)
        .where(
            Handle.handle == handle,
            ~blocked_me,
            or_(Handle.discoverable, connected, Handle.user_id == viewer),
        )
    )
    if not allow_self:
        stmt = stmt.where(Handle.user_id != viewer)
    row = session.execute(stmt).one_or_none()
    if row is None:
        raise TargetNotFound()
    return ResolvedTarget(
        user_id=row[0], handle=row[1], avatar_url=row[2], display_name=row[3], profile_id=row[4]
    )


async def resolve_handle(
    session: Session, principal: PrincipalLike, *, handle: str
) -> ResolvedTarget:
    """Resolve an exact handle for the caller, or raise ``TargetNotFound``.

    Not found (identically): nonexistent or malformed handle, a target who blocked the caller,
    and an undiscoverable target the caller holds no approved connection to. The caller's own
    handle resolves (to themselves). Creates nothing; access to the target's profile is still
    decided by F4 (``visible_claims``).

    Rate limit: the lookup limit is checked first (``RateLimited`` once exhausted, whatever the
    handle), but only a ``TargetNotFound`` result records an attempt. Successful resolves (e.g.
    F5's repeated reads of a connection's profile) cost nothing, while enumerating handles,
    which produces mostly not-found results, spends the quota. Every hidden case takes the same
    not-found path (one statement, then one record), so recording adds no difference between
    them; a found handle is already distinguishable by its result.
    """
    if lookup_limiter.exceeded(principal.user_id, LOOKUP_LIMIT, LOOKUP_WINDOW):
        raise RateLimited()
    normalized = normalize_handle(handle)

    def work() -> ResolvedTarget:
        _check_actor(session, principal)
        return _resolve_sync(session, principal.user_id, normalized, allow_self=True)

    try:
        return await _run(work)
    except TargetNotFound:
        lookup_limiter.record(principal.user_id)
        raise


# --- Requests ---------------------------------------------------------------------------------


def _live_row(
    session: Session, requester: UUID, target: UUID, *, for_update: bool
) -> ConnectionRequest | None:
    stmt = select(ConnectionRequest).where(
        ConnectionRequest.requester_user_id == requester,
        ConnectionRequest.target_user_id == target,
        ConnectionRequest.status.in_(_LIVE),
    )
    if for_update:
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


async def lookup_and_request(
    principal: PrincipalLike, *, handle: str, sessions: SessionFactory | None = None
) -> RequestResult:
    """Exact-handle lookup, then create (or return the existing live) request to that person.

    Raises ``RateLimited`` (checked before resolution), ``TargetNotFound`` (every hidden case,
    including the caller's own handle and a target who blocked the caller) or
    ``ConnectionsUnavailable``. A duplicate returns the existing pending or approved row with
    ``created=False``; the requester never learns the granted tier.
    """
    _count_lookup(principal)
    normalized = normalize_handle(handle)
    factory = _factory(sessions)
    me = principal.user_id

    def work() -> RequestResult:
        with factory() as session, session.begin():
            _begin(session)
            _check_actor(session, principal)
            # Serializes this requester's creations so the count below is exact.
            _advisory_lock(session, _lock_key("revel.connections.requester", me))
            recent = session.execute(
                select(func.count())
                .select_from(ConnectionRequest)
                .where(
                    ConnectionRequest.requester_user_id == me,
                    ConnectionRequest.created_at > func.now() - REQUEST_WINDOW,
                    # Blocks the TARGET inserted (no live row to update) name this user as
                    # requester but were not created by them: inserted already blocked, so
                    # responded_at equals created_at (one transaction's now()). A request
                    # blocked later was responded to in a later transaction.
                    ~and_(
                        ConnectionRequest.status == _BLOCKED,
                        ConnectionRequest.responded_at == ConnectionRequest.created_at,
                    ),
                )
            ).scalar_one()
            if recent >= REQUEST_LIMIT:
                raise RateLimited()
            target = _resolve_sync(session, me, normalized, allow_self=False)
            identity = PublicIdentity(
                display_name=target.display_name,
                handle=target.handle,
                avatar_url=target.avatar_url,
            )
            _lock_pair(session, me, target.user_id)
            row = _live_row(session, me, target.user_id, for_update=True)
            created = False
            if row is not None and row.status == _BLOCKED:
                raise TargetNotFound()  # blocked after resolution; same response
            if row is None:
                try:
                    with session.begin_nested():
                        row = session.execute(
                            insert(ConnectionRequest)
                            .values(
                                requester_user_id=me,
                                target_user_id=target.user_id,
                                requester_delegation_id=principal.delegation_id,
                            )
                            .returning(ConnectionRequest)
                        ).scalar_one()
                    created = True
                except IntegrityError:
                    # A concurrent creation won the partial unique index; return its row.
                    row = _live_row(session, me, target.user_id, for_update=False)
                    if row is None or row.status == _BLOCKED:
                        raise TargetNotFound() from None
            return RequestResult(
                target=identity,
                request=RequestInfo(id=row.id, status=row.status, created_at=row.created_at),
                created=created,
            )

    return await _run(work)


# --- Lists ------------------------------------------------------------------------------------


def _person(row: Mapping[str, Any], *, with_assistant: bool) -> Person:
    assistant = None
    if with_assistant and row.get("client_id") is not None:
        assistant = AssistantInfo(name=row.get("agent_name"), client_id=row["client_id"])
    return Person(
        display_name=row["display_name"],
        handle=row["handle"],
        avatar_url=row["avatar_url"],
        assistant=assistant,
    )


def _people_query(other_user: Any, delegation_col: Any | None) -> Any:
    """Select request rows joined to the other person's identity (and assistant)."""
    cols: list[Any] = [
        ConnectionRequest,
        User.display_name.label("display_name"),
        Handle.handle.label("handle"),
        Handle.avatar_url.label("avatar_url"),
    ]
    if delegation_col is not None:
        cols += [Agent.name.label("agent_name"), Agent.oauth_client_id.label("client_id")]
    stmt = (
        select(*cols)
        .join(User, User.id == other_user)
        .outerjoin(Handle, Handle.user_id == other_user)
    )
    if delegation_col is not None:
        delegation = aliased(AgentDelegation)
        stmt = stmt.outerjoin(delegation, delegation.id == delegation_col).outerjoin(
            Agent, Agent.id == delegation.agent_id
        )
    return stmt


async def list_incoming(
    principal: PrincipalLike, *, sessions: SessionFactory | None = None
) -> list[IncomingRequest]:
    """Pending requests TO the caller, newest first, with each requester's acting assistant."""
    factory = _factory(sessions)

    def work() -> list[IncomingRequest]:
        with factory() as session:
            _check_actor(session, principal)
            stmt = (
                _people_query(
                    ConnectionRequest.requester_user_id, ConnectionRequest.requester_delegation_id
                )
                .where(
                    ConnectionRequest.target_user_id == principal.user_id,
                    ConnectionRequest.status == _PENDING,
                )
                .order_by(ConnectionRequest.created_at.desc())
                .limit(LIST_LIMIT)
            )
            out = []
            for row in session.execute(stmt).mappings():
                req = row["ConnectionRequest"]
                out.append(
                    IncomingRequest(
                        id=req.id,
                        status=req.status,
                        created_at=req.created_at,
                        requester=_person(row, with_assistant=True),
                    )
                )
            return out

    return await _run(work)


async def list_outgoing(
    principal: PrincipalLike, *, sessions: SessionFactory | None = None
) -> list[OutgoingRequest]:
    """The caller's pending and approved requests (never declined/cancelled/revoked/blocked)."""
    factory = _factory(sessions)

    def work() -> list[OutgoingRequest]:
        with factory() as session:
            _check_actor(session, principal)
            stmt = (
                _people_query(ConnectionRequest.target_user_id, None)
                .where(
                    ConnectionRequest.requester_user_id == principal.user_id,
                    ConnectionRequest.status.in_((_PENDING, _APPROVED)),
                )
                .order_by(ConnectionRequest.created_at.desc())
                .limit(LIST_LIMIT)
            )
            out = []
            for row in session.execute(stmt).mappings():
                req = row["ConnectionRequest"]
                out.append(
                    OutgoingRequest(
                        id=req.id,
                        status=req.status,
                        created_at=req.created_at,
                        target=PublicIdentity(
                            display_name=row["display_name"],
                            handle=row["handle"],
                            avatar_url=row["avatar_url"],
                        ),
                    )
                )
            return out

    return await _run(work)


async def list_connections(
    principal: PrincipalLike, *, sessions: SessionFactory | None = None
) -> list[ConnectionEntry]:
    """People the caller approved, with the tier each holds on the caller's profile."""
    factory = _factory(sessions)

    def work() -> list[ConnectionEntry]:
        with factory() as session:
            _check_actor(session, principal)
            stmt = (
                _people_query(
                    ConnectionRequest.requester_user_id, ConnectionRequest.requester_delegation_id
                )
                .where(
                    ConnectionRequest.target_user_id == principal.user_id,
                    ConnectionRequest.status == _APPROVED,
                )
                .order_by(ConnectionRequest.responded_at.desc())
                .limit(LIST_LIMIT)
            )
            out = []
            for row in session.execute(stmt).mappings():
                req = row["ConnectionRequest"]
                out.append(
                    ConnectionEntry(
                        request_id=req.id,
                        tier=req.granted_tier,
                        since=req.responded_at,
                        person=_person(row, with_assistant=False),
                    )
                )
            return out

    return await _run(work)


async def list_blocked(
    principal: PrincipalLike, *, sessions: SessionFactory | None = None
) -> list[BlockedEntry]:
    """People the caller blocked (for unblocking)."""
    factory = _factory(sessions)

    def work() -> list[BlockedEntry]:
        with factory() as session:
            _check_actor(session, principal)
            stmt = (
                _people_query(ConnectionRequest.requester_user_id, None)
                .where(
                    ConnectionRequest.target_user_id == principal.user_id,
                    ConnectionRequest.status == _BLOCKED,
                )
                .order_by(ConnectionRequest.updated_at.desc())
                .limit(LIST_LIMIT)
            )
            out = []
            for row in session.execute(stmt).mappings():
                req = row["ConnectionRequest"]
                out.append(
                    BlockedEntry(
                        request_id=req.id,
                        blocked_at=req.updated_at,
                        person=PublicIdentity(
                            display_name=row["display_name"],
                            handle=row["handle"],
                            avatar_url=row["avatar_url"],
                        ),
                    )
                )
            return out

    return await _run(work)


# --- Actions ----------------------------------------------------------------------------------

Side = Literal["target", "requester"]


def _pair_for(
    session: Session, principal: PrincipalLike, request_id: UUID, side: Side
) -> tuple[UUID, UUID]:
    """(requester, target) of a request the caller is on ``side`` of, else ``RequestNotFound``."""
    me = principal.user_id
    owner_col = ConnectionRequest.target_user_id
    if side == "requester":
        owner_col = ConnectionRequest.requester_user_id
    row = session.execute(
        select(ConnectionRequest.requester_user_id, ConnectionRequest.target_user_id).where(
            ConnectionRequest.id == request_id, owner_col == me
        )
    ).one_or_none()
    if row is None:
        raise RequestNotFound()
    return row[0], row[1]


def _locked_row(
    session: Session, principal: PrincipalLike, request_id: UUID, side: Side
) -> ConnectionRequest:
    """Check the caller's side of the request, take the pair lock, then row-lock the request.

    Ownership is checked once, before the lock: a row's requester and target never change, so
    the check still holds under the lock; only its status may have changed, which every caller
    re-checks on the locked row.
    """
    requester, target = _pair_for(session, principal, request_id, side)
    _lock_pair(session, requester, target)
    row = session.execute(
        select(ConnectionRequest).where(ConnectionRequest.id == request_id).with_for_update()
    ).scalar_one()
    return row


def _update_row(session: Session, request_id: UUID, **values: Any) -> ConnectionRequest:
    """UPDATE the row (the row write that must precede any revoke hook) and return it."""
    return session.execute(
        update(ConnectionRequest)
        .where(ConnectionRequest.id == request_id)
        .values(updated_at=func.now(), **values)
        .returning(ConnectionRequest)
        .execution_options(synchronize_session=False, populate_existing=True)
    ).scalar_one()


def _result(row: ConnectionRequest) -> ResponseResult:
    return ResponseResult(request_id=row.id, status=row.status, tier=row.granted_tier)


def _grant_sync(
    factory: sessionmaker[Session], requester: UUID, target: UUID, tier: ConnectionTier
) -> None:
    """Tx2 of approve/tier change: under the pair lock, grant only if still approved at ``tier``.

    Never raises: the change is committed; a missing grant only denies until reconcile.
    """
    try:
        with factory() as session, session.begin():
            _begin(session)
            _lock_pair(session, requester, target)
            row = _live_row(session, requester, target, for_update=False)
            if row is None or row.status != _APPROVED or row.granted_tier != tier.value:
                return
            profile_id = _profile_id(session, target)
            if profile_id is None:
                return
            anyio.from_thread.run(
                partial(
                    authz.on_connection_granted,
                    profile_id=profile_id,
                    viewer_user_id=requester,
                    tier=tier,
                )
            )
    except Exception as exc:  # committed already; reconcile restores the grant
        logger.error(
            "connections: post-commit grant skipped (%s); fga_reconcile will restore it",
            type(exc).__name__,
        )


def _unblock_sync(factory: sessionmaker[Session], requester: UUID, target: UUID) -> None:
    """Tx2 of unblock: under the pair lock, delete ``blocked`` only if no live block remains."""
    try:
        with factory() as session, session.begin():
            _begin(session)
            _lock_pair(session, requester, target)
            row = _live_row(session, requester, target, for_update=False)
            if row is not None and row.status == _BLOCKED:
                return
            profile_id = _profile_id(session, target)
            if profile_id is None:
                return
            anyio.from_thread.run(
                partial(authz.on_unblocked, profile_id=profile_id, blocked_user_id=requester)
            )
    except Exception as exc:
        logger.error(
            "connections: post-commit unblock skipped (%s); fga_reconcile will apply it",
            type(exc).__name__,
        )


async def _set_tier(
    principal: PrincipalLike,
    request_id: UUID,
    tier: ConnectionTier,
    *,
    from_status: str,
    sessions: SessionFactory | None,
) -> ResponseResult:
    factory = _factory(sessions)

    def tx1() -> tuple[ResponseResult, UUID, UUID]:
        with factory() as session, session.begin():
            _begin(session)
            _check_actor(session, principal)
            row = _locked_row(session, principal, request_id, "target")
            if row.status != from_status:
                raise InvalidState()
            profile_id = _profile_id(session, row.target_user_id)
            if profile_id is None:
                raise ProfileNotSetUp()
            values: dict[str, Any] = {
                "status": _APPROVED,
                "granted_tier": tier.value,
                "responder_delegation_id": principal.delegation_id,
            }
            if row.responded_at is None:
                values["responded_at"] = func.now()
            row = _update_row(session, request_id, **values)
            _hook(
                authz.on_connection_tier_changing,
                profile_id=profile_id,
                viewer_user_id=row.requester_user_id,
                tier=tier,
            )
            return _result(row), row.requester_user_id, row.target_user_id

    result, requester, target = await _run(tx1)
    await anyio.to_thread.run_sync(partial(_grant_sync, factory, requester, target, tier))
    return result


async def approve(
    principal: PrincipalLike,
    request_id: UUID,
    *,
    tier: str | ConnectionTier,
    sessions: SessionFactory | None = None,
) -> ResponseResult:
    """Target approves a pending request at ``tier`` (``connections`` or ``trusted_circle``)."""
    return await _set_tier(
        principal, request_id, _tier(tier), from_status=_PENDING, sessions=sessions
    )


async def change_tier(
    principal: PrincipalLike,
    request_id: UUID,
    *,
    tier: str | ConnectionTier,
    sessions: SessionFactory | None = None,
) -> ResponseResult:
    """Target changes an approved connection's tier; applies on the requester's next check."""
    return await _set_tier(
        principal, request_id, _tier(tier), from_status=_APPROVED, sessions=sessions
    )


async def decline(
    principal: PrincipalLike, request_id: UUID, *, sessions: SessionFactory | None = None
) -> ResponseResult:
    """Target declines a pending request (no tuples exist for a pending request)."""
    factory = _factory(sessions)

    def work() -> ResponseResult:
        with factory() as session, session.begin():
            _begin(session)
            _check_actor(session, principal)
            row = _locked_row(session, principal, request_id, "target")
            if row.status != _PENDING:
                raise InvalidState()
            row = _update_row(
                session,
                request_id,
                status=ConnectionRequestStatus.DECLINED.value,
                responded_at=func.now(),
                responder_delegation_id=principal.delegation_id,
            )
            return _result(row)

    return await _run(work)


async def cancel_request(
    principal: PrincipalLike, request_id: UUID, *, sessions: SessionFactory | None = None
) -> ResponseResult:
    """Requester withdraws their own pending request (status ``cancelled``)."""
    factory = _factory(sessions)

    def work() -> ResponseResult:
        with factory() as session, session.begin():
            _begin(session)
            _check_actor(session, principal)
            row = _locked_row(session, principal, request_id, "requester")
            if row.status != _PENDING:
                raise InvalidState()
            row = _update_row(
                session,
                request_id,
                status=ConnectionRequestStatus.CANCELLED.value,
                responded_at=func.now(),
            )
            return _result(row)

    return await _run(work)


async def revoke(
    principal: PrincipalLike, request_id: UUID, *, sessions: SessionFactory | None = None
) -> ResponseResult:
    """Target revokes an approved connection; denied from the requester's next check."""
    factory = _factory(sessions)

    def work() -> ResponseResult:
        with factory() as session, session.begin():
            _begin(session)
            _check_actor(session, principal)
            row = _locked_row(session, principal, request_id, "target")
            if row.status != _APPROVED:
                raise InvalidState()
            row = _update_row(
                session,
                request_id,
                status=ConnectionRequestStatus.REVOKED.value,
                granted_tier=None,
                responder_delegation_id=principal.delegation_id,
            )
            profile_id = _profile_id(session, row.target_user_id)
            if profile_id is not None:
                _hook(
                    authz.on_connection_revoked,
                    profile_id=profile_id,
                    viewer_user_id=row.requester_user_id,
                )
            return _result(row)

    return await _run(work)


def _cancel_reverse_pending(factory: sessionmaker[Session], blocker: UUID, blocked: UUID) -> None:
    """After a block: cancel the blocker's own pending request TO the blocked user."""
    with factory() as session, session.begin():
        _begin(session)
        _lock_pair(session, blocker, blocked)
        row = _live_row(session, blocker, blocked, for_update=True)
        if row is not None and row.status == _PENDING:
            _update_row(
                session,
                row.id,
                status=ConnectionRequestStatus.CANCELLED.value,
                responded_at=func.now(),
            )


async def block(
    principal: PrincipalLike, request_id: UUID, *, sessions: SessionFactory | None = None
) -> ResponseResult:
    """Target blocks the requester of any request addressed to them (pending, approved or past).

    Updates the live pending/approved row to ``blocked`` (clearing the tier), or inserts a new
    ``blocked`` row when the pair has no live row; the result names the blocked row (which may
    be a new id). Idempotent on an existing block. Then cancels the blocker's own pending
    request to the blocked user.
    """
    factory = _factory(sessions)

    def work() -> tuple[ResponseResult, UUID]:
        with factory() as session, session.begin():
            _begin(session)
            _check_actor(session, principal)
            requester, target = _pair_for(session, principal, request_id, "target")
            _lock_pair(session, requester, target)
            row = _live_row(session, requester, target, for_update=True)
            if row is not None and row.status == _BLOCKED:
                return _result(row), requester
            if row is not None:
                values: dict[str, Any] = {
                    "status": _BLOCKED,
                    "granted_tier": None,
                    "responder_delegation_id": principal.delegation_id,
                }
                if row.responded_at is None:
                    values["responded_at"] = func.now()
                row = _update_row(session, row.id, **values)
            else:
                row = session.execute(
                    insert(ConnectionRequest)
                    .values(
                        requester_user_id=requester,
                        target_user_id=target,
                        status=_BLOCKED,
                        responded_at=func.now(),
                        responder_delegation_id=principal.delegation_id,
                    )
                    .returning(ConnectionRequest)
                ).scalar_one()
            profile_id = _profile_id(session, target)
            if profile_id is not None:
                _hook(authz.on_blocked, profile_id=profile_id, blocked_user_id=requester)
            return _result(row), requester

    result, blocked_user = await _run(work)
    try:
        await _run(partial(_cancel_reverse_pending, factory, principal.user_id, blocked_user))
    except ConnectionsUnavailable:
        logger.warning("connections: could not cancel the blocker's reverse pending request")
    return result


async def unblock(
    principal: PrincipalLike, request_id: UUID, *, sessions: SessionFactory | None = None
) -> ResponseResult:
    """Target lifts a block (``blocked`` -> ``revoked``). Restores no tier: the other person
    must request again."""
    factory = _factory(sessions)

    def tx1() -> tuple[ResponseResult, UUID, UUID]:
        with factory() as session, session.begin():
            _begin(session)
            _check_actor(session, principal)
            row = _locked_row(session, principal, request_id, "target")
            if row.status != _BLOCKED:
                raise InvalidState()
            row = _update_row(
                session,
                request_id,
                status=ConnectionRequestStatus.REVOKED.value,
                responder_delegation_id=principal.delegation_id,
            )
            return _result(row), row.requester_user_id, row.target_user_id

    result, requester, target = await _run(tx1)
    await anyio.to_thread.run_sync(partial(_unblock_sync, factory, requester, target))
    return result


__all__ = [
    "LOOKUP_LIMIT",
    "LOOKUP_WINDOW",
    "REQUEST_LIMIT",
    "REQUEST_WINDOW",
    "BlockedEntry",
    "ConnectionEntry",
    "ConnectionsError",
    "ConnectionsUnavailable",
    "HandleInfo",
    "HandleUnavailable",
    "IncomingRequest",
    "InvalidAvatarUrl",
    "InvalidHandle",
    "InvalidState",
    "InvalidTier",
    "NotAllowed",
    "OutgoingRequest",
    "ProfileNotSetUp",
    "RateLimited",
    "RequestNotFound",
    "RequestResult",
    "ResolvedTarget",
    "ResponseResult",
    "TargetNotFound",
    "approve",
    "block",
    "cancel_request",
    "change_tier",
    "decline",
    "get_my_handle",
    "list_blocked",
    "list_connections",
    "list_incoming",
    "list_outgoing",
    "lookup_and_request",
    "normalize_handle",
    "resolve_handle",
    "set_handle",
    "unblock",
]
