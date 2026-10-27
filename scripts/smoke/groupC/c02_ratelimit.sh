#!/usr/bin/env bash
# C02 — the login rate limiter must lock the abused USERNAME, not every user
# sharing the client IP (office NAT / reverse proxy).
#
# Usage: c02_ratelimit.sh <victim_email> <victim_password> <spray_email>
# Exits 0 = PASS (unrelated user can still log in), 1 = FAIL (IP-wide lockout).
set -uo pipefail
BASE=${BASE:-https://localhost:8099}
ACCT=00000000-0000-0000-0000-000000000002
VICTIM="$1"; VPASS="$2"; SPRAY="${3:-regc-spray-target@regc.test}"
PG_CONTAINER="${FS_SMOKE_PG_CONTAINER:?set FS_SMOKE_PG_CONTAINER}"
PSQL() { docker exec "$PG_CONTAINER" psql -U flow_steward -d flow_steward -tAc "$1"; }

login() { # email pass -> http code
  curl -sk -o /dev/null -w '%{http_code}' -X POST "$BASE/api/auth/login" \
    -H 'Content-Type: application/json' -H "Origin: $BASE" \
    -d "{\"email\":\"$1\",\"password\":\"$2\",\"account_id\":\"$ACCT\"}"
}

echo "[setup] clearing auth_rate_limits"
PSQL "DELETE FROM auth_rate_limits;" >/dev/null

echo "[1] control: victim logs in cleanly -> $(login "$VICTIM" "$VPASS")"
PSQL "DELETE FROM auth_rate_limits;" >/dev/null

echo "[2] spraying 6 bad passwords at a DIFFERENT username ($SPRAY)"
for i in 1 2 3 4 5 6; do printf '    attempt %d -> %s\n' "$i" "$(login "$SPRAY" "definitely-wrong-$i")"; done

echo "[3] rate-limit table state:"
PSQL "SELECT key, jsonb_array_length(attempts_jsonb), coalesce(locked_until::text,'-') FROM auth_rate_limits ORDER BY key;" | sed 's/^/    /'

CODE=$(login "$VICTIM" "$VPASS")
echo "[4] UNRELATED user with CORRECT password from the same IP -> HTTP $CODE"

echo "[cleanup] clearing auth_rate_limits"
PSQL "DELETE FROM auth_rate_limits;" >/dev/null

if [ "$CODE" = "200" ]; then echo "C02 PASS"; exit 0; fi
echo "C02 FAIL — shared-IP lockout denied an unrelated valid login"; exit 1
