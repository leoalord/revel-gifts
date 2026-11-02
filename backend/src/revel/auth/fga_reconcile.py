"""Backfill and repair OpenFGA tuples from PostgreSQL. Owned by F4 (authorization).

Run explicitly (idempotent; never at app startup)::

    uv run python -m revel.auth.fga_reconcile [--prune] [--min-age MINUTES]

1. Snapshot ``profiles`` and ``agent_delegations`` in one REPEATABLE READ, read-only transaction.
2. Every profile: ``profile:<p>#owner@user:<u>``. Every ACTIVE delegation: ``acts_for`` and
   ``user:<u>#delegate@agent:<d>`` (owner_agent is computed from them).
3. Every DISCONNECTED delegation: its ``acts_for`` and ``delegate`` tuples are deleted.
4. Re-check, in a fresh transaction, the delegations step 2 treated as active; any that were
   disconnected meanwhile are revoked as in step 3.
5. ``--prune``: delete ``acts_for`` / ``delegate`` tuples whose ``agent:<id>`` is not an active
   delegation. A candidate is kept if a fresh transaction shows it active (created after the
   snapshot), or if its OpenFGA write time is younger than ``--min-age`` (default 10 minutes:
   covers hook writes whose identity transaction has not committed yet).

Every step requires configured OpenFGA (revocation must never be skipped). Summary on stderr.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import anyio.to_thread
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from revel.auth.authz import (
    ACTS_FOR,
    DELEGATE,
    TupleHooks,
    reconcile_agent_tuples,
    reconcile_owner_tuples,
)
from revel.auth.fga import FgaClient, FgaConfigurationError, RelationTuple, create_fga_client
from revel.db.enums import DelegationStatus
from revel.db.models import AgentDelegation, Profile
from revel.settings import get_settings

DEFAULT_MIN_AGE = timedelta(minutes=10)

SessionFactory = Callable[[], sessionmaker[Session]]


@dataclass(frozen=True, slots=True)
class Snapshot:
    profiles: dict[UUID, UUID]  # user_id -> profile_id
    active: dict[UUID, UUID]  # delegation_id -> user_id
    disconnected: dict[UUID, UUID]  # delegation_id -> user_id


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    owner_tuples: int
    agent_tuples: int
    revoked_delegations: int
    revoked_after_recheck: int
    pruned_tuples: int
    kept_recent_or_active: int

    def __str__(self) -> str:
        return (
            f"owner tuples ensured: {self.owner_tuples}; agent tuples ensured: "
            f"{self.agent_tuples}; disconnected delegations revoked: {self.revoked_delegations} "
            f"(+{self.revoked_after_recheck} after re-check); orphan tuples pruned: "
            f"{self.pruned_tuples} (kept {self.kept_recent_or_active} recent or now active)"
        )


def _statuses(factory: SessionFactory, ids: Collection[UUID]) -> dict[UUID, tuple[UUID, str]]:
    """Fresh read-only transaction: ``delegation_id -> (user_id, status)`` for ``ids``."""
    if not ids:
        return {}
    with factory()() as session, session.begin():
        session.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
        rows = session.execute(
            select(AgentDelegation.id, AgentDelegation.user_id, AgentDelegation.status).where(
                AgentDelegation.id.in_(list(ids))
            )
        )
        return {d: (u, s) for d, u, s in rows}


def _snapshot(factory: SessionFactory) -> Snapshot:
    with factory()() as session:
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        session.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
        try:
            return read_snapshot(session)
        finally:
            session.rollback()


def read_snapshot(session: Session) -> Snapshot:
    """Profiles and delegations, read in the caller's (REPEATABLE READ) transaction."""
    profiles = {
        user_id: profile_id
        for user_id, profile_id in session.execute(select(Profile.user_id, Profile.id))
    }
    active: dict[UUID, UUID] = {}
    disconnected: dict[UUID, UUID] = {}
    rows = session.execute(
        select(AgentDelegation.id, AgentDelegation.user_id, AgentDelegation.status)
    )
    for delegation_id, user_id, status in rows:
        target = active if status == DelegationStatus.ACTIVE.value else disconnected
        target[delegation_id] = user_id
    return Snapshot(profiles, active, disconnected)


