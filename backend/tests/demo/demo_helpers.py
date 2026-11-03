"""Builders shared by the F10 demo reset tests (synthetic people only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from authz_fakes import FakeFga
from sqlalchemy.orm import Session

from revel.auth.authz import delegation_tuples, owner_tuple
from revel.auth.identity import Principal
from revel.db.enums import ConnectionTier, DelegationStatus
from revel.db.models import (
    Agent,
    AgentDelegation,
    AgentDelegationSession,
    ContributionPolicy,
    Handle,
    Profile,
    User,
)
from revel.services import connections


@dataclass
class Person:
    user: User
    delegation: AgentDelegation
    profile: Profile | None
    handle: str

    @property
    def ui(self) -> Principal:
        return Principal(user_id=self.user.id, agent_id=None, delegation_id=None, via="ui")


@dataclass
class World:
    leo: Person
    sam: Person
    jordan: Person
    cal: Person  # non-demo user
    agent: Agent


def add_person(
    session: Session, fga: FakeFga, agent: Agent, handle: str, *, profile: bool = True
) -> Person:
    user = User(workos_user_id=f"user_{handle}_{uuid4().hex[:6]}", display_name="Synthetic")
    session.add(user)
    session.flush()
    delegation = AgentDelegation(
        user_id=user.id,
        agent_id=agent.id,
        status=DelegationStatus.ACTIVE.value,
        authorized_sid=f"session_{uuid4().hex[:8]}",
    )
    session.add(delegation)
    session.flush()
    session.add(AgentDelegationSession(delegation_id=delegation.id, sid=delegation.authorized_sid))
    prof = None
    if profile:
        prof = Profile(user_id=user.id)
        session.add(prof)
    session.add(Handle(user_id=user.id, handle=handle))
    session.flush()
    fga.tuples |= set(delegation_tuples(user.id, delegation.id))
    if prof is not None:
        fga.tuples.add(owner_tuple(user.id, prof.id))
    return Person(user=user, delegation=delegation, profile=prof, handle=handle)


def add_policy(session: Session, person: Person, version: int) -> None:
    assert person.profile is not None
    session.add(
        ContributionPolicy(
            profile_id=person.profile.id,
            version=version,
            approved_by_user_id=person.user.id,
            approved_at=datetime.now(UTC),
        )
    )
    session.flush()


async def connect(
    sessions, requester: Person, target: Person, tier: ConnectionTier | None
) -> object:
    """requester asks target; target approves at ``tier`` (None leaves it pending)."""
    result = await connections.lookup_and_request(
        requester.ui, handle=target.handle, sessions=sessions
    )
    if tier is not None:
        await connections.approve(target.ui, result.request.id, tier=tier, sessions=sessions)
    return result.request.id
