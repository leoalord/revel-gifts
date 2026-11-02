"""Backfill and repair OpenFGA tuples from PostgreSQL. Owned by F4 (authorization).

Run explicitly (idempotent; never at app startup)::

    uv run python -m revel.auth.fga_reconcile [--prune] [--min-age MINUTES] [--window MINUTES]

1. Snapshot, in one REPEATABLE READ read-only transaction: ``profiles``, ``agent_delegations``,
   ``connection_requests``, the current (highest ``version``) contribution policy per profile
   and ``questions`` (explicit columns only; never the deferred private fields).
2. Grants (model v2 tuples the rows justify):

   - every profile: ``owner``; every question: ``about`` and ``requester`` (immutable rows);
   - ACTIVE delegations: ``acts_for``, ``delegate`` and ``platform:revel#connected_agent``;
   - APPROVED connection requests: the requester holds ``granted_tier`` (``connection`` or
     ``trusted``) on the TARGET's profile (requests are directional, requester -> target);
   - pairs with no live ``blocked`` row: their ``blocked`` tuple is deleted (an unblock);
   - current policy ``public_opt_in``: both ``public_viewer`` tuples.

   Rows changed within the in-flight ``--window`` (default 2 minutes; ``updated_at``, or the
   policy's ``created_at``) are not granted: their hook just ran in the changing transaction.
3. Revocations, always: DISCONNECTED delegations lose every agent tuple; a directed pair whose
   live row is not ``approved`` loses both tier tuples (an approved one keeps only its tier); a
   ``blocked`` pair (the target blocked the requester) gets ``profile:<target>#blocked@user:
   <requester>`` and loses its tiers; a profile whose current policy has no Public opt-in loses
   ``public_viewer``.
4. Barrier: a short transaction takes EXCLUSIVE locks on ``agent_delegations``,
   ``connection_requests`` and ``contribution_policies`` (waiting for in-flight disconnects,
   connection changes and policy versions to commit, and holding new ones back), re-reads them
   and, while still holding the locks, re-applies the exact current state of every delegation,
   pair and policy that changed since the snapshot. A change committed after step 1 therefore
   cannot be overwritten by steps 2-3, and one that starts later runs its hook after them.

   This relies on the hook-ordering contract (``authz`` module docstring): a writer runs its
   UPDATE/INSERT on these tables BEFORE awaiting a revoke hook, so by the time its tuple is
   deleted it holds ROW EXCLUSIVE and the barrier waits for its commit. EXCLUSIVE (rather than
   SHARE) also waits for writers holding only ROW SHARE (``SELECT ... FOR UPDATE``) who called
   the hook before their UPDATE. A writer holding no lock on these tables at all (none, or only
   an advisory lock) when it runs a revoke hook cannot be seen by any table lock: it MUST follow
   the contract. Plain SELECTs (ACCESS SHARE) are never blocked; inserts into tables with a
   foreign key to these (their ``FOR KEY SHARE`` check takes ROW SHARE) wait like writers.

   Postgres lock queues are FIFO, so app writers queue behind a waiting barrier. The barrier is
   therefore bounded: the lock wait (``lock_timeout``) and every statement under it
   (``statement_timeout``) are ``BARRIER_LOCK_TIMEOUT_SECONDS`` (3s), and the OpenFGA work while
   the locks are held is cut off after ``BARRIER_APPLY_SECONDS`` (3s, the same as
   ``authz.HOOK_TIMEOUT_SECONDS``: no longer than a writer may already wait on an in-flight
   hook), rolling back and releasing the locks. A writer queued behind the barrier waits at
   most about 3s for the lock attempt plus the time the locks are held (bounded by the same
   limits). Failed attempts are retried ``BARRIER_ATTEMPTS`` times with jittered delays.
   If every attempt fails, ``BarrierNotApplied`` is raised and the CLI exits 3 with "re-run
   reconcile": step 2 may have re-granted rows revoked after the snapshot, and only the barrier
   corrects those, so a failed barrier is never reported as success (and ``--prune`` does not
   run).
5. ``--prune``: delete tuples of the relations above (except ``owner``) that the snapshot does
   not justify, only if written at least ``--min-age`` ago (default 10 minutes: covers hook
   writes whose transaction has not committed yet) and still unjustified in a fresh read.

Every step requires configured OpenFGA (revocation must never be skipped). Summary on stderr.
"""

