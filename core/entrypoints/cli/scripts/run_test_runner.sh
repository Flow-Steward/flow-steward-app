#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
# shellcheck source=core/entrypoints/cli/scripts/docker_compose_env.sh
source "${ROOT_DIR}/core/entrypoints/cli/scripts/docker_compose_env.sh"

if [[ $# -eq 0 ]]; then
  cat <<'EOF'
Usage:
  core/entrypoints/cli/scripts/run_test_runner.sh python -m pytest tests/e2e/test_workflow_parallelism.py -q -rs

This runs the command inside the isolated `fs-test-runner` compose service,
which talks to the live app at http://fs-app:8099 by default.
EOF
  exit 1
fi

fs_compose run --rm -T fs-test-runner "$@"
