"""Application composition: FastAPI HTTP API plus the MCP Streamable HTTP endpoint.

Run locally with ``uv run uvicorn revel.app:app --reload`` from ``backend/``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from revel.api import routers
from revel.auth.authz import configure_authz, reset_authz
from revel.auth.fga import create_fga_client
from revel.auth.mcp_auth import mcp_auth_kwargs
from revel.auth.visibility import resolve_profile_for_user
from revel.mcp.server import build_mcp_asgi_app, create_mcp_server
from revel.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_runtime()

    # A session manager can run only once, so each app gets its own MCP server instance.
    mcp_server = create_mcp_server(settings, **mcp_auth_kwargs(settings))
    mcp_app = build_mcp_asgi_app(mcp_server, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The OpenFGA client opens an HTTP session, so it is created inside the event loop.
        # configure_authz makes no network calls, so the app boots before fga_bootstrap has run.
        app.state.fga = create_fga_client(settings)
        # The profile resolver opens its own short read-only session per lookup (lazy engine).
        configure_authz(app.state.fga, resolve_profile_for_user)
        try:
            # The MCP sub-app's own lifespan is not run; the parent owns MCP startup/shutdown.
            async with mcp_server.session_manager.run():
                yield
        finally:
            reset_authz(app.state.fga)
            await app.state.fga.close()

    app = FastAPI(title="Revel Gifts", lifespan=lifespan)
    app.state.settings = settings
    app.state.mcp_server = mcp_server

    # Cloud Run reserves some paths ending in "z", so /healthz may never reach the app on
    # *.run.app (https://docs.cloud.google.com/run/docs/known-issues, "Reserved URL paths").
    # Probes and deployed checks use /health; /healthz stays for local and compose checks.
    @app.get("/health", include_in_schema=False)
    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    for router in routers():
        app.include_router(router)

    # Mount the SDK app at "/" LAST: its internal route is exactly settings.mcp_path (no /mcp/
    # redirect), and every FastAPI route registered above (/health, /healthz, /.well-known/*,
    # /me) wins.
    # Routes added to `app` after this line would be shadowed; add them via revel.api.routers().
    app.mount("/", mcp_app)
    return app


app = create_app()
