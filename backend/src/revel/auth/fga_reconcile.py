"""Backfill and repair OpenFGA tuples from PostgreSQL. Owned by F4 (authorization).

Run explicitly (idempotent; never at app startup)::

    uv run python -m revel.auth.fga_reconcile [--prune]

- Every profile: ``user:<owner> owner profile:<id>``.
- Every ACTIVE delegation: ``acts_for``, plus ``owner_agent`` on the user's profile if any.
- Every DISCONNECTED delegation: its ``acts_for`` and ``owner_agent`` tuples are deleted
  (revocation; requires configured OpenFGA, like every other step).
- ``--prune``: also deletes ``acts_for`` / ``owner_agent`` tuples whose ``agent:<id>`` is not an
  active delegation in PostgreSQL (orphans from a failed commit, deleted test data, ...).

Reads ``profiles`` and ``agent_delegations`` in one read-only transaction. Prints a
summary to stderr.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import anyio.to_thread
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from revel.auth.authz import (
    ACTS_FOR,
    OWNER_AGENT,
    TupleHooks,
    reconcile_agent_tuples,
    reconcile_owner_tuples,
)
from revel.auth.fga import FgaClient, FgaConfigurationError, RelationTuple, create_fga_client
from revel.db.enums import DelegationStatus
from revel.db.models import AgentDelegation, Profile
from revel.settings import get_settings


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
    pruned_tuples: int

    def __str__(self) -> str:
        return (
            f"owner tuples ensured: {self.owner_tuples}; agent tuples ensured: "
            f"{self.agent_tuples}; disconnected delegations revoked: "
            f"{self.revoked_delegations}; orphan tuples pruned: {self.pruned_tuples}"
        )


def read_snapshot(session: Session) -> Snapshot:
    session.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
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


async def reconcile(fga: FgaClient, snapshot: Snapshot, *, prune: bool = False) -> ReconcileSummary:
    if not fga.configured:
        raise FgaConfigurationError("OpenFGA is not configured; cannot reconcile")

    owners = await reconcile_owner_tuples(fga, [(u, p) for u, p in snapshot.profiles.items()])
    agent_writes = 0
    profileless = []
    triples = []
    for delegation_id, user_id in snapshot.active.items():
        profile_id = snapshot.profiles.get(user_id)
        if profile_id is None:
            profileless.append(RelationTuple(f"user:{user_id}", ACTS_FOR, f"agent:{delegation_id}"))
        else:
            triples.append((user_id, delegation_id, profile_id))
    agent_writes += await reconcile_agent_tuples(fga, triples)
    await fga.write_tuples(profileless)
    agent_writes += len(profileless)

    async def profile_of(user_id: UUID) -> UUID | None:
        return snapshot.profiles.get(user_id)

    hooks = TupleHooks(fga, profile_of)
    for delegation_id, user_id in snapshot.disconnected.items():
        await hooks.on_agent_disconnected(user_id=user_id, delegation_id=delegation_id)

    pruned = 0
    if prune:
        stale: list[RelationTuple] = []
        for t in await fga.read_tuples():
            if t.relation == ACTS_FOR:
                delegation = _delegation_of(t.object)
            elif t.relation == OWNER_AGENT:
                delegation = _delegation_of(t.user)
            else:
                continue
            if delegation is None or delegation not in snapshot.active:
                stale.append(t)
        await fga.delete_tuples(stale)
        pruned = len(stale)

    return ReconcileSummary(owners, agent_writes, len(snapshot.disconnected), pruned)


def _default_sessionmaker() -> sessionmaker[Session]:
    from revel.db.session import get_sessionmaker

    return get_sessionmaker()


async def run(
    *,
    prune: bool,
    fga: FgaClient | None = None,
    sessionmaker_provider: Callable[[], sessionmaker[Session]] = _default_sessionmaker,
) -> ReconcileSummary:
    def read() -> Snapshot:
        with sessionmaker_provider()() as session, session.begin():
            return read_snapshot(session)

    client = fga if fga is not None else create_fga_client(get_settings())
    try:
        if not client.configured:
            raise FgaConfigurationError("OpenFGA is not configured; cannot reconcile")
        snapshot = await anyio.to_thread.run_sync(read)
        return await reconcile(client, snapshot, prune=prune)
    finally:
        if fga is None:
            await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m revel.auth.fga_reconcile")
    parser.add_argument("--prune", action="store_true", help="delete orphan agent tuples")
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(run(prune=args.prune))
    except FgaConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
