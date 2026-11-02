"""Shared fixtures and builders for F7 question tests (re-exported by conftest.py).

DB-backed tests need ``REVEL_TEST_DATABASE_URL`` (a disposable database: reset to Alembic base
and upgraded to head once per session, tables truncated per test); they are skipped without it.
OpenFGA: when ``OPENFGA_TEST_API_URL`` is reachable the ``fga`` fixture is a REAL OpenFGA client
on a store bootstrapped with the repository model (once per session); otherwise it falls back to
F4's in-memory model-v2 fake (``tests/authz/authz_fakes.py``) and ``FGA_KIND`` says so. Run DB
suites serially. No network calls to Muse Spark: tests inject an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "authz"))

from authz_fakes import FakeFga

from revel.auth.authz import (
    blocked_tuple,
    configure_authz,
    delegation_tuples,
    owner_tuple,
    reset_authz,
    tier_tuple,
)
from revel.auth.fga import RelationTuple
from revel.auth.identity import Principal
from revel.db.enums import Audience, ConnectionTier, DelegationStatus
from revel.db.models import (
    Agent,
    AgentDelegation,
    Claim,
    ContributionPolicy,
    Handle,
    Profile,
    User,
)
from revel.services.muse_spark import MuseSparkClient
from revel.services.questions import QuestionDeps, reset_question_deps
from revel.settings import Settings

DATABASE_URL = os.environ.get("REVEL_TEST_DATABASE_URL")
OPENFGA_TEST_API_URL = os.environ.get("OPENFGA_TEST_API_URL", "")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

requires_db = pytest.mark.skipif(not DATABASE_URL, reason="REVEL_TEST_DATABASE_URL not set")


def _openfga_reachable() -> bool:
    if not OPENFGA_TEST_API_URL:
        return False
    try:
        return httpx.get(f"{OPENFGA_TEST_API_URL}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


REAL_OPENFGA = _openfga_reachable()
FGA_KIND = "openfga" if REAL_OPENFGA else "fake"


# --- Database ---------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    if not DATABASE_URL:
        pytest.skip("REVEL_TEST_DATABASE_URL not set")
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
def factory(engine: Engine) -> sessionmaker[Session]:
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE answer_evidence, answers, question_events, assignments, questions, "
                "connection_requests, handles, claim_events, claims, contribution_policies, "
                "profiles, agent_delegations, agents, users CASCADE"
            )
        )
    return sessionmaker(bind=engine, expire_on_commit=False)


# --- OpenFGA ----------------------------------------------------------------------------------

_store: dict[str, str] = {}


@pytest.fixture
async def fga() -> AsyncIterator[Any]:
    """Real OpenFGA (when reachable) or F4's fake, installed with ``configure_authz``."""
    reset_authz()
    reset_question_deps()
    if not REAL_OPENFGA:
        fake = FakeFga()
        configure_authz(fake)
        yield fake
        reset_authz()
        return
    from revel.auth import fga_bootstrap
    from revel.auth.fga import create_fga_client

    base = Settings(_env_file=None, openfga_api_url=OPENFGA_TEST_API_URL)
    if not _store:
        boot = await fga_bootstrap.bootstrap(base, store_name=f"revel-f7-test-{uuid4()}")
        _store["store"] = boot.store_id
        _store["model"] = boot.authorization_model_id
    settings = base.model_copy(
        update={
            "openfga_store_id": _store["store"],
            "openfga_authorization_model_id": _store["model"],
        }
    )
    client = create_fga_client(settings)
    configure_authz(client)
    try:
        yield client
    finally:
        reset_authz()
        await client.close()


# --- Fake Muse Spark --------------------------------------------------------------------------

TEST_KEY = SecretStr("test-key-not-real")


def completion(content: str | None) -> dict[str, Any]:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content, "reasoning_content": "..."}}
        ]
    }


@dataclass
class FakeMuse:
    """Records requests; responds with ``content`` / ``status`` / ``delay`` / ``exc``."""

    content: str | None = None
    status: int = 200
    delay: float = 0.0
    exc: Exception | None = None
    requests: list[httpx.Request] | None = None

    def client(self, *, timeout: float = 2.0, provider: str = "fireworks") -> MuseSparkClient:
        self.requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            assert self.requests is not None
            self.requests.append(request)
            if self.delay:
                await anyio.sleep(self.delay)
            if self.exc is not None:
                raise self.exc
            return httpx.Response(self.status, json=completion(self.content))

        return MuseSparkClient(
            api_key=TEST_KEY,
            provider=provider,
            timeout_seconds=timeout,
            transport=httpx.MockTransport(handler),
        )


def proposal(question: str, ids: list[str]) -> str:
    return json.dumps({"minimized_question": question, "respondent_ids": ids})


# --- World ------------------------------------------------------------------------------------

PURPOSE = {
    "occasion": "30th birthday",
    "occasion_date": "March 14",
    "budget": "$80",
    "candidate_ideas": ["peony bouquet", "ceramics class voucher"],
    "notes": "She mentioned wanting to learn pottery at the lake house",
}
REQUESTED = "What flowers does Chelsea like?"


