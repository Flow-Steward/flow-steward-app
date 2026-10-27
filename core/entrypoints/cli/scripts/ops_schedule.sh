#!/usr/bin/env bash
set -euo pipefail
umask 077

# Run recurring maintenance tasks and persist generated artifacts under
# canonical root folders (`logs/`, `backups/`).
# Usage:
#   core/entrypoints/cli/scripts/ops_schedule.sh backup
#   core/entrypoints/cli/scripts/ops_schedule.sh retention
#   core/entrypoints/cli/scripts/ops_schedule.sh health
#   core/entrypoints/cli/scripts/ops_schedule.sh workspace-prune
#   core/entrypoints/cli/scripts/ops_schedule.sh all

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
ARTIFACT_ROOT="${FS_ARTIFACT_ROOT:-${ROOT_DIR}}"
LOG_DIR="${ARTIFACT_ROOT}/logs/runtime"
BACKUP_DIR="${ARTIFACT_ROOT}/backups"
HEALTH_DIR="${ARTIFACT_ROOT}/logs/health"
WORKSPACE_DIR="${ARTIFACT_ROOT}/logs/workspaces"
PYTHON_BIN="${PYTHON_BIN:-${FS_PYTHON_BIN:-python3}}"

mkdir -p "${LOG_DIR}" "${BACKUP_DIR}" "${HEALTH_DIR}" "${WORKSPACE_DIR}"

ACTION="${1:-all}"

now_utc() {
  date -u +"%Y%m%dT%H%M%SZ"
}

append_log() {
  local log_file="$1"
  shift
  printf '%s %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*" >> "${log_file}"
}

run_task() {
  local task="$1"
  shift

  local run_ts
  run_ts="$(now_utc)"
  local run_log="${LOG_DIR}/${task}_${run_ts}.log"
  local rolling_log="${LOG_DIR}/${task}.log"

  append_log "${rolling_log}" "[start] task=${task} run_log=${run_log}"
  if "$@" > "${run_log}" 2>&1; then
    cat "${run_log}" >> "${rolling_log}"
    append_log "${rolling_log}" "[ok] task=${task}"
    return 0
  fi

  local code=$?
  cat "${run_log}" >> "${rolling_log}" || true
  append_log "${rolling_log}" "[fail] task=${task} exit_code=${code}"
  return "${code}"
}

run_backup() {
  local ts
  ts="$(now_utc)"
  local out_file="${BACKUP_DIR}/flow_steward_${ts}.dump.enc"
  run_task "backup" "${ROOT_DIR}/core/entrypoints/cli/scripts/db_backup.sh" "${out_file}"
}

run_retention() {
  run_task "retention" \
    "${PYTHON_BIN}" \
    -m core.application.tooling.ops.retention_cleanup
}

run_health_snapshot() {
  local ts
  ts="$(now_utc)"
  local out_file="${HEALTH_DIR}/health_${ts}.json"
  run_task "health" \
    "${PYTHON_BIN}" \
    -m core.entrypoints.cli \
    --json \
    jobs \
    health-snapshot \
    --output "${out_file}"
}

run_workspace_prune() {
  run_task "workspace_prune" \
    "${PYTHON_BIN}" \
    -m core.application.tooling.ops.prune_workspace_artifacts \
    --workspace-root "${WORKSPACE_DIR}"
}

status=0
case "${ACTION}" in
  backup)
    run_backup || status=$?
    ;;
  retention)
    run_retention || status=$?
    ;;
  health)
    run_health_snapshot || status=$?
    ;;
  workspace-prune)
    run_workspace_prune || status=$?
    ;;
  all)
    run_backup || status=$?
    run_retention || status=$?
    run_health_snapshot || status=$?
    run_workspace_prune || status=$?
    ;;
  *)
    echo "Usage: core/entrypoints/cli/scripts/ops_schedule.sh {backup|retention|health|workspace-prune|all}" >&2
    exit 2
    ;;
esac

exit "${status}"
