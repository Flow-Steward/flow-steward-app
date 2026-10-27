#!/usr/bin/env bash
set -uo pipefail
DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec "$DIR/run_referenced_group.sh" A "$@"

