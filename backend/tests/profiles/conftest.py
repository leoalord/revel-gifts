"""Fixtures for F5 profile/contribution tests (RG-18).

DB-backed tests need ``REVEL_TEST_DATABASE_URL`` (a disposable database: reset to Alembic base
and upgraded to head once per session, tables truncated per test) and are skipped without it.
OpenFGA is F4's in-memory ``FakeFga`` (model v2) unless a test is marked ``openfga`` (then
``OPENFGA_TEST_API_URL`` must point at a real server). Run DB suites serially.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.orm import Session, sessionmaker

TESTS_DIR = Path(__file__).resolve().parents[1]
# Reuse F4's FakeFga and F2's TestAuthKit (their directories are not packages on sys.path).
for extra in (TESTS_DIR / "authz", TESTS_DIR):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from authz_fakes import FakeFga  # noqa: E402

from revel.auth.authz import configure_authz, delegation_tuples, reset_authz  # noqa: E402
from revel.auth.identity import Principal  # noqa: E402
from revel.db.enums import DelegationStatus  # noqa: E402
from revel.db.models import Agent, AgentDelegation, User  # noqa: E402
from revel.services import profiles  # noqa: E402

DATABASE_URL = os.environ.get("REVEL_TEST_DATABASE_URL")
ALEMBIC_INI = TESTS_DIR.parent / "alembic.ini"
TABLES = (
    "answer_evidence, answers, question_events, assignments, questions, connection_requests, "
    "handles, claim_events, claims, contribution_policies, profiles, agent_delegations, agents, "
    "users"
)


@pytest.fixture(scope="session")
def pf_engine() -> Iterator[Engine]:
    if not DATABASE_URL:
        pytest.skip("REVEL_TEST_DATABASE_URL is not set; skipping PostgreSQL-backed tests")
    from alembic import command
    from alembic.config import Config

    migrate = create_engine(DATABASE_URL, poolclass=pool.NullPool)
    for action, target in ((command.downgrade, "base"), (command.upgrade, "head")):
        config = Config(str(ALEMBIC_INI))
        with migrate.begin() as connection:
            config.attributes["connection"] = connection
            action(config, target)
    migrate.dispose()
    pooled = create_engine(DATABASE_URL, pool_size=5)
    try:
        yield pooled
    finally:
        pooled.dispose()


@pytest.fixture
def factory(pf_engine: Engine) -> Iterator[sessionmaker[Session]]:
    with pf_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    made = sessionmaker(bind=pf_engine, expire_on_commit=False)
    profiles.configure_sessionmaker(lambda: made)
    try:
        yield made
    finally:
        profiles.configure_sessionmaker(None)


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as s:
        yield s


@pytest.fixture
def fga() -> Iterator[FakeFga]:
    fake = FakeFga()
    configure_authz(fake)
    try:
        yield fake
    finally:
        reset_authz()


@dataclass
class Person:
    user: User
    delegation: AgentDelegation

    @property
    def ui(self) -> Principal:
        return Principal(user_id=self.user.id, agent_id=None, delegation_id=None, via="ui")

    @property
    def mcp(self) -> Principal:
        d = self.delegation
        return Principal(user_id=d.user_id, agent_id=d.agent_id, delegation_id=d.id, via="mcp")

    @property
    def api(self) -> Principal:
        d = self.delegation
        return Principal(user_id=d.user_id, agent_id=d.agent_id, delegation_id=d.id, via="api")


@dataclass
class World:
    alex: Person
    sam: Person
    cal: Person
    agent: Agent


@pytest.fixture
def world(session: Session, fga: FakeFga) -> World:
    """Three users, each with an active delegation of the shared client "Muse" (tuples seeded
    as F2's connect hook writes them). No profiles yet."""
    agent = Agent(oauth_client_id=f"client_muse_{uuid4().hex[:6]}", name="Muse")
    session.add(agent)
    session.flush()
    people = {}
    for name in ("alex", "sam", "cal"):
        user = User(workos_user_id=f"user_{name}_{uuid4().hex[:6]}")
        session.add(user)
        session.flush()
        delegation = AgentDelegation(
            user_id=user.id, agent_id=agent.id, status=DelegationStatus.ACTIVE.value
        )
        session.add(delegation)
        session.flush()
        fga.tuples |= set(delegation_tuples(user.id, delegation.id))
        people[name] = Person(user=user, delegation=delegation)
    session.commit()
    return World(agent=agent, **people)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "openfga: integration test against a real OpenFGA server (OPENFGA_TEST_API_URL)"
    )
