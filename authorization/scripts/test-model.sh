#!/usr/bin/env bash
# Validate and test the OpenFGA model with the pinned OpenFGA CLI image.
set -euo pipefail

FGA_CLI_IMAGE="openfga/cli:v0.8.2"
AUTHZ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fga() {
  docker run --rm -v "${AUTHZ_DIR}:/authorization:ro" -w /authorization "${FGA_CLI_IMAGE}" "$@"
}

echo "Validating model.fga with ${FGA_CLI_IMAGE}"
validation="$(fga model validate --file model.fga)"
echo "${validation}"
if ! grep -q '"is_valid": *true' <<<"${validation}"; then
  echo "Model validation failed" >&2
  exit 1
fi

# Test files reference ../model.fga; the CLI only follows paths outside a test
# file's directory with --allow-external-files (all files here are repo-owned).
echo "Running tests/*.fga.yaml"
fga model test --allow-external-files --tests 'tests/*.fga.yaml'

# Negative cases must be rejected: profile#owner_agent is computed, never written directly.
echo "Running tests/negative/*.fga.yaml (each must be rejected)"
for negative in "${AUTHZ_DIR}"/tests/negative/*.fga.yaml; do
  name="tests/negative/$(basename "${negative}")"
  if output="$(fga model test --allow-external-files --tests "${name}" 2>&1)"; then
    echo "${output}"
    echo "Negative test unexpectedly passed: ${name}" >&2
    exit 1
  fi
  if ! grep -q "Invalid tuple" <<<"${output}"; then
    echo "${output}"
    echo "Negative test failed for an unexpected reason: ${name}" >&2
    exit 1
  fi
  echo "rejected as expected: ${name}"
done

# model.json (written by fga_bootstrap) must be the CLI transform of model.fga.
echo "Checking model.json matches model.fga"
# shellcheck source=generate-model-json.sh
source "${AUTHZ_DIR}/scripts/generate-model-json.sh"
if ! diff -u "${AUTHZ_DIR}/model.json" <(transform_model); then
  echo "model.json is stale: run bash authorization/scripts/generate-model-json.sh" >&2
  exit 1
fi
echo "model.json is up to date"
