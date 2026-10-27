#!/usr/bin/env bash
set -euo pipefail
umask 077

# OPERATOR RECOVERY ONLY. This destructive script is intentionally not registered in the public Flow Steward CLI.
# Restore PostgreSQL backup into pg-fs.
# Supports protected exports (*.enc), custom-format dumps, and plain SQL.
#
# Usage:
#   core/entrypoints/cli/scripts/db_restore.sh <backup_file> \
#     --target-compose-project <exact-project> --confirm
#
# WARNING: this replaces the confirmed target database from template0 before restore.

IN_FILE="${1:-}"
if [[ -z "$IN_FILE" ]]; then
  echo "Usage: core/entrypoints/cli/scripts/db_restore.sh <backup_file> --target-compose-project <exact-project> --confirm" >&2
  exit 1
fi
shift
target_compose_project=""
confirmed=0
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --target-compose-project)
      target_compose_project="${2:-}"
      shift 2
      ;;
    --confirm)
      confirmed=1
      shift
      ;;
    *)
      echo "Unknown restore argument: $1" >&2
      exit 1
      ;;
  esac
done
if [[ ! -f "$IN_FILE" ]]; then
  echo "Backup file not found: $IN_FILE" >&2
  exit 1
fi
if [[ -z "${FS_BACKUP_ENCRYPTION_KEY:-}" ]]; then
  echo "FS_BACKUP_ENCRYPTION_KEY must be exported explicitly for operator recovery" >&2
  exit 1
fi

DB_USER="${POSTGRES_USER:-flow_steward}"
DB_NAME="${POSTGRES_DB:-flow_steward}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
# shellcheck source=core/entrypoints/cli/scripts/docker_compose_env.sh
source "${ROOT_DIR}/core/entrypoints/cli/scripts/docker_compose_env.sh"
if [[ "$confirmed" -ne 1 ]]; then
  echo "Restore requires --confirm" >&2
  exit 1
fi
if [[ -z "$target_compose_project" ]]; then
  echo "Restore requires --target-compose-project <exact-project>" >&2
  exit 1
fi
if [[ "$target_compose_project" != "$FS_COMPOSE_PROJECT_NAME" ]]; then
  echo "Restore target mismatch: requested '$target_compose_project', selected '$FS_COMPOSE_PROJECT_NAME'" >&2
  exit 1
fi
protect_export_container="/app/core/application/tooling/db/protected_export.py"
restore_tooling_service="fs-app"
# The services that write to the database. AC17-R162: on Compact `stack stop` is
# refused, so the runbook had no way to stop them, and a restore ran underneath a
# runtime that kept writing.
restore_writer_services=(fs-app fs-worker control-plane fs-mcp fs-extension-installer)
if [[ " ${FS_COMPOSE_FILES:-} " == *"docker-compose.compact.yaml"* ]]; then
  restore_tooling_service="compact-runtime"
  restore_writer_services=(compact-runtime)
fi
stopped_writer_services=()

# Stop every writer that runs, and leave it stopped: ./install.sh starts it again and
# verifies the installation, which a restore script cannot.
stop_restore_writers() {
  local running service
  running=" $(fs_compose ps --status running --services | tr '\n' ' ') "
  for service in "${restore_writer_services[@]}"; do
    if [[ "$running" == *" ${service} "* ]]; then
      stopped_writer_services+=("$service")
    fi
  done
  if [[ "${#stopped_writer_services[@]}" -gt 0 ]]; then
    echo "[restore] stopping the services that write to the database: ${stopped_writer_services[*]}"
    fs_compose stop "${stopped_writer_services[@]}"
  fi
}

report_stopped_writers() {
  local status=$?
  if [[ "$status" -ne 0 && "$database_replaced" -eq 1 ]]; then
    # AC17-R164: the restore died after the database was dropped, before the graph
    # catalogue repair, and the message still said to run ./install.sh -- which started
    # the runtime on a database whose graph search was broken, and reported it healthy.
    echo "[restore] FAILED after the database was replaced: it is incomplete and its graph catalogue was not repaired. Do not start the runtime; rerun this restore until it prints \"[restore] done\"." >&2
    return "$status"
  fi
  if [[ "${#stopped_writer_services[@]}" -gt 0 ]]; then
    echo "[restore] ${stopped_writer_services[*]} stopped for the restore and left stopped; run ./install.sh to start them and verify the installation." >&2
  fi
  return "$status"
}
database_replaced=0
trap report_stopped_writers EXIT