def _delegation_of(agent_object: str) -> UUID | None:
    kind, _, raw = agent_object.partition(":")
    if kind != "agent":
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


async def reconcile(
    fga: FgaClient,
    factory: SessionFactory,
    *,
    prune: bool = False,
    min_age: timedelta = DEFAULT_MIN_AGE,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ReconcileSummary:
    if not fga.configured:
        raise FgaConfigurationError("OpenFGA is not configured; cannot reconcile")

    snapshot = await anyio.to_thread.run_sync(_snapshot, factory)
    owners = await reconcile_owner_tuples(fga, list(snapshot.profiles.items()))
    agents = await reconcile_agent_tuples(
        fga, [(user_id, d) for d, user_id in snapshot.active.items()]
    )

    hooks = TupleHooks(fga)
    for delegation_id, user_id in snapshot.disconnected.items():
        await hooks.on_agent_disconnected(user_id=user_id, delegation_id=delegation_id)

    # A delegation disconnected after the snapshot may just have been re-granted above.
    current = await anyio.to_thread.run_sync(_statuses, factory, list(snapshot.active))
    turned = {
        d: user_id
        for d, (user_id, status) in current.items()
        if status != DelegationStatus.ACTIVE.value
    }
    for delegation_id, user_id in turned.items():
        await hooks.on_agent_disconnected(user_id=user_id, delegation_id=delegation_id)

    pruned = kept = 0
    if prune:
        cutoff = now() - min_age
        candidates: dict[RelationTuple, UUID | None] = {}
        for stored in await fga.read_stored_tuples():
            t = stored.tuple
            if t.relation == ACTS_FOR:
                delegation = _delegation_of(t.object)
            elif t.relation == DELEGATE:
                delegation = _delegation_of(t.user)
            else:
                continue
            if (
                delegation is not None
                and delegation in snapshot.active
                and delegation not in turned
            ):
                continue
            if stored.written_at > cutoff:
                kept += 1  # possibly an in-flight hook write whose transaction is uncommitted
                continue
            candidates[t] = delegation
        ids = {d for d in candidates.values() if d is not None}
        fresh = await anyio.to_thread.run_sync(_statuses, factory, ids)
        stale = [
            t
            for t, d in candidates.items()
            if d is None or fresh.get(d, (None, ""))[1] != DelegationStatus.ACTIVE.value
        ]
        kept += len(candidates) - len(stale)
        await fga.delete_tuples(stale)
        pruned = len(stale)

    return ReconcileSummary(
        owner_tuples=owners,
        agent_tuples=agents,
        revoked_delegations=len(snapshot.disconnected),
        revoked_after_recheck=len(turned),
        pruned_tuples=pruned,
        kept_recent_or_active=kept,
    )


def _default_sessionmaker() -> sessionmaker[Session]:
    from revel.db.session import get_sessionmaker

    return get_sessionmaker()


async def run(
    *,
    prune: bool,
    min_age: timedelta = DEFAULT_MIN_AGE,
    fga: FgaClient | None = None,
    sessionmaker_provider: SessionFactory = _default_sessionmaker,
) -> ReconcileSummary:
    client = fga if fga is not None else create_fga_client(get_settings())
    try:
        return await reconcile(client, sessionmaker_provider, prune=prune, min_age=min_age)
    finally:
        if fga is None:
            await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m revel.auth.fga_reconcile")
    parser.add_argument("--prune", action="store_true", help="delete orphan agent tuples")
    parser.add_argument(
        "--min-age",
        type=float,
        default=DEFAULT_MIN_AGE.total_seconds() / 60,
        metavar="MINUTES",
        help="only prune tuples written at least this long ago (default 10)",
    )
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(run(prune=args.prune, min_age=timedelta(minutes=args.min_age)))
    except FgaConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
