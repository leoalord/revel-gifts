# Authorization

Executable OpenFGA authorization model and permission cases.

- `model.fga`: model v1 (schema 1.1): `user`, `agent` (`acts_for`) and `profile` (`owner`, `owner_agent`, `contribute`, `view_only_me`). An `agent` object ID is an `agent_delegations.id`, not the global `agents.id`.
- `tests/*.fga.yaml`: OpenFGA CLI test cases (tuples, checks, list_objects).
- `scripts/test-model.sh`: validates the model and runs all tests with the pinned CLI image `openfga/cli:v0.8.2` via Docker; exits non-zero on failure.

Run from the repository root: `bash authorization/scripts/test-model.sh` (requires Docker).

[Relationships, Discovery & Privacy](https://app.notion.com/p/40b2bade7f31811d8e55d8818e949b31) owns audience, contribution, approval and revocation decisions. [MVP scope](https://app.notion.com/p/40b2bade7f3181ad84f0d14cf641fec7) defines required checks. Keep implemented models/tests traceable to those pages rather than maintaining a second prose policy here.