# AC17-R164: pg_restore stops reading a custom-format dump at its last entry, and on
# Compact the decryption that fed it then failed on "broken pipe" -- pipefail ended the
# script before the graph catalogue repair. Take the whole stream first, as the
# inspection above does, and restore from the file.
apply_custom_dump() {
  fs_compose exec -T pg-fs sh -ceu '
    umask 077
    restore_archive="$(mktemp /tmp/flow-steward-apply.XXXXXX)"
    trap '\''rm -f "$restore_archive"'\'' EXIT
    cat > "$restore_archive"
    pg_restore -v --no-owner --no-privileges -U "$1" -d "$2" "$restore_archive"
  ' sh "$DB_USER" "$DB_NAME"
}

is_protected_export() {
  head -c 64 "$1" | LC_ALL=C grep -q "^protected-export:"
}

protected_export_decrypt_mode() {
  if head -c 64 "$1" | LC_ALL=C grep -q "^protected-export:aesgcm-stream-v2$"; then
    printf '%s' "decrypt"
  else
    printf '%s' "decrypt-legacy"
  fi
}

decrypt_protected_export() {
  local mode="$1"
  fs_compose run --rm --no-deps -T --pull never \
    -e FS_BACKUP_ENCRYPTION_KEY \
    -e FS_ARTIFACT_ROOT=/tmp \
    --entrypoint python \
    "${restore_tooling_service}" \
    "${protect_export_container}" "${mode}"
}

inspect_protected_custom_export() {
  fs_compose exec -T pg-fs sh -ceu '
    umask 077
    restore_archive="$(mktemp /tmp/flow-steward-restore.XXXXXX)"
    trap '\''rm -f "$restore_archive"'\'' EXIT
    cat > "$restore_archive"
    pg_restore -l "$restore_archive" > /dev/null
  '
}

prepare_restore_database() {
  case "$DB_NAME" in
    postgres | template0 | template1)
      echo "Refusing to replace PostgreSQL system database: $DB_NAME" >&2
      exit 1
      ;;
  esac
  echo "[restore] replacing confirmed target database: $DB_NAME"
  fs_compose exec -T pg-fs dropdb \
    --if-exists \
    --force \
    --maintenance-db=postgres \
    -U "$DB_USER" \
    "$DB_NAME"
  fs_compose exec -T pg-fs createdb \
    --template=template0 \
    --owner="$DB_USER" \
    --maintenance-db=postgres \
    -U "$DB_USER" \
    "$DB_NAME"
}

finalize_restored_database() {
  local finalize_sql
  # One definition, also run by `./flow-steward doctor --fix graph_catalog`.
  finalize_sql="$(cat -- "${SCRIPT_DIR}/age_catalog_repair.sql")"
  fs_compose exec -T pg-fs psql \
    -v ON_ERROR_STOP=1 \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    -c "$finalize_sql"
}

restore_kind="$IN_FILE"
if [[ "$IN_FILE" == *.enc ]]; then
  restore_kind="${IN_FILE%.enc}"
fi

if [[ "$IN_FILE" == *.enc ]] && ! is_protected_export "$IN_FILE"; then
  echo "Encrypted backup extension provided but file is not a protected export: $IN_FILE" >&2
  exit 1
fi

# AC17-R161: the runbook stops the stack before a restore, which stops pg-fs too, and the
# restore then failed on "service pg-fs is not running" with the installation fully down.
# Start the database alone -- no writer comes up with it -- and wait until it serves.
echo "[restore] starting the database alone"
fs_compose up -d --no-deps --wait pg-fs

case "$restore_kind" in
  *.sql)
    if is_protected_export "$IN_FILE"; then
      decrypt_protected_export "$(protected_export_decrypt_mode "$IN_FILE")" \
        < "$IN_FILE" > /dev/null
    fi
    ;;
  *)
    echo "[restore] inspecting custom-format dump before replacing the target"
    if is_protected_export "$IN_FILE"; then
      decrypt_protected_export "$(protected_export_decrypt_mode "$IN_FILE")" < "$IN_FILE" | \
        inspect_protected_custom_export
    else
      fs_compose exec -T pg-fs pg_restore -l < "$IN_FILE" > /dev/null
    fi
    ;;
esac

stop_restore_writers
database_replaced=1
prepare_restore_database

case "$restore_kind" in
  *.sql)
    echo "[restore] applying SQL file: $IN_FILE"
    if is_protected_export "$IN_FILE"; then
      decrypt_protected_export "$(protected_export_decrypt_mode "$IN_FILE")" < "$IN_FILE" | \
        fs_compose exec -T pg-fs psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME"
    else
      fs_compose exec -T pg-fs psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" < "$IN_FILE"
    fi
    ;;
  *)
    echo "[restore] applying custom-format dump: $IN_FILE"
    if is_protected_export "$IN_FILE"; then
      decrypt_protected_export "$(protected_export_decrypt_mode "$IN_FILE")" < "$IN_FILE" | \
        apply_custom_dump
    else
      apply_custom_dump < "$IN_FILE"
    fi
    ;;
esac

finalize_restored_database
database_replaced=0

echo "[restore] done"
