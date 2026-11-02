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
uv run uvicorn revel.app:app --reload     # http://localhost:8000/health (also /healthz), MCP at /mcp
uv run pytest                             # tests
uv run ruff check . && uv run ruff format --check .
```

Configuration comes from environment variables, then `backend/.env`, then the repository-root
`.env` (copy `../.env.example`). Local Postgres and OpenFGA: `docker compose up -d` from the repo
root (see `../dev/README.md`).

`APP_ENV=production` requires an `https://` `PUBLIC_BASE_URL`; `create_app` raises otherwise.
Deployed checks use `/health`: Cloud Run reserves some paths ending in `z`. The container image
is built from the repo-root `Dockerfile` (see `../infra/README.md`); it never contains `.env`.

## Database migrations

F3 owns `alembic.ini`, `migrations/` and `src/revel/db/` (models in `revel.db.models`, enums in
`revel.db.enums`, engine/sessions in `revel.db.session`). Other features request schema changes
from F3, which keeps a single Alembic head. `migrations/env.py` reads the URL from settings
(`DATABASE_URL`); it is never stored in `alembic.ini` or logged. Cloud SQL unix-socket URLs work:
`postgresql+psycopg://USER:PASS@/revel?host=/cloudsql/PROJECT:REGION:INSTANCE`.

```sh
cd backend
uv run alembic upgrade head      # apply (explicit step, not run on app startup)
uv run alembic check             # models and migrations agree (no drift)
uv run alembic heads             # must print exactly one head
uv run alembic downgrade base    # roll back everything (destroys data)
uv run alembic revision --autogenerate -m "..."   # then hand-review; name constraints via conventions
```

PostgreSQL-backed tests run against a disposable database and are skipped unless
`REVEL_TEST_DATABASE_URL` is set; they downgrade it to base and recreate it:

```sh
POSTGRES_HOST_PORT=55433 docker compose -p revel-test up -d --wait postgres   # from repo root
docker compose -p revel-test exec -T postgres psql -U revel -d revel -c "CREATE DATABASE revel_test"
REVEL_TEST_DATABASE_URL=postgresql+psycopg://revel:revel@localhost:55433/revel_test uv run pytest tests/db
```

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
