# Authorization

Executable OpenFGA authorization model and permission cases.

- `model.fga`: model v1 (schema 1.1): `user`, `agent` (`acts_for`) and `profile` (`owner`, `owner_agent`, `contribute`, `view_only_me`). An `agent` object ID is an `agent_delegations.id`, not the global `agents.id`.
- `tests/*.fga.yaml`: OpenFGA CLI test cases (tuples, checks, list_objects).
- `model.json`: the committed JSON form of `model.fga`, produced by `scripts/generate-model-json.sh` (pinned CLI `model transform`). The backend bootstrap writes this file, so deploying the model needs neither Docker nor a DSL parser (the Python SDK has no DSL transformer). Regenerate it whenever `model.fga` changes.
- `scripts/test-model.sh`: validates the model, runs all tests with the pinned CLI image `openfga/cli:v0.8.2` via Docker, and fails if `model.json` is stale; exits non-zero on failure.

Run from the repository root: `bash authorization/scripts/test-model.sh` (requires Docker).

## Runtime service (backend)

- `backend/src/revel/auth/fga.py`: `create_fga_client(settings)` (stored at `app.state.fga`), idempotent tuple writes/deletes, checks. `OPENFGA_GOOGLE_AUTH=true` sends a Google ID token with audience `OPENFGA_API_URL`.
- `backend/src/revel/auth/authz.py`: subject mapping (`agent:<delegation_id>` when a delegation is active, else `user:<user_id>`; inconsistent principals are denied), module-level hooks `on_user_created` / `on_agent_delegated` / `on_agent_disconnected` over the client installed by `configure_authz` (app lifespan), reconciliation helpers, `can_contribute`/`require_contribute`, `resolve_viewer_tier`/`require_viewer`. The module docstring states the fail-closed rule: grant hooks warn and return while unconfigured, revoke hooks and checks raise. Claim filtering (`visible_claims`) lands with the claims schema.
- Bootstrap, explicitly (never at app startup), from `backend/`: `uv run python -m revel.auth.fga_bootstrap`. It uses `OPENFGA_STORE_ID` or finds/creates the store named `revel-gifts`, writes `model.json` only if it differs from the latest model, and prints only `OPENFGA_STORE_ID=` / `OPENFGA_AUTHORIZATION_MODEL_ID=` on stdout. Model path: `--model`, then `OPENFGA_MODEL_PATH`, then `../authorization/model.json` from the working directory (image layout), then the repository path. `--check` is a read-only probe printing `unconfigured` or `healthy` (exit 0), non-zero otherwise.
- Integration tests (`pytest -m openfga`) need a real server; see `backend/tests/authz/test_authz_openfga.py` for the isolated compose command. They skip unless `OPENFGA_TEST_API_URL` is set and reachable.

[Relationships, Discovery & Privacy](https://app.notion.com/p/40b2bade7f31811d8e55d8818e949b31) owns audience, contribution, approval and revocation decisions. [MVP scope](https://app.notion.com/p/40b2bade7f3181ad84f0d14cf641fec7) defines required checks. Keep implemented models/tests traceable to those pages rather than maintaining a second prose policy here.