def agent_principal(delegation: AgentDelegation, via: str = "mcp") -> Principal:
    return Principal(
        user_id=delegation.user_id,
        agent_id=delegation.agent_id,
        delegation_id=delegation.id,
        via=via,  # type: ignore[arg-type]
    )


@dataclass
class World:
    owner: User
    profile: Profile
    owner_d1: AgentDelegation
    owner_d2: AgentDelegation
    owner_old: AgentDelegation  # disconnected
    requester: User
    requester_d: AgentDelegation
    stranger_d: AgentDelegation
    blocked_d: AgentDelegation
    public_d: AgentDelegation
    connections_claim: UUID
    only_me_claim: UUID

    def deps(
        self, factory: sessionmaker[Session], muse: MuseSparkClient | None = None, **kw: Any
    ) -> QuestionDeps:
        return QuestionDeps(sessionmaker=factory, muse=muse, **kw)


def _user(session: Session, name: str, handle: str | None) -> User:
    user = User(workos_user_id=f"user_{handle or name}_{uuid4().hex[:6]}", display_name=name)
    session.add(user)
    session.flush()
    if handle:
        session.add(Handle(user_id=user.id, handle=handle))
    return user


def _agent(session: Session, client_id: str, name: str) -> Agent:
    agent = Agent(oauth_client_id=client_id, name=name)
    session.add(agent)
    session.flush()
    return agent


def _delegation(session: Session, user: User, agent: Agent, active: bool = True) -> AgentDelegation:
    d = AgentDelegation(
        user_id=user.id,
        agent_id=agent.id,
        status=(DelegationStatus.ACTIVE if active else DelegationStatus.DISCONNECTED).value,
        disconnected_at=None if active else datetime.now(UTC),
    )
    session.add(d)
    session.flush()
    return d


def _claim(session: Session, profile: Profile, statement: str, audience: Audience) -> UUID:
    claim = Claim(
        profile_id=profile.id,
        category="likes",
        statement=statement,
        audience=audience.value,
        evidence_type="explicit",
        status="active",
        contributor_user_id=profile.user_id,
        contributed_via="ui",
    )
    session.add(claim)
    session.flush()
    return claim.id


async def build_world(factory: sessionmaker[Session], fga: Any) -> World:
    """Chelsea (owner, two active assistants + one disconnected) and Leo (Connections) plus a
    stranger, a blocked former Trusted Circle member and a Public-only viewer."""
    with factory() as s:
        muse = _agent(s, "client_muse", "Muse")
        claude = _agent(s, "client_claude", "Claude")
        owner = _user(s, "Chelsea Example", "chelsea")
        requester = _user(s, "Leo Example", "leo")
        stranger = _user(s, "Sam Stranger", "sam")
        blocked = _user(s, "Bea Blocked", "bea")
        public = _user(s, "Pat Public", "pat")
        profile = Profile(user_id=owner.id)
        s.add(profile)
        s.flush()
        for user in (requester, stranger):
            s.add(Profile(user_id=user.id))
        world = World(
            owner=owner,
            profile=profile,
            owner_d1=_delegation(s, owner, muse),
            owner_d2=_delegation(s, owner, claude),
            owner_old=_delegation(s, owner, _agent(s, "client_old", "OldBot"), active=False),
            requester=requester,
            requester_d=_delegation(s, requester, muse),
            stranger_d=_delegation(s, stranger, muse),
            blocked_d=_delegation(s, blocked, muse),
            public_d=_delegation(s, public, muse),
            connections_claim=_claim(s, profile, "Likes peonies and tulips", Audience.CONNECTIONS),
            only_me_claim=_claim(s, profile, "Private only-me fact", Audience.ONLY_ME),
        )
        s.commit()
    tuples: list[RelationTuple] = [owner_tuple(owner.id, profile.id)]
    for d in (
        world.owner_d1,
        world.owner_d2,
        world.requester_d,
        world.stranger_d,
        world.blocked_d,
        world.public_d,
    ):
        tuples.extend(delegation_tuples(d.user_id, d.id))
    tuples.append(tier_tuple(profile.id, requester.id, ConnectionTier.CONNECTIONS))
    tuples.append(tier_tuple(profile.id, blocked.id, ConnectionTier.TRUSTED_CIRCLE))
    tuples.append(blocked_tuple(profile.id, blocked.id))
    await fga.write_tuples(tuples)
    return world


def set_policy(factory: sessionmaker[Session], world: World, *, questions_enabled: bool) -> None:
    with factory() as s:
        s.add(
            ContributionPolicy(
                profile_id=world.profile.id,
                version=1,
                approved_by_user_id=world.owner.id,
                questions_enabled=questions_enabled,
            )
        )
        s.commit()


@pytest.fixture
def world_builder(factory: sessionmaker[Session], fga: Any) -> Callable[[], Any]:
    async def build() -> World:
        return await build_world(factory, fga)

    return build


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
