from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from revel.app import create_app
from revel.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    # Ignore any developer .env so tests are deterministic.
    return Settings(_env_file=None, app_env="test", public_base_url="http://localhost:8000")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # Entering the TestClient context runs the app lifespan (MCP session manager).
    with TestClient(app, base_url="http://localhost:8000") as test_client:
        yield test_client
