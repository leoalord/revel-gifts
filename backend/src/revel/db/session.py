"""Engine and session factory. Nothing connects or is created at import time.

Pool sizing targets Cloud SQL db-f1-micro connection limits: keep Cloud Run max instances times
(POOL_SIZE + MAX_OVERFLOW) below the instance's connection limit.
"""

from functools import lru_cache

from pydantic import SecretStr
from sqlalchemy import URL, Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from revel.settings import get_settings

POOL_SIZE = 3
MAX_OVERFLOW = 2


def database_url(value: str | SecretStr | URL | None = None) -> URL:
    """Parse a database URL (plain or ``SecretStr``); defaults to ``settings.database_url``."""
    if value is None:
        value = get_settings().database_url
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return make_url(value)


def create_db_engine(url: str | SecretStr | URL | None = None) -> Engine:
    """Pooled application engine. Does not connect until first use."""
    return create_engine(
        database_url(url),
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
    )


@lru_cache
def get_engine() -> Engine:
    """Process-wide engine from settings, created on first call."""
    return create_db_engine()


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)
