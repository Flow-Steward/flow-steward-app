#!/usr/bin/env bash
# Upgrade the isolated candidate while every database writer is quiesced.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/release/candidate_operation_common.sh
source "$SCRIPT_DIR/candidate_operation_common.sh"
require_candidate_context
preflight_candidate_operation

cleanup() {
  code=$?
  trap - EXIT
  if [ "$maintenance_active" -eq 1 ]; then
    restore_candidate_services || true
  fi
  exit "$code"
}
trap cleanup EXIT

begin_candidate_maintenance
candidate_stack --profile cli up -d --no-build fs-cli >/dev/null
candidate_stack --profile cli exec -T fs-cli python -m core.entrypoints.cli --non-interactive \
  database upgrade --author release-smoke --maintenance-confirmed --confirm --plain
restore_candidate_services
wait_candidate_ready

trap - EXIT
printf 'candidate database upgrade completed commit=%s project=%s\n' \
  "$CANDIDATE_COMMIT" "$COMPOSE_PROJECT"
