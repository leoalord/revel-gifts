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
