# Backend

One Python package under `src/revel/`:

```text
app.py       # Application composition and lifecycle; added during build
api/         # HTTP adapters
mcp/         # MCP adapters
services/    # Shared operations and enforcement
auth/        # Identity/delegation and authorization integrations
db/          # Models and persistence
```

`migrations/` holds database migrations; `tests/` holds backend checks. Dependency manifests, lockfiles and run commands will be added during implementation.

Keep transport adapters thin and enforce shared behavior in services. Explicitly protect mounted MCP operations; HTTP route dependencies do not automatically secure them. The parent application owns mounted-server startup/shutdown. See [deployment guidance](../docs/deployment.md).

[Notion](https://app.notion.com/p/4092bade7f31805191e7c4c884960908) owns stack choices; [MVP scope](https://app.notion.com/p/40b2bade7f3181ad84f0d14cf641fec7) owns required features and acceptance. Exact contracts remain to be designed with those features.
