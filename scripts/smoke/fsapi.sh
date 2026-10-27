#!/usr/bin/env bash
# One-shot authenticated API request used by smoke groups.
set -uo pipefail
METHOD="${1:?method required}"
PATH_PART="${2:?path required}"
BODY="${3:-}"
BASE="${FS_SMOKE_BASE:-https://localhost:8099}"
ACCOUNT="${FS_SMOKE_ACCOUNT:-00000000-0000-0000-0000-000000000002}"
EMAIL="${FS_SMOKE_EMAIL:?set FS_SMOKE_EMAIL}"
PASSWORD="${FS_SMOKE_PASSWORD:?set FS_SMOKE_PASSWORD}"
JAR="$(mktemp)"
trap 'rm -f -- "$JAR"' EXIT

CSRF=$(curl -sk -c "$JAR" -X POST "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' -H "Origin: $BASE" \
  --data-binary "$(python3 - "$EMAIL" "$PASSWORD" "$ACCOUNT" <<'PY'
import json, sys
print(json.dumps({"email": sys.argv[1], "password": sys.argv[2], "account_id": sys.argv[3]}))
PY
)" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("csrf_token", ""))' 2>/dev/null)
[ -n "$CSRF" ] || { printf '__HTTP_401__\n'; exit 0; }

args=(-sk -b "$JAR" -X "$METHOD" "$BASE$PATH_PART" -H "Origin: $BASE" -H "x-csrf-token: $CSRF")
[ -z "$BODY" ] || args+=(-H 'Content-Type: application/json' --data-binary "$BODY")
curl "${args[@]}" -w '__HTTP_%{http_code}__'
