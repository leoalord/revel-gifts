# Backend

One Python package under `src/revel/`:

```text
app.py       # Application composition and lifecycle (create_app, module-level app)
settings.py  # Environment configuration (pydantic-settings)
api/         # HTTP adapters; api.routers() lists routers included at the root
mcp/         # MCP adapters (official MCP Python SDK, MCPServer)
services/    # Shared operations and enforcement
auth/        # Identity/delegation and authorization integrations (mcp_auth.py, router.py: F2)
db/          # Models and persistence
```

`migrations/` holds database migrations (owned by F3); `tests/` holds backend checks.

## Setup and run

Requires [uv](https://docs.astral.sh/uv/) (Python 3.12 is pinned in `.python-version`).

```sh
cd backend
uv sync                                   # install runtime + dev dependencies from uv.lock
uv run uvicorn revel.app:app --reload     # http://localhost:8000/healthz, MCP at /mcp
uv run pytest                             # tests
uv run ruff check . && uv run ruff format --check .
```

Configuration comes from environment variables, then `backend/.env`, then the repository-root
`.env` (copy `../.env.example`). Local Postgres and OpenFGA: `docker compose up -d` from the repo
root (see `../dev/README.md`).

Inspect the MCP endpoint with the MCP Inspector CLI:

```sh
npx -y @modelcontextprotocol/inspector --cli http://localhost:8000/mcp --transport http --method tools/list
```

## MCP composition notes

- Pinned SDK: `mcp==2.3.0`; the high-level server is `mcp.server.mcpserver.MCPServer`.
- Stateless Streamable HTTP with JSON responses, so any Cloud Run replica serves any request.
- The SDK app is mounted at `/` after all FastAPI routes; its internal route is exactly
  `MCP_PATH`. Add HTTP routes through `revel.api.routers()` so they are registered before it.
- The parent lifespan runs `session_manager.run()`; mounted sub-app lifespans do not run.
- Host/Origin allowlist (DNS-rebinding protection): the `PUBLIC_BASE_URL` host/origin,
  `ALLOWED_HOSTS`/`ALLOWED_ORIGINS` extras, and localhost in development/test. Disallowed Host
  returns 421, disallowed Origin 403; requests without Origin are allowed.

Keep transport adapters thin and enforce shared behavior in services. Explicitly protect mounted MCP operations; HTTP route dependencies do not automatically secure them. See [deployment guidance](../docs/deployment.md).

[Notion](https://app.notion.com/p/4092bade7f31805191e7c4c884960908) owns stack choices; [MVP scope](https://app.notion.com/p/40b2bade7f3181ad84f0d14cf641fec7) owns required features and acceptance.
