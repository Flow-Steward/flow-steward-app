#!/usr/bin/env bash
set -euo pipefail
umask 077

# Validate an encrypted PostgreSQL backup without mutating any database.
# Usage:
#   core/entrypoints/cli/scripts/db_backup_validate.sh <backup_file>

IN_FILE="${1:-}"
if [[ -z "${IN_FILE}" ]]; then
  echo "Usage: core/entrypoints/cli/scripts/db_backup_validate.sh <backup_file>" >&2
  exit 1
fi
if [[ ! -f "${IN_FILE}" ]]; then
  echo "Backup file not found: ${IN_FILE}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
ARTIFACT_ROOT="${FS_ARTIFACT_ROOT:-${ROOT_DIR}}"
BACKUP_DIR="${ARTIFACT_ROOT}/backups"
# shellcheck source=core/entrypoints/cli/scripts/docker_compose_env.sh
source "${ROOT_DIR}/core/entrypoints/cli/scripts/docker_compose_env.sh"
PROTECT_EXPORT="${ROOT_DIR}/core/application/tooling/db/protected_export.py"
PYTHON_BIN="${PYTHON_BIN:-${FS_PYTHON_BIN:-python3}}"

if [[ -z "${FS_BACKUP_ENCRYPTION_KEY:-}" ]]; then
  ENV_FILE="$(fs_compose_env_file_path)"
  if BACKUP_KEY="$(fs_read_env_file_value "FS_BACKUP_ENCRYPTION_KEY" "${ENV_FILE}")"; then
    export FS_BACKUP_ENCRYPTION_KEY="${BACKUP_KEY}"
  fi
fi

if [[ -L "${BACKUP_DIR}" ]]; then
  echo "[validate] canonical backups directory must not be a symbolic link" >&2
  exit 1
fi
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
BACKUP_DIR_REAL="$(cd -- "${BACKUP_DIR}" && pwd -P)"
if [[ -z "${FS_BACKUP_ENCRYPTION_KEY:-}" ]]; then
  echo "[validate] FS_BACKUP_ENCRYPTION_KEY is required" >&2
  exit 1
fi

is_protected_export() {
  head -c 64 "$1" | LC_ALL=C grep -q "^protected-export:"
}

if ! is_protected_export "${IN_FILE}"; then
  echo "[validate] input is not a protected export" >&2
  exit 1
fi

TMP_DUMP="$(mktemp "${BACKUP_DIR_REAL}/.db_backup_validate.XXXXXX.dump")"
chmod 600 "${TMP_DUMP}"
trap 'rm -f "${TMP_DUMP}"' EXIT

if ! "${PYTHON_BIN}" "${PROTECT_EXPORT}" decrypt --input "${IN_FILE}" --output "${TMP_DUMP}"; then
  echo "[validate] protected export decrypt failed" >&2
  exit 1
fi

if ! fs_compose exec -T pg-fs pg_restore -l < "${TMP_DUMP}" > /dev/null; then
  echo "[validate] decrypted payload is not a readable PostgreSQL custom-format dump" >&2
  exit 1
fi

echo "[validate] backup is a readable protected PostgreSQL custom-format dump"
