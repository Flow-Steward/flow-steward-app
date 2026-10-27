#!/usr/bin/env bash
# Exercise an actual dump/restore rollback only against the named isolated candidate.
set -euo pipefail
umask 077

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/release/candidate_operation_common.sh
source "$SCRIPT_DIR/candidate_operation_common.sh"
require_candidate_context
preflight_candidate_operation

PG_CONTAINER="${FS_SMOKE_PG_CONTAINER:-}"
assert_container_project "$PG_CONTAINER"

RELEASE_DIR="$REPO_ROOT/.flow-steward/release/$CANDIDATE_COMMIT"
DUMP_FILE="$RELEASE_DIR/rollback-smoke.dump"
HASH_FILE="$RELEASE_DIR/rollback-smoke.sha256"
mkdir -p "$RELEASE_DIR"
cleanup() {
  code=$?
  trap - EXIT
  rm -f "$DUMP_FILE"
  if [ "$maintenance_active" -eq 1 ]; then
    restore_candidate_services >/dev/null 2>&1 || true
  fi
  exit "$code"
}
trap cleanup EXIT

begin_candidate_maintenance
candidate_stack --profile cli run --rm -T --entrypoint python fs-cli \
  -m core.application.tooling.ops.rebuild_workflow_knowledge_graph --drop-only >/dev/null
candidate_stack exec -T pg-fs sh -eu -c \
  'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' >"$DUMP_FILE"
[ -s "$DUMP_FILE" ] || { printf 'candidate rollback dump is empty\n' >&2; exit 1; }
candidate_stack exec -T pg-fs pg_restore -l <"$DUMP_FILE" >/dev/null
shasum -a 256 "$DUMP_FILE" | awk '{print $1}' >"$HASH_FILE"

candidate_stack exec -T pg-fs sh -eu -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()" >/dev/null'
candidate_stack exec -T pg-fs sh -eu -c \
  'exec dropdb --if-exists --force --maintenance-db=postgres \
    -U "$POSTGRES_USER" "$POSTGRES_DB"'
candidate_stack exec -T pg-fs sh -eu -c \
  'exec createdb --maintenance-db=postgres -U "$POSTGRES_USER" \
    -O "$POSTGRES_USER" "$POSTGRES_DB"'
candidate_stack exec -T pg-fs sh -eu -c \
  'exec pg_restore --no-owner --no-privileges \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <"$DUMP_FILE"
candidate_stack --profile cli run --rm -T --entrypoint python fs-cli \
  -m core.application.tooling.ops.rebuild_workflow_knowledge_graph >/dev/null
restore_candidate_services
wait_candidate_ready

rm -f "$DUMP_FILE"
trap - EXIT
printf 'candidate rollback restored project=%s dump_sha256=%s\n' \
  "$COMPOSE_PROJECT" "$(tr -d '[:space:]' <"$HASH_FILE")"
