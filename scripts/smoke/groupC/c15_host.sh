#!/bin/sh
# C15 — is the email-change verification link built from the Host header?
# Runs INSIDE the docker network so the Host header reaches the app unmodified.
set -u
APP=${FS_SMOKE_INTERNAL_BASE:?set FS_SMOKE_INTERNAL_BASE}
EVIL=evil.attacker.example
J=/tmp/c15.jar
rm -f $J

RESP=$(curl -s -c $J -X POST "$APP/api/auth/login" \
  -H "Host: $EVIL" -H "Origin: http://$EVIL" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$2\",\"account_id\":\"00000000-0000-0000-0000-000000000002\"}")
echo "LOGIN: $(echo "$RESP" | head -c 80)"
CS=$(echo "$RESP" | sed -n 's/.*"csrf_token":"\([^"]*\)".*/\1/p')
echo "CSRF: ${CS:-<none>}"

echo "--- POST /api/users/me/email-change/request with spoofed Host ---"
curl -s -b $J -c $J -X POST "$APP/api/users/me/email-change/request" \
  -H "Host: $EVIL" -H "Origin: http://$EVIL" -H "x-csrf-token: $CS" \
  -H 'Content-Type: application/json' \
  -d "{\"new_email\":\"$3\",\"current_password\":\"$2\"}" \
  -w '\n__HTTP_%{http_code}__\n'
