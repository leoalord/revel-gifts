"""Database test fixtures.

DB-backed tests need a disposable PostgreSQL database in ``REVEL_TEST_DATABASE_URL``; they
downgrade it to base and upgrade it to head, destroying its contents. Without the variable they
are skipped. Pure unit tests in this package never connect.
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, pool

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
TEST_DATABASE_URL_ENV = "REVEL_TEST_DATABASE_URL"


@pytest.fixture
def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not url:
        pytest.skip(f"{TEST_DATABASE_URL_ENV} is not set; skipping PostgreSQL-backed tests")
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        yield engine
    finally:
        engine.dispose()


RunAlembic = Callable[..., None]


@pytest.fixture(scope="session")
def run_alembic(db_engine: Engine) -> RunAlembic:
    """Run an alembic ``command`` function against the test database (one transaction)."""

    def run(command: Callable[..., object], *args: object, **kwargs: object) -> None:
        config = Config(str(ALEMBIC_INI))
        with db_engine.begin() as connection:
            config.attributes["connection"] = connection
            command(config, *args, **kwargs)

    return run


@pytest.fixture(scope="session")
def migrated_engine(db_engine: Engine, run_alembic: RunAlembic) -> Engine:
    """Test database reset to base and upgraded to head once per session."""
    from alembic import command

    run_alembic(command.downgrade, "base")
    run_alembic(command.upgrade, "head")
    return db_engine
