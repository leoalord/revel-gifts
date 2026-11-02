"""Process-wide auth components shared by the MCP server and the HTTP API.

One verifier and one resolver per settings object serve both transports, so MCP
tools and HTTP routes authenticate identically. ``create_app(settings)`` gets its
runtime via ``mcp_auth_kwargs(settings)``; HTTP routes find it through
``request.app.state.settings`` and MCP tools through the settings passed to
``register``. Tests may call :func:`configure_auth` first to inject a repository,
hooks or HTTP clients for that settings object.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import Request

from revel.auth.config import AuthConfig
from revel.auth.hooks import AuthzHooks
from revel.auth.identity import IdentityResolver
from revel.auth.repository import IdentityRepository, SqlAlchemyIdentityRepository
from revel.auth.tokens import JWKSCache, WorkOSTokenVerifier
from revel.auth.workos_users import NullUserDirectory, UserDirectory, WorkOSUserDirectory

logger = logging.getLogger(__name__)


class AuthNotConfigured(RuntimeError):
    """Auth runtime or identity store has not been configured for this process."""


@dataclass
class AuthRuntime:
    config: AuthConfig
    verifier: WorkOSTokenVerifier
    resolver: IdentityResolver | None
    http_client: httpx.AsyncClient | None = None
    _as_metadata: dict[str, Any] | None = field(default=None, repr=False)
    _as_metadata_at: float = field(default=0.0, repr=False)

    @property
    def repository(self) -> IdentityRepository:
        return self.require_resolver().repository

    def require_resolver(self) -> IdentityResolver:
        if self.resolver is None:
            raise AuthNotConfigured("identity repository is not configured")
        return self.resolver

    async def authorization_server_metadata(self, ttl_seconds: float = 3600.0) -> dict[str, Any]:
        """AuthKit's RFC 8414 metadata, cached in-process."""
        url = self.config.authorization_server_metadata_url
        if url is None:
            raise AuthNotConfigured("AuthKit domain is not configured")
        if self._as_metadata is not None and time.monotonic() - self._as_metadata_at < ttl_seconds:
            return self._as_metadata
        if self.http_client is not None:
            response = await self.http_client.get(url, timeout=5.0)
        else:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("authorization server metadata is not an object")
        self._as_metadata = data
        self._as_metadata_at = time.monotonic()
        return data


# Runtimes keyed by settings identity. The settings object is kept alongside so an
# ``id`` reused after garbage collection can never match a different object.
_runtimes: dict[int, tuple[Any, AuthRuntime]] = {}


def _default_repository(settings: Any) -> IdentityRepository:
    """SQLAlchemy repository over F3's ``revel.db.session`` (resolved lazily, on first use).

    The process settings use F3's shared engine/sessionmaker. A different settings
    object (e.g. ``create_app(custom_settings)``) gets its own engine built by F3's
    ``create_db_engine`` from that object's ``database_url``.
    """
    from sqlalchemy.orm import Session, sessionmaker

    from revel.db.session import create_db_engine, get_sessionmaker
    from revel.settings import get_settings

    def provider() -> sessionmaker[Session]:
        if settings is get_settings():
            return get_sessionmaker()
        engine = create_db_engine(settings.database_url)
        return sessionmaker(bind=engine, expire_on_commit=False)

    return SqlAlchemyIdentityRepository(provider)


def configure_auth(
    settings: Any,
    *,
    repository: IdentityRepository | None = None,
    user_directory: UserDirectory | None = None,
    hooks: AuthzHooks | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> AuthRuntime:
    """Build and register the auth runtime for ``settings``."""
    config = AuthConfig.from_settings(settings)
    jwks = JWKSCache(config.jwks_url, http_client=http_client) if config.jwks_url else None
    verifier = WorkOSTokenVerifier(config, jwks=jwks)
    if repository is None:
        repository = _default_repository(settings)
    if user_directory is None:
        user_directory = (
            WorkOSUserDirectory(
                config.workos_api_key,
                base_url=config.workos_api_base_url,
                http_client=http_client,
            )
            if config.workos_api_key
            else NullUserDirectory()
        )
    resolver = (
        IdentityResolver(repository, user_directory=user_directory, hooks=hooks)
        if repository is not None
        else None
    )
    runtime = AuthRuntime(
        config=config, verifier=verifier, resolver=resolver, http_client=http_client
    )
    _runtimes[id(settings)] = (settings, runtime)
    return runtime


def runtime_for(settings: Any) -> AuthRuntime:
    """Return the runtime registered for ``settings``, configuring one if needed."""
    entry = _runtimes.get(id(settings))
    if entry is not None and entry[0] is settings:
        return entry[1]
    return configure_auth(settings)


def get_auth_runtime(request: Request) -> AuthRuntime:
    """FastAPI dependency: the runtime for the application's settings."""
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise AuthNotConfigured("application settings are not available")
    return runtime_for(settings)


def reset_auth_runtimes() -> None:
    """Test helper: forget all configured runtimes."""
    _runtimes.clear()
