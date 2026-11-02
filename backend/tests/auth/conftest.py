"""Fixtures for F2 auth tests: mocked AuthKit, in-memory repository, recording hooks."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from revel.app import create_app
from revel.auth.repository import AgentRecord, DelegationRecord, OnCreated, UserRecord
from revel.auth.runtime import AuthRuntime, reset_auth_runtimes
from revel.mcp import identity_tools
from revel.settings import Settings

from .authkit import ISSUER, TestAuthKit

PUBLIC_BASE_URL = "http://localhost:8000"
RESOURCE = f"{PUBLIC_BASE_URL}/mcp"
RESOURCE_METADATA_URL = f"{PUBLIC_BASE_URL}/.well-known/oauth-protected-resource/mcp"

__all__ = ["ISSUER", "PUBLIC_BASE_URL", "RESOURCE", "RESOURCE_METADATA_URL"]


class InMemoryIdentityRepository:
    """Test double with the same insert-if-absent and rollback-on-hook-failure semantics."""

    def __init__(self) -> None:
        self.users: dict[UUID, UserRecord] = {}
        self.agents: dict[UUID, AgentRecord] = {}
        self.delegations: dict[UUID, DelegationRecord] = {}
        self._lock = anyio.Lock()

    async def get_user_by_workos_id(self, workos_user_id: str) -> UserRecord | None:
        return next((u for u in self.users.values() if u.workos_user_id == workos_user_id), None)

    async def get_user(self, user_id: UUID) -> UserRecord | None:
        return self.users.get(user_id)

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        return self.agents.get(agent_id)

    async def ensure_user(
        self,
        workos_user_id: str,
        *,
        email: str | None,
        display_name: str | None,
        on_created: OnCreated[UserRecord] | None = None,
    ) -> tuple[UserRecord, bool]:
        async with self._lock:
            existing = await self.get_user_by_workos_id(workos_user_id)
            if existing is not None:
                return existing, False
            record = UserRecord(uuid4(), workos_user_id, email, display_name)
            self.users[record.id] = record
            try:
                if on_created is not None:
                    await on_created(record)
            except BaseException:
                del self.users[record.id]
                raise
            return record, True

    async def ensure_agent(self, oauth_client_id: str) -> tuple[AgentRecord, bool]:
        async with self._lock:
            existing = next(
                (a for a in self.agents.values() if a.oauth_client_id == oauth_client_id), None
            )
            if existing is not None:
                return existing, False
            record = AgentRecord(uuid4(), oauth_client_id, None)
            self.agents[record.id] = record
            return record, True

    async def ensure_delegation(
        self,
        user_id: UUID,
        agent_id: UUID,
        *,
        on_created: OnCreated[DelegationRecord] | None = None,
    ) -> tuple[DelegationRecord, bool]:
        async with self._lock:
            existing = next(
                (
                    d
                    for d in self.delegations.values()
                    if d.user_id == user_id and d.agent_id == agent_id
                ),
                None,
            )
            if existing is not None:
                return existing, False
            record = DelegationRecord(uuid4(), user_id, agent_id, "active")
            self.delegations[record.id] = record
            try:
                if on_created is not None:
                    await on_created(record)
            except BaseException:
                del self.delegations[record.id]
                raise
            return record, True

    def disconnect(self, delegation_id: UUID) -> None:
        """Simulates #14's disconnect flow."""
        self.delegations[delegation_id] = replace(
            self.delegations[delegation_id], status="disconnected"
        )


class RecordingHooks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_next_user_hook = False

    async def on_user_created(self, *, user_id: UUID) -> None:
        if self.fail_next_user_hook:
            self.fail_next_user_hook = False
            raise RuntimeError("authz unavailable")
        self.calls.append(("on_user_created", {"user_id": user_id}))

    async def on_agent_delegated(
        self, *, user_id: UUID, agent_id: UUID, delegation_id: UUID
    ) -> None:
        self.calls.append(
            (
                "on_agent_delegated",
                {"user_id": user_id, "agent_id": agent_id, "delegation_id": delegation_id},
            )
        )


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


def build_app(settings: Settings) -> FastAPI:
    """F1's real ``create_app``; registers whoami only if ``create_mcp_server`` did not."""
    app = create_app(settings)
    server = app.state.mcp_server
    if "whoami" not in {tool.name for tool in server._tool_manager.list_tools()}:
        identity_tools.register(server, settings)
    return app


@pytest.fixture
def auth_client(runtime: AuthRuntime, auth_settings: Settings) -> Iterator[TestClient]:
    app = build_app(auth_settings)
    # Entering the TestClient context runs the app lifespan (MCP session manager).
    with TestClient(app, base_url=PUBLIC_BASE_URL) as client:
        yield client
