"""Fixtures for F6 connection tests against real PostgreSQL (and, where marked, real OpenFGA).

Needs ``REVEL_TEST_DATABASE_URL`` (a disposable database: reset to Alembic base and upgraded to
head once per session, tables truncated per test); skipped without it. ``openfga`` tests also
need ``OPENFGA_TEST_API_URL``. The in-memory OpenFGA fake is F4's ``tests/authz/authz_fakes``.
Run DB suites serially.
"""

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "authz"))

from authz_fakes import FakeFga

from revel.auth.authz import configure_authz, reset_authz
from revel.services import connections

DATABASE_URL = os.environ.get("REVEL_TEST_DATABASE_URL")
OPENFGA_TEST_API_URL = os.environ.get("OPENFGA_TEST_API_URL", "")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "openfga: integration test against a real OpenFGA server (OPENFGA_TEST_API_URL)"
    )


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
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


@pytest.fixture
def sessions(factory: sessionmaker[Session]):
    """The ``sessions=`` provider the service functions take."""
    return lambda: factory


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as s:
        yield s


@pytest.fixture
def fake_fga() -> Iterator[FakeFga]:
    fga = FakeFga()
    configure_authz(fga)
    yield fga
    reset_authz()


@pytest.fixture(autouse=True)
def _reset_limits() -> Iterator[None]:
    connections.lookup_limiter.reset()
    yield
    connections.lookup_limiter.reset()
