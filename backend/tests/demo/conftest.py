"""Fixtures for the F10 demo reset tests (RG-45) against real PostgreSQL and F4's FakeFga.

Needs ``REVEL_TEST_DATABASE_URL`` (a disposable database: reset to Alembic base and upgraded to
head once per session, tables truncated per test); skipped without it. Synthetic people only.
Run DB suites serially.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.orm import Session, sessionmaker

TESTS_DIR = Path(__file__).resolve().parents[1]
for extra in (TESTS_DIR / "authz", TESTS_DIR / "demo"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from authz_fakes import FakeFga  # noqa: E402
from demo_helpers import World, add_person, add_policy, connect  # noqa: E402

from revel.auth.authz import (  # noqa: E402
    configure_authz,
    reset_authz,
)
from revel.db.enums import ConnectionTier  # noqa: E402
from revel.db.models import (  # noqa: E402
    Agent,
)
from revel.services import connections, profiles  # noqa: E402
from revel.settings import Settings  # noqa: E402

DATABASE_URL = os.environ.get("REVEL_TEST_DATABASE_URL")
ALEMBIC_INI = TESTS_DIR.parent / "alembic.ini"
TABLES = (
    "answer_evidence, answers, question_events, assignments, questions, connection_requests, "
    "handles, claim_events, claims, contribution_policies, profiles, agent_delegation_sessions, "
    "agent_delegations, agents, users"
)
DEV_URL = "https://api.revel.gifts"


@pytest.fixture(scope="session")
def demo_engine() -> Iterator[Engine]:
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
    pooled = create_engine(DATABASE_URL, pool_size=10, max_overflow=5)
    try:
        yield pooled
    finally:
        pooled.dispose()


@pytest.fixture
def factory(demo_engine: Engine) -> Iterator[sessionmaker[Session]]:
    with demo_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    made = sessionmaker(bind=demo_engine, expire_on_commit=False)
    profiles.configure_sessionmaker(lambda: made)
    connections.lookup_limiter.reset()
    try:
        yield made
    finally:
        profiles.configure_sessionmaker(None)


@pytest.fixture
def sessions(factory: sessionmaker[Session]):
    return lambda: factory


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


@pytest.fixture
def dev_settings() -> Settings:
    return Settings(_env_file=None, app_env="test", public_base_url=DEV_URL)


@pytest.fixture
async def world(session: Session, fga: FakeFga, sessions) -> World:
    """Leo (recipient, policy v1), Sam and Jordan (demo) and Cal (non-demo), each with an
    active delegation (authorized_sid + a delegation session). Baseline: Jordan approved by Leo
    at trusted_circle."""
    agent = Agent(oauth_client_id=f"client_muse_{uuid4().hex[:6]}", name="Muse")
    session.add(agent)
    session.flush()
    leo = add_person(session, fga, agent, "leo_walker")
    sam = add_person(session, fga, agent, "sam_demo")
    jordan = add_person(session, fga, agent, "jordan_demo")
    cal = add_person(session, fga, agent, "cal_other")
    add_policy(session, leo, 1)
    session.commit()
    await connect(sessions, jordan, leo, ConnectionTier.TRUSTED_CIRCLE)
    return World(leo=leo, sam=sam, jordan=jordan, cal=cal, agent=agent)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
