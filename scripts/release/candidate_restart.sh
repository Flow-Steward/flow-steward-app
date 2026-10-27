#!/usr/bin/env bash
# Restart candidate runtime services after proving every named container is isolated.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/release/candidate_operation_common.sh
source "$SCRIPT_DIR/candidate_operation_common.sh"
require_candidate_context
preflight_candidate_operation

assert_container_project "${FS_SMOKE_APP_CONTAINER:-}"
if [ -n "${FS_SMOKE_CLI_CONTAINER:-}" ]; then
  assert_container_project "$FS_SMOKE_CLI_CONTAINER"
fi

cleanup() {
  code=$?
  trap - EXIT
  if [ "$maintenance_active" -eq 1 ]; then
    restore_candidate_services >/dev/null 2>&1 || true
  fi
  exit "$code"
}
trap cleanup EXIT

begin_candidate_maintenance
restore_candidate_services
wait_candidate_ready

trap - EXIT
printf 'candidate restarted commit=%s project=%s\n' "$CANDIDATE_COMMIT" "$COMPOSE_PROJECT"
