# Revel Gifts

Agent-first gifting context and collaboration. The repo contains the FastAPI and MCP backend, React interface, OpenFGA model, and local and GCP infrastructure.

Coding agents and orchestrators: read [AGENTS.md](AGENTS.md) for source references, ownership and review gates.

## Source of truth

[Notion](https://app.notion.com/p/4092bade7f31805191e7c4c884960908) owns product decisions and the agreed stack. The [MVP page](https://app.notion.com/p/40b2bade7f3181ad84f0d14cf641fec7) owns build scope, priorities and acceptance criteria. Repository documentation describes code organization and operation; it does not independently define product requirements. If it conflicts with Notion, follow the owning Notion page and reconcile the repository guidance before implementation. Historical research is available in [Notion](https://app.notion.com/p/40b2bade7f3181cf9709deca51417f33).

## Layout

| Path | Responsibility |
| --- | --- |
| `backend/` | Python application, MCP/HTTP adapters, shared services, persistence and [product skills](backend/src/revel/guidance/skills/index.md) |
| `frontend/` | Human interface |
| `authorization/` | Executable authorization models and permission cases |
| `infra/` | Local service setup and GCP infrastructure as code |
| `docs/` | Implementation and operating guidance |

Keep the remote repository private as directed by Notion. Setup: [backend](backend/README.md), [local services](infra/local/README.md). See [deployment guidance](docs/deployment.md).
