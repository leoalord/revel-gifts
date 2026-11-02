# Revel Gifts

Agent-first gifting context and collaboration. This repository currently contains a directory skeleton, implementation guidance and historical research reports; no runnable application or verified integration exists yet.

Coding agents and orchestrators: read [AGENTS.md](AGENTS.md) for source references, ownership and review gates.

## Source of truth

[Notion](https://app.notion.com/p/4092bade7f31805191e7c4c884960908) owns product decisions and the agreed stack. The [MVP page](https://app.notion.com/p/40b2bade7f3181ad84f0d14cf641fec7) owns build scope, priorities and acceptance criteria. Repository documentation describes code organization and operation; it does not independently define product requirements. If it conflicts with Notion, follow the owning Notion page and reconcile the repository guidance before implementation. Do not infer new requirements from the historical research reports.

## Layout

| Path | Responsibility |
| --- | --- |
| `backend/` | Python application, MCP/HTTP adapters, shared services and persistence |
| `frontend/` | Human interface |
| `agent-guidance/` | Versioned agent-facing workflow instructions |
| `authorization/` | Executable authorization models and permission cases |
| `infra/` | Infrastructure as code |
| `dev/` | Local development and synthetic fixtures |
| `docs/` | Implementation and operating guidance |

Keep the remote repository private as directed by Notion. Setup commands will be documented when implemented. See [deployment guidance](docs/deployment.md).
