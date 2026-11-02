"""Application composition: FastAPI HTTP API plus the MCP Streamable HTTP endpoint.

Run locally with ``uv run uvicorn revel.app:app --reload`` from ``backend/``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from revel.api import routers
from revel.auth.mcp_auth import mcp_auth_kwargs
from revel.mcp.server import build_mcp_asgi_app, create_mcp_server
from revel.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    # A session manager can run only once, so each app gets its own MCP server instance.
    mcp_server = create_mcp_server(settings, **mcp_auth_kwargs(settings))
    mcp_app = build_mcp_asgi_app(mcp_server, settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # The MCP sub-app's own lifespan is not run; the parent owns MCP startup/shutdown.
        async with mcp_server.session_manager.run():
            yield

    app = FastAPI(title="Revel Gifts", lifespan=lifespan)
    app.state.settings = settings
    app.state.mcp_server = mcp_server

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    for router in routers():
        app.include_router(router)

    # Mount the SDK app at "/" LAST: its internal route is exactly settings.mcp_path (no /mcp/
    # redirect), and every FastAPI route registered above (/healthz, /.well-known/*, /me) wins.
    # Routes added to `app` after this line would be shadowed; add them via revel.api.routers().
    app.mount("/", mcp_app)
    return app


app = create_app()
