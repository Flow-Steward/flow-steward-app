#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
# shellcheck source=core/entrypoints/cli/scripts/docker_compose_env.sh
source "${ROOT_DIR}/core/entrypoints/cli/scripts/docker_compose_env.sh"

# Capture Postgres container logs to a timestamped file while tests are running.
# This is non-disruptive: it tails existing stderr logs instead of reconfiguring Postgres.
#
# Usage:
#   core/entrypoints/cli/scripts/capture_pg_slow_queries.sh
#   core/entrypoints/cli/scripts/capture_pg_slow_queries.sh --since 10m
#   core/entrypoints/cli/scripts/capture_pg_slow_queries.sh --since 30s logs/runtime/postgres_slow_manual.log

SINCE="${POSTGRES_SLOW_LOG_SINCE:-5m}"
OUT_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --since" >&2
        exit 1
      fi
      SINCE="$2"
      shift 2
      ;;
    -h|--help)
      cat <<'EOF'
Usage: core/entrypoints/cli/scripts/capture_pg_slow_queries.sh [--since <duration>] [output_file]

Examples:
  core/entrypoints/cli/scripts/capture_pg_slow_queries.sh
  core/entrypoints/cli/scripts/capture_pg_slow_queries.sh --since 10m
  core/entrypoints/cli/scripts/capture_pg_slow_queries.sh --since 30s logs/runtime/postgres_slow_manual.log
EOF
      exit 0
      ;;
    *)
      if [[ -n "$OUT_FILE" ]]; then
        echo "Unexpected extra argument: $1" >&2
        exit 1
      fi
      OUT_FILE="$1"
      shift
      ;;
  esac
done

if [[ -z "$OUT_FILE" ]]; then
  OUT_FILE="logs/runtime/postgres_slow_$(date -u +%Y%m%dT%H%M%SZ).log"
fi

mkdir -p "$(dirname "$OUT_FILE")"
touch "$OUT_FILE"

echo "[pg-slow-capture] writing to $OUT_FILE"
echo "[pg-slow-capture] source=pg-fs since=$SINCE"
echo "[pg-slow-capture] stop with Ctrl-C"

fs_compose logs \
  --no-color \
  --timestamps \
  --follow \
  --since "$SINCE" \
  pg-fs 2>&1 | tee -a "$OUT_FILE"
