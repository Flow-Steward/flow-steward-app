#!/usr/bin/env bash
# Rebuild and recreate the exact immutable candidate named by the smoke gate.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/release/candidate_operation_common.sh
source "$SCRIPT_DIR/candidate_operation_common.sh"
require_candidate_context

APP_CONTAINER="${FS_SMOKE_APP_CONTAINER:-}"
if [ -n "$APP_CONTAINER" ]; then
  assert_container_project "$APP_CONTAINER"
fi

restore_candidate_images
preflight_candidate_operation
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
candidate_stack --profile cli up -d --no-build --force-recreate >/dev/null
maintenance_active=0
wait_candidate_ready

trap - EXIT
printf 'candidate rebuilt commit=%s project=%s\n' "$CANDIDATE_COMMIT" "$COMPOSE_PROJECT"
