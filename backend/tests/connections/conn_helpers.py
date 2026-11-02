"""Builders shared by the F6 connection tests (synthetic people only)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from authz_fakes import FakeFga
from sqlalchemy.orm import Session

from revel.auth.authz import delegation_tuples, owner_tuple
from revel.auth.identity import Principal
from revel.db.enums import DelegationStatus
from revel.db.models import Agent, AgentDelegation, Handle, Profile, User

# --- Builders ---------------------------------------------------------------------------------


@dataclass
class Person:
    user: User
    delegation: AgentDelegation
    profile: Profile | None
    handle: str

    @property
    def ui(self) -> Principal:
        return Principal(user_id=self.user.id, agent_id=None, delegation_id=None, via="ui")

    @property
    def agent(self) -> Principal:
        return Principal(
            user_id=self.user.id,
            agent_id=self.delegation.agent_id,
            delegation_id=self.delegation.id,
            via="mcp",
        )


def add_agent(session: Session, client_id: str = "client_muse") -> Agent:
    agent = session.query(Agent).filter_by(oauth_client_id=client_id).one_or_none()
    if agent is None:
        agent = Agent(oauth_client_id=client_id, name="Muse")
        session.add(agent)
        session.flush()
    return agent


def add_person(
    session: Session,
    name: str,
    *,
    discoverable: bool = True,
    profile: bool = True,
    handle: bool = True,
    active: bool = True,
) -> Person:
    agent = add_agent(session)
    user = User(workos_user_id=f"user_{name}_{uuid4().hex[:6]}", display_name=name.title())
    session.add(user)
    session.flush()
    delegation = AgentDelegation(
        user_id=user.id,
        agent_id=agent.id,
        status=(DelegationStatus.ACTIVE if active else DelegationStatus.DISCONNECTED).value,
        disconnected_at=None if active else datetime.now(UTC),
    )
    session.add(delegation)
    prof = None
    if profile:
        prof = Profile(user_id=user.id)
        session.add(prof)
    handle_value = f"{name}_demo"
    if handle:
        session.add(
            Handle(
                user_id=user.id,
                handle=handle_value,
                discoverable=discoverable,
                avatar_url=f"https://example.test/{name}.png",
            )
        )
    session.flush()
    session.commit()
    return Person(user=user, delegation=delegation, profile=prof, handle=handle_value)


def seed_fga(fga: FakeFga, *people: Person) -> None:
    """Tuples that F2 (delegations) and F5 (profile owner) would have written."""
    for p in people:
        if p.delegation.status == DelegationStatus.ACTIVE.value:
            fga.tuples |= set(delegation_tuples(p.user.id, p.delegation.id))
        if p.profile is not None:
            fga.tuples.add(owner_tuple(p.user.id, p.profile.id))
