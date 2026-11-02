#!/usr/bin/env bash
# Regenerate authorization/model.json from model.fga with the pinned OpenFGA CLI image.
# model.json is what `python -m revel.auth.fga_bootstrap` writes to OpenFGA, so a deployed
# bootstrap needs neither Docker nor a DSL parser. test-model.sh fails if it is stale.
set -euo pipefail

FGA_CLI_IMAGE="openfga/cli:v0.8.2"
AUTHZ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Print model.fga as canonical JSON (2-space indent, sorted keys).
transform_model() {
  docker run --rm -v "${AUTHZ_DIR}:/authorization:ro" -w /authorization "${FGA_CLI_IMAGE}" \
    model transform --file model.fga --input-format fga |
    python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin), indent=2, sort_keys=True))'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  transform_model >"${AUTHZ_DIR}/model.json"
  echo "Wrote ${AUTHZ_DIR}/model.json"
fi