import argparse
import asyncio
import logging
import random
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import anyio.from_thread
import anyio.to_thread
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from revel.auth.authz import (
    ABOUT,
    ACTS_FOR,
    BLOCKED,
    CONNECTED_AGENT,
    CONNECTION,
    DELEGATE,
    PUBLIC_VIEWER,
    REQUESTER,
    TRUSTED,
    TupleHooks,
    blocked_tuple,
    delegation_tuples,
    public_tuples,
    question_tuples,
    reconcile_owner_tuples,
    tier_tuple,
    tier_tuples,
)
from revel.auth.fga import FgaClient, FgaConfigurationError, RelationTuple, create_fga_client
from revel.db.enums import (
    LIVE_CONNECTION_REQUEST_STATUSES,
    ConnectionRequestStatus,
    ConnectionTier,
    DelegationStatus,
)
from revel.db.models import (
    AgentDelegation,
    ConnectionRequest,
    ContributionPolicy,
    Profile,
    Question,
)
from revel.settings import get_settings

DEFAULT_MIN_AGE = timedelta(minutes=10)
DEFAULT_WINDOW = timedelta(minutes=2)
# Barrier bounds (see the module docstring, step 4).
BARRIER_LOCK_TIMEOUT_SECONDS = 3.0
BARRIER_APPLY_SECONDS = 3.0
BARRIER_ATTEMPTS = 4
BARRIER_RETRY_DELAY_SECONDS = (0.5, 2.0)  # jitter range between attempts
EXIT_BARRIER_NOT_APPLIED = 3
# Relations --prune may delete (owner tuples are only ever written).
PRUNABLE = frozenset(
    {ACTS_FOR, DELEGATE, CONNECTED_AGENT, TRUSTED, CONNECTION, BLOCKED, PUBLIC_VIEWER}
    | {ABOUT, REQUESTER}
)

SessionFactory = Callable[[], sessionmaker[Session]]
logger = logging.getLogger(__name__)


class BarrierNotApplied(RuntimeError):
    """The barrier (step 4) failed every attempt: OpenFGA may still hold grants for rows revoked
    after the snapshot. Re-run reconcile."""


_EPOCH = datetime.fromtimestamp(0, UTC)
_LIVE_REQUEST_VALUES = {s.value for s in LIVE_CONNECTION_REQUEST_STATUSES}


