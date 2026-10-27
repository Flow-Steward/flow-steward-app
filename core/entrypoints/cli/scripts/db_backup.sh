#!/usr/bin/env bash
set -euo pipefail
umask 077

# Create a compressed PostgreSQL backup from the pg-fs container.
# Usage:
#   core/entrypoints/cli/scripts/db_backup.sh [output_file]
# Example:
#   core/entrypoints/cli/scripts/db_backup.sh backups/flow_steward_$(date +%Y%m%d_%H%M%S).dump.enc

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
ARTIFACT_ROOT="${FS_ARTIFACT_ROOT:-${ROOT_DIR}}"
BACKUP_DIR="${ARTIFACT_ROOT}/backups"
BACKUP_LOG_DIR="${ARTIFACT_ROOT}/logs/runtime"
FAILURE_MESSAGE="Protected PostgreSQL backup failed; inspect PostgreSQL, backup-key configuration, and available disk space."
TMP_OUT=""
TMP_DUMP=""
TMP_ERROR=""

cleanup() {
  local code=$?
  trap - EXIT
  rm -f "${TMP_OUT}" "${TMP_DUMP}" "${TMP_ERROR}"
  if [[ "${code}" -ne 0 ]]; then
    mkdir -p "${BACKUP_LOG_DIR}"
    chmod 700 "${BACKUP_LOG_DIR}"
    local failure_log="${BACKUP_LOG_DIR}/backup_failure_$(date -u +%Y%m%dT%H%M%SZ)_$$.log"
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${FAILURE_MESSAGE}" > "${failure_log}"
    chmod 600 "${failure_log}"
    echo "[backup] retained redacted failure log: ${failure_log}" >&2
  fi
  exit "${code}"
}
trap cleanup EXIT

# shellcheck source=core/entrypoints/cli/scripts/docker_compose_env.sh
source "${ROOT_DIR}/core/entrypoints/cli/scripts/docker_compose_env.sh"
PROTECT_EXPORT="${ROOT_DIR}/core/application/tooling/db/protected_export.py"
PYTHON_BIN="${PYTHON_BIN:-${FS_PYTHON_BIN:-python3}}"
if [[ -L "${BACKUP_DIR}" ]]; then
  echo "[backup] canonical backups directory must not be a symbolic link" >&2
  exit 1
fi
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
BACKUP_DIR_REAL="$(cd -- "${BACKUP_DIR}" && pwd -P)"
OUT_FILE="${1:-${BACKUP_DIR}/flow_steward_$(date +%Y%m%d_%H%M%S).dump.enc}"
OUT_DIR="$(dirname -- "${OUT_FILE}")"
if [[ ! -d "${OUT_DIR}" ]] || [[ -L "${OUT_DIR}" ]]; then
  echo "[backup] output must be directly under the canonical backups directory" >&2
  exit 1
fi
OUT_DIR_REAL="$(cd -- "${OUT_DIR}" && pwd -P)"
if [[ "${OUT_DIR_REAL}" != "${BACKUP_DIR_REAL}" ]] || [[ "$(basename -- "${OUT_FILE}")" == .* ]]; then
  echo "[backup] output must be directly under the canonical backups directory" >&2
  exit 1
fi
OUT_FILE="${BACKUP_DIR_REAL}/$(basename -- "${OUT_FILE}")"

if [[ -z "${FS_BACKUP_ENCRYPTION_KEY:-}" ]]; then
  ENV_FILE="$(fs_compose_env_file_path)"
  if BACKUP_KEY="$(fs_read_env_file_value "FS_BACKUP_ENCRYPTION_KEY" "${ENV_FILE}")"; then
    export FS_BACKUP_ENCRYPTION_KEY="${BACKUP_KEY}"
  fi
fi
if [[ -z "${FS_BACKUP_ENCRYPTION_KEY:-}" ]]; then
  echo "[backup] FS_BACKUP_ENCRYPTION_KEY is required" >&2
  exit 1
fi
if [[ -e "${OUT_FILE}" || -L "${OUT_FILE}" ]]; then
  echo "[backup] output already exists: ${OUT_FILE}" >&2
  exit 1
fi

TMP_OUT="$(mktemp "${BACKUP_DIR_REAL}/.db_backup.XXXXXX.dump.enc")"
TMP_DUMP="$(mktemp "${BACKUP_DIR_REAL}/.db_backup_validate.XXXXXX.dump")"
TMP_ERROR="$(mktemp "${BACKUP_DIR_REAL}/.db_backup.XXXXXX.error")"
chmod 600 "${TMP_OUT}" "${TMP_DUMP}" "${TMP_ERROR}"

echo "[backup] capturing and encrypting protected export"
if ! fs_compose exec -T pg-fs pg_dump \
  -U "${POSTGRES_USER:-flow_steward}" \
  -d "${POSTGRES_DB:-flow_steward}" \
  -Fc 2> "${TMP_ERROR}" | "$PYTHON_BIN" "$PROTECT_EXPORT" encrypt > "$TMP_OUT" 2>> "${TMP_ERROR}"; then
  echo "[backup] pg_dump failed or protected export encryption failed; check PostgreSQL and backup-key configuration" >&2
  exit 1
fi

if [[ ! -s "${TMP_OUT}" ]] || ! head -c 64 "${TMP_OUT}" | LC_ALL=C grep -q "^protected-export:"; then
  echo "[backup] protected export envelope is missing or empty" >&2
  exit 1
fi
if ! "${PYTHON_BIN}" "${PROTECT_EXPORT}" decrypt --input "${TMP_OUT}" --output "${TMP_DUMP}"; then
  echo "[backup] protected export decrypt verification failed" >&2
  exit 1
fi
if ! fs_compose exec -T pg-fs pg_restore -l < "${TMP_DUMP}" > /dev/null; then
  echo "[backup] decrypted payload is not a readable PostgreSQL custom-format dump" >&2
  exit 1
fi

chmod 600 "${TMP_OUT}"
mv "${TMP_OUT}" "${OUT_FILE}"
rm -f "${TMP_DUMP}"
rm -f "${TMP_ERROR}"
trap - EXIT

echo "[backup] done"
