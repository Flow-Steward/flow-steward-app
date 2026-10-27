#!/usr/bin/env bash
set -uo pipefail
DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TESTS=("$@")
[ "${#TESTS[@]}" -gt 0 ] || TESTS=(B01 B03 B04 B05 B06 B07 B08 B09 B10 B11 B12 B13 B14 B15 B16 B17 B18 B19)

if [ "${FS_SMOKE_ISOLATED:-0}" != 1 ]; then
  for check_id in "${TESTS[@]}"; do
    printf '%s SKIP isolated audit data plane is required\n' "$check_id"
  done
  exit 0
fi

: "${FS_SMOKE_PG_CONTAINER:?set FS_SMOKE_PG_CONTAINER to the isolated candidate PostgreSQL container}"
export FS="${FS:-$DIR/../fsapi.sh}"
export FS_SMOKE_PG_CONTAINER
exec bash "$DIR/snip.sh" "${TESTS[@]}"

