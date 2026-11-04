# Authorization

Executable OpenFGA authorization model and permission cases.

- `model.fga`: model v2 (schema 1.1), additive over v1 (every v1 type and relation is unchanged).
  - v1: `user` (`delegate`), `agent` (`acts_for`) and `profile` (`owner`, computed `owner_agent` = `delegate from owner`, `contribute`, `view_only_me`). An `agent` object ID is an `agent_delegations.id`, not the global `agents.id`. Profile creation writes only `owner`; `owner_agent` is never written.
  - Agent connect writes `acts_for`, `delegate` and `platform:revel#connected_agent` (no profile needed); disconnect deletes all three. `platform:revel` exists because OpenFGA rejects `delegate from public_viewer` over a `[user:*]` tupleset, and `agent:*` would also match disconnected delegations.
  - `profile` tiers: written `trusted`, `connection` (`[user]`), `public_viewer` (`[user:*, platform#connected_agent]`) and `blocked` (`[user]`); computed `view_trusted` > `view_connections` > `view_public`, each including the higher tiers plus the owner and owner's agents. A user's agents inherit the user's tier (`delegate from trusted|connection|blocked`). `profile:<B>#blocked@user:<A>` removes A and A's agents from every tier on B's profile, Public included, even with a stale grant; it is directional and never applies to the owner.
  - `question`: written `about` (`[profile]`) and `requester` (`[user]`); computed `requester_agent`, `respond` (= `owner_agent from about`) and `read_answer` (owner side always; requester and requester's agents unless the owner blocked the requester). `read_answer` does not follow the requester's tier: it survives revoking the requester's connection (only a block removes it), so F7 (#20) must re-check, when releasing an answer, that the requester can still see the facts it relies on.
- `tests/negative/*.fga.yaml`: cases the CLI must reject (direct writes of computed relations, agents in `trusted`/`connection`/`requester`, an `agent:*` `public_viewer`).
- `tests/*.fga.yaml`: OpenFGA CLI test cases (tuples, checks, list_objects).
- `model.json`: the committed JSON form of `model.fga`, produced by `scripts/generate-model-json.sh` (pinned CLI `model transform`). The backend bootstrap writes this file, so deploying the model needs neither Docker nor a DSL parser (the Python SDK has no DSL transformer). Regenerate it whenever `model.fga` changes.
- `scripts/test-model.sh`: validates the model, runs all tests with the pinned CLI image `openfga/cli:v0.8.2` via Docker, and fails if `model.json` is stale; exits non-zero on failure.

Run from the repository root: `bash authorization/scripts/test-model.sh` (requires Docker).

## Runtime integration

- [Authorization service](../backend/src/revel/auth/authz.py) owns principal mapping, permission checks and the hook transaction contract. Read its transaction-order and locking rules before changing a caller. Revokes run after the row write and before commit. Grants on existing objects run after commit.
- [Claim visibility](../backend/src/revel/auth/visibility.py) owns the shared and owner claim-read paths.
- [Bootstrap](../backend/src/revel/auth/fga_bootstrap.py) installs the model explicitly. [Reconcile](../backend/src/revel/auth/fga_reconcile.py) repairs tuple drift. The [infrastructure runbook](../infra/README.md#openfga-bootstrap) covers deployed commands.
- For real-server integration tests, see [the isolated Compose setup](../backend/tests/authz/test_authz_openfga.py).

[Relationships, Discovery & Privacy](https://app.notion.com/p/40b2bade7f31811d8e55d8818e949b31) owns audience, contribution, approval and revocation decisions. [MVP scope](https://app.notion.com/p/40b2bade7f3181ad84f0d14cf641fec7) defines required checks. Keep implemented models/tests traceable to those pages rather than maintaining a second prose policy here.