@dataclass(frozen=True, slots=True)
class PairState:
    """State of one directed pair (requester -> target): its live row, if any."""

    # pending / approved / blocked, or None when the pair has no live row.
    live_status: str | None
    granted_tier: str | None
    # Latest updated_at over all the pair's rows (in-flight window).
    changed_at: datetime

    def same_access(self, other: "PairState | None") -> bool:
        return other is not None and (self.live_status, self.granted_tier) == (
            other.live_status,
            other.granted_tier,
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    profiles: dict[UUID, UUID]  # user_id -> profile_id
    active: dict[UUID, UUID]  # delegation_id -> user_id
    disconnected: dict[UUID, UUID]  # delegation_id -> user_id
    # Database time of the read (transaction start), for the in-flight window.
    taken_at: datetime = _EPOCH
    delegation_changed: dict[UUID, datetime] = field(default_factory=dict)
    pairs: dict[tuple[UUID, UUID], PairState] = field(default_factory=dict)
    # profile_id -> (public_opt_in, created_at) of the current policy.
    policies: dict[UUID, tuple[bool, datetime]] = field(default_factory=dict)
    questions: dict[UUID, tuple[UUID, UUID]] = field(default_factory=dict)  # -> (profile, req)

    def public_opt_in(self, profile_id: UUID) -> bool:
        # No policy yet: the column default (false).
        return self.policies.get(profile_id, (False, _EPOCH))[0]


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    owner_tuples: int
    agent_tuples: int
    revoked_delegations: int
    revoked_after_recheck: int
    pruned_tuples: int
    kept_recent_or_active: int
    connection_tuples: int = 0
    blocked_pairs: int = 0
    public_profiles: int = 0
    question_tuples: int = 0
    skipped_in_flight: int = 0
    rechecked_changes: int = 0

    def __str__(self) -> str:
        return (
            f"owner tuples ensured: {self.owner_tuples}; agent tuples ensured: "
            f"{self.agent_tuples}; disconnected delegations revoked: {self.revoked_delegations} "
            f"(+{self.revoked_after_recheck} after re-check); connection tuples ensured: "
            f"{self.connection_tuples}; blocked pairs: {self.blocked_pairs}; public profiles: "
            f"{self.public_profiles}; question tuples ensured: {self.question_tuples}; "
            f"skipped in-flight rows: {self.skipped_in_flight}; changes re-applied at the "
            f"barrier: {self.rechecked_changes}; orphan tuples pruned: {self.pruned_tuples} "
            f"(kept {self.kept_recent_or_active} recent or now active)"
        )


# --- Reads ------------------------------------------------------------------------------------


def read_snapshot(session: Session, *, questions: bool = True) -> Snapshot:
    """Every row reconcile needs, read in the caller's transaction (explicit columns only).

    ``questions=False`` skips the (immutable) questions, e.g. while the barrier holds locks.
    """
    taken_at = session.execute(select(func.now())).scalar_one()
    profiles = {
        user_id: profile_id
        for user_id, profile_id in session.execute(select(Profile.user_id, Profile.id))
    }
    active: dict[UUID, UUID] = {}
    disconnected: dict[UUID, UUID] = {}
    changed: dict[UUID, datetime] = {}
    rows = session.execute(
        select(
            AgentDelegation.id,
            AgentDelegation.user_id,
            AgentDelegation.status,
            AgentDelegation.updated_at,
        )
    )
    for delegation_id, user_id, status, updated_at in rows:
        target = active if status == DelegationStatus.ACTIVE.value else disconnected
        target[delegation_id] = user_id
        changed[delegation_id] = updated_at

    live: dict[tuple[UUID, UUID], tuple[str, str | None]] = {}
    latest: dict[tuple[UUID, UUID], datetime] = {}
    requests = session.execute(
        select(
            ConnectionRequest.requester_user_id,
            ConnectionRequest.target_user_id,
            ConnectionRequest.status,
            ConnectionRequest.granted_tier,
            ConnectionRequest.updated_at,
        )
    )
    for requester, target_user, status, tier, updated_at in requests:
        pair = (requester, target_user)
        latest[pair] = max(latest.get(pair, _EPOCH), updated_at)
        if status in _LIVE_REQUEST_VALUES:  # at most one per directed pair (F3's index)
            live[pair] = (status, tier)
    pairs = {
        pair: PairState(*live.get(pair, (None, None)), changed_at=changed_at)
        for pair, changed_at in latest.items()
    }

    # Current policy = highest version per profile (append-only; several rows per profile).
    ranked = select(
        ContributionPolicy.profile_id,
        ContributionPolicy.public_opt_in,
        ContributionPolicy.created_at,
        func.row_number()
        .over(
            partition_by=ContributionPolicy.profile_id,
            order_by=ContributionPolicy.version.desc(),
        )
        .label("rank"),
    ).subquery()
    policies = {
        profile_id: (opt_in, created_at)
        for profile_id, opt_in, created_at in session.execute(
            select(ranked.c.profile_id, ranked.c.public_opt_in, ranked.c.created_at).where(
                ranked.c.rank == 1
            )
        )
    }
    # Explicit columns: private_purpose / escalation_context are never loaded.
    question_rows = (
        session.execute(select(Question.id, Question.profile_id, Question.requester_user_id))
        if questions
        else ()
    )
    question_map = {
        question_id: (profile_id, requester) for question_id, profile_id, requester in question_rows
    }
    return Snapshot(
        profiles=profiles,
        active=active,
        disconnected=disconnected,
        taken_at=taken_at,
        delegation_changed=changed,
        pairs=pairs,
        policies=policies,
        questions=question_map,
    )


def _snapshot(factory: SessionFactory) -> Snapshot:
    with factory()() as session:
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        session.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
        try:
            return read_snapshot(session)
        finally:
            session.rollback()


def _fresh_snapshot(factory: SessionFactory) -> Snapshot:
    """A new read-only read (``--prune``'s re-check), independent of ``_snapshot``."""
    with factory()() as session, session.begin():
        session.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
        return read_snapshot(session)


# --- Desired state ----------------------------------------------------------------------------


def _pair_tuples(snapshot: Snapshot, pair: tuple[UUID, UUID]) -> set[RelationTuple]:
    """Tier/blocked tuples a directed pair justifies (on the target's profile)."""
    requester, target_user = pair
    state = snapshot.pairs.get(pair)
    profile_id = snapshot.profiles.get(target_user)
    if state is None or profile_id is None:
        return set()
    if state.live_status == ConnectionRequestStatus.APPROVED.value and state.granted_tier:
        return {tier_tuple(profile_id, requester, ConnectionTier(state.granted_tier))}
    if state.live_status == ConnectionRequestStatus.BLOCKED.value:
        return {blocked_tuple(profile_id, requester)}
    return set()


def desired_tuples(snapshot: Snapshot) -> set[RelationTuple]:
    """Every PRUNABLE tuple the snapshot justifies (``owner`` is never pruned)."""
    desired: set[RelationTuple] = set()
    for delegation_id, user_id in snapshot.active.items():
        desired.update(delegation_tuples(user_id, delegation_id))
    for pair in snapshot.pairs:
        desired |= _pair_tuples(snapshot, pair)
    for profile_id in snapshot.profiles.values():
        if snapshot.public_opt_in(profile_id):
            desired.update(public_tuples(profile_id))
    for question_id, (profile_id, requester) in snapshot.questions.items():
        desired.update(question_tuples(question_id, profile_id, requester))
    return desired


def _pair_sync(
    snapshot: Snapshot, pair: tuple[UUID, UUID]
) -> tuple[list[RelationTuple], list[RelationTuple]]:
    """``(writes, deletes)`` making one pair's tuples exactly what the snapshot says."""
    requester, target_user = pair
    profile_id = snapshot.profiles.get(target_user)
    if profile_id is None:
        return [], []
    managed = {*tier_tuples(profile_id, requester), blocked_tuple(profile_id, requester)}
    wanted = _pair_tuples(snapshot, pair)
    return sorted(wanted, key=str), sorted(managed - wanted, key=str)


# --- Reconcile --------------------------------------------------------------------------------


async def _apply(
    fga: FgaClient, writes: Iterable[RelationTuple], deletes: Iterable[RelationTuple]
) -> None:
    # Narrowing first where both apply (deletes of tiers before writes is fail-closed).
    deletes, writes = list(deletes), list(writes)
    blocked_writes = [t for t in writes if t.relation == BLOCKED]
    if blocked_writes:
        await fga.write_tuples(blocked_writes)
    if deletes:
        await fga.delete_tuples(deletes)
    rest = [t for t in writes if t.relation != BLOCKED]
    if rest:
        await fga.write_tuples(rest)


async def _grant_and_revoke(
    fga: FgaClient, snapshot: Snapshot, window: timedelta
) -> dict[str, int]:
    cutoff = snapshot.taken_at - window
    counts = dict.fromkeys(("agents", "connections", "blocked", "public", "questions", "skip"), 0)

    # Delegations: grant settled active ones; always revoke disconnected ones.
    writes: list[RelationTuple] = []
    for delegation_id, user_id in snapshot.active.items():
        if snapshot.delegation_changed.get(delegation_id, _EPOCH) < cutoff:
            writes += delegation_tuples(user_id, delegation_id)
        else:
            counts["skip"] += 1
    await fga.write_tuples(writes)
    counts["agents"] = len(writes)
    hooks = TupleHooks(fga)
    for delegation_id, user_id in snapshot.disconnected.items():
        await hooks.on_agent_disconnected(user_id=user_id, delegation_id=delegation_id)

    # Connection pairs: revocations and blocks always; grants and unblocks only when settled.
    grants: list[RelationTuple] = []
    narrowing_writes: list[RelationTuple] = []
    deletes: list[RelationTuple] = []
    for pair, state in snapshot.pairs.items():
        pair_writes, pair_deletes = _pair_sync(snapshot, pair)
        settled = state.changed_at < cutoff
        for t in pair_writes:
            if t.relation == BLOCKED:
                narrowing_writes.append(t)
                counts["blocked"] += 1
            elif settled:
                grants.append(t)
                counts["connections"] += 1
        for t in pair_deletes:
            # Deleting a block widens access: only for settled pairs.
            if t.relation != BLOCKED or settled:
                deletes.append(t)
        if not settled and any(t.relation != BLOCKED for t in pair_writes):
            counts["skip"] += 1
    await _apply(fga, narrowing_writes, deletes)
    await fga.write_tuples(grants)

    # Public opt-in: withdraw always, grant when the current policy is settled.
    public_writes: list[RelationTuple] = []
    public_deletes: list[RelationTuple] = []
    for profile_id in snapshot.profiles.values():
        opt_in, created_at = snapshot.policies.get(profile_id, (False, _EPOCH))
        if not opt_in:
            public_deletes += public_tuples(profile_id)
        elif created_at < cutoff:
            public_writes += public_tuples(profile_id)
            counts["public"] += 1
        else:
            counts["skip"] += 1
    await fga.delete_tuples(public_deletes)
    await fga.write_tuples(public_writes)

    # Questions: about/requester never change.
    question_writes: list[RelationTuple] = []
    for question_id, (profile_id, requester) in snapshot.questions.items():
        question_writes += question_tuples(question_id, profile_id, requester)
    await fga.write_tuples(question_writes)
    counts["questions"] = len(question_writes)
    return counts


def _barrier(factory: SessionFactory, apply: Callable[[Snapshot], None]) -> Snapshot:
    """Lock the mutable tables in EXCLUSIVE mode, read them fresh and run ``apply`` while the
    locks are held. Any raise (lock or statement timeout, ``apply`` timeout) rolls back and
    releases the locks.

    EXCLUSIVE waits for, and holds back, every transaction holding ROW EXCLUSIVE (an UPDATE,
    INSERT or DELETE on the table) or ROW SHARE (``SELECT ... FOR UPDATE/FOR SHARE``, including
    the ``FOR KEY SHARE`` of a foreign-key check from a referencing insert). It still admits
    ACCESS SHARE, so plain SELECTs (token resolution, visibility reads) never wait on it. SHARE
    was not enough: a writer that row-locked with FOR UPDATE and awaited its revoke hook before
    its UPDATE held only ROW SHARE, so the barrier read its row as unchanged and kept the
    snapshot's stale grant. A writer holding no table lock (only an advisory lock, or none)
    before its hook is still invisible here; the hook-ordering contract in ``authz`` (row write
    BEFORE any revoke hook) covers that case. Not read-only (the lock needs it)."""
    timeout_ms = int(BARRIER_LOCK_TIMEOUT_SECONDS * 1000)
    with factory()() as session, session.begin():
        connection = session.connection()
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{timeout_ms}ms'")
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = '{timeout_ms}ms'")
        connection.exec_driver_sql(
            "LOCK TABLE agent_delegations, connection_requests, contribution_policies "
            "IN EXCLUSIVE MODE"
        )
        fresh = read_snapshot(session, questions=False)
        apply(fresh)
        return fresh


def _retry_delay() -> float:
    return random.uniform(*BARRIER_RETRY_DELAY_SECONDS)


async def _recheck(fga: FgaClient, factory: SessionFactory, snapshot: Snapshot) -> tuple[int, int]:
    """Barrier step: re-apply the exact fresh state of everything changed since the snapshot.

    Returns ``(delegations revoked, changes re-applied)``. Raises ``BarrierNotApplied`` after
    ``BARRIER_ATTEMPTS`` failed attempts.
    """
    hooks = TupleHooks(fga)
    result = [0, 0]

    async def sync(fresh: Snapshot) -> None:
        result[:] = [0, 0]
        with anyio.fail_after(BARRIER_APPLY_SECONDS):
            for delegation_id, user_id in snapshot.active.items():
                if delegation_id in fresh.disconnected:
                    await hooks.on_agent_disconnected(user_id=user_id, delegation_id=delegation_id)
                    result[0] += 1
            writes: list[RelationTuple] = []
            deletes: list[RelationTuple] = []
            for pair, state in snapshot.pairs.items():
                if state.same_access(fresh.pairs.get(pair)):
                    continue
                pair_writes, pair_deletes = _pair_sync(fresh, pair)
                writes += pair_writes
                deletes += pair_deletes
                result[1] += 1
            for profile_id in snapshot.profiles.values():
                if snapshot.public_opt_in(profile_id) == fresh.public_opt_in(profile_id):
                    continue
                target = writes if fresh.public_opt_in(profile_id) else deletes
                target += public_tuples(profile_id)
                result[1] += 1
            await _apply(fga, writes, deletes)

    def run_in_thread(fresh: Snapshot) -> None:
        anyio.from_thread.run(sync, fresh)

    last: BaseException | None = None
    for attempt in range(1, BARRIER_ATTEMPTS + 1):
        try:
            await anyio.to_thread.run_sync(_barrier, factory, run_in_thread)
            return result[0], result[1]
        except (OperationalError, TimeoutError) as exc:
            last = exc
            logger.warning(
                "reconcile barrier attempt %d/%d failed (%s)",
                attempt,
                BARRIER_ATTEMPTS,
                type(exc).__name__,
            )
            if attempt < BARRIER_ATTEMPTS:
                await anyio.sleep(_retry_delay())
    raise BarrierNotApplied(
        f"barrier not applied after {BARRIER_ATTEMPTS} attempts "
        f"({type(last).__name__}); re-run reconcile"
    ) from last


async def _prune(
    fga: FgaClient, factory: SessionFactory, snapshot: Snapshot, cutoff: datetime
) -> tuple[int, int]:
    """Delete unjustified PRUNABLE tuples older than ``cutoff``; ``(pruned, kept)``."""
    justified = desired_tuples(snapshot)
    kept = 0
    candidates: list[RelationTuple] = []
    for stored in await fga.read_stored_tuples():
        t = stored.tuple
        if t.relation not in PRUNABLE or t in justified:
            continue
        if stored.written_at > cutoff:
            kept += 1  # possibly an in-flight hook write whose transaction is uncommitted
            continue
        candidates.append(t)
    if not candidates:
        return 0, kept
    now_justified = desired_tuples(await anyio.to_thread.run_sync(_fresh_snapshot, factory))
    stale = [t for t in candidates if t not in now_justified]
    kept += len(candidates) - len(stale)
    await fga.delete_tuples(stale)
    return len(stale), kept


async def reconcile(
    fga: FgaClient,
    factory: SessionFactory,
    *,
    prune: bool = False,
    min_age: timedelta = DEFAULT_MIN_AGE,
    window: timedelta = DEFAULT_WINDOW,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ReconcileSummary:
    if not fga.configured:
        raise FgaConfigurationError("OpenFGA is not configured; cannot reconcile")

    snapshot = await anyio.to_thread.run_sync(_snapshot, factory)
    owners = await reconcile_owner_tuples(fga, list(snapshot.profiles.items()))
    counts = await _grant_and_revoke(fga, snapshot, window)
    turned, rechecked = await _recheck(fga, factory, snapshot)

    pruned = kept = 0
    if prune:
        pruned, kept = await _prune(fga, factory, snapshot, now() - min_age)

    return ReconcileSummary(
        owner_tuples=owners,
        agent_tuples=counts["agents"],
        revoked_delegations=len(snapshot.disconnected),
        revoked_after_recheck=turned,
        pruned_tuples=pruned,
        kept_recent_or_active=kept,
        connection_tuples=counts["connections"],
        blocked_pairs=counts["blocked"],
        public_profiles=counts["public"],
        question_tuples=counts["questions"],
        skipped_in_flight=counts["skip"],
        rechecked_changes=rechecked,
    )


def _default_sessionmaker() -> sessionmaker[Session]:
    from revel.db.session import get_sessionmaker

    return get_sessionmaker()


async def run(
    *,
    prune: bool,
    min_age: timedelta = DEFAULT_MIN_AGE,
    window: timedelta = DEFAULT_WINDOW,
    fga: FgaClient | None = None,
    sessionmaker_provider: SessionFactory = _default_sessionmaker,
) -> ReconcileSummary:
    client = fga if fga is not None else create_fga_client(get_settings())
    try:
        return await reconcile(
            client, sessionmaker_provider, prune=prune, min_age=min_age, window=window
        )
    finally:
        if fga is None:
            await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m revel.auth.fga_reconcile")
    parser.add_argument("--prune", action="store_true", help="delete orphan tuples")
    parser.add_argument(
        "--min-age",
        type=float,
        default=DEFAULT_MIN_AGE.total_seconds() / 60,
        metavar="MINUTES",
        help="only prune tuples written at least this long ago (default 10)",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=DEFAULT_WINDOW.total_seconds() / 60,
        metavar="MINUTES",
        help="do not re-grant rows changed within this in-flight window (default 2)",
    )
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(
            run(
                prune=args.prune,
                min_age=timedelta(minutes=args.min_age),
                window=timedelta(minutes=args.window),
            )
        )
    except FgaConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BarrierNotApplied as exc:
        print(
            f"error: {exc}. OpenFGA may still grant access revoked after the snapshot; "
            "re-run reconcile.",
            file=sys.stderr,
        )
        return EXIT_BARRIER_NOT_APPLIED
    print(summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
