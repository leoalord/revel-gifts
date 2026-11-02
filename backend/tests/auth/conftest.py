"""Fixtures for F2 auth tests (test doubles live in ``fakes.py`` and are re-exported here)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from revel.app import create_app
from revel.auth.runtime import AuthRuntime, reset_auth_runtimes
from revel.settings import Settings

from .authkit import ISSUER, TestAuthKit
from .fakes import InMemoryIdentityRepository, RecordingHooks

PUBLIC_BASE_URL = "http://localhost:8000"
RESOURCE = f"{PUBLIC_BASE_URL}/mcp"
RESOURCE_METADATA_URL = f"{PUBLIC_BASE_URL}/.well-known/oauth-protected-resource/mcp"

__all__ = [
    "ISSUER",
    "PUBLIC_BASE_URL",
    "RESOURCE",
    "RESOURCE_METADATA_URL",
    "InMemoryIdentityRepository",
    "RecordingHooks",
]


@pytest.fixture
def authkit() -> TestAuthKit:
    return TestAuthKit()


@pytest.fixture
def repo() -> InMemoryIdentityRepository:
    return InMemoryIdentityRepository()


@pytest.fixture
def hooks() -> RecordingHooks:
    return RecordingHooks()


@pytest.fixture
def auth_settings() -> Settings:
    return TestAuthKit.settings(public_base_url=PUBLIC_BASE_URL)


@pytest.fixture
def runtime(
    authkit: TestAuthKit,
    auth_settings: Settings,
    repo: InMemoryIdentityRepository,
    hooks: RecordingHooks,
) -> Iterator[AuthRuntime]:
    yield authkit.install(auth_settings, repository=repo, hooks=hooks)
    reset_auth_runtimes()


TokenFactory = Callable[..., str]


@pytest.fixture
def make_token(auth_settings: Settings) -> TokenFactory:
    def factory(**kwargs: Any) -> str:
        return TestAuthKit.token(auth_settings, **kwargs)

    return factory


@pytest.fixture
def auth_client(runtime: AuthRuntime, auth_settings: Settings) -> Iterator[TestClient]:
    app = create_app(auth_settings)
    # Entering the TestClient context runs the app lifespan (MCP session manager).
    with TestClient(app, base_url=PUBLIC_BASE_URL) as client:
        yield client
