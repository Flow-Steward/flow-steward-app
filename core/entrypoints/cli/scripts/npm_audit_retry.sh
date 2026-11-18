#!/usr/bin/env bash
# Retry npm audit only for unmistakable transient transport failures. Advisory
# findings and every non-network failure retain their original non-zero exit.
set -uo pipefail

MAX_ATTEMPTS="${FS_NPM_AUDIT_MAX_ATTEMPTS:-3}"
RETRY_DELAY="${FS_NPM_AUDIT_RETRY_DELAY_SECONDS:-2}"
case "$MAX_ATTEMPTS" in ''|*[!0-9]*) printf 'FS_NPM_AUDIT_MAX_ATTEMPTS must be a positive integer\n' >&2; exit 2 ;; esac
[ "$MAX_ATTEMPTS" -ge 1 ] || { printf 'FS_NPM_AUDIT_MAX_ATTEMPTS must be at least 1\n' >&2; exit 2; }

OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/flow-steward-npm-audit.XXXXXX")"
trap 'rm -f "$OUTPUT_FILE"' EXIT
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
EXCEPTIONS_FILE="${REPO_ROOT}/infra/tooling/quality/npm-audit-exceptions.json"

# An advisory finding passes only when every blocking advisory is an
# owner-accepted, unexpired exception for a dev-only package.
accepted_exceptions_only() {
  [ -f "$EXCEPTIONS_FILE" ] && [ -f package-lock.json ] || return 1
  # npm audit exits non-zero for any finding; only its JSON report matters here.
  npm audit --json "$@" >"$OUTPUT_FILE" 2>/dev/null || true
  python3 "${REPO_ROOT}/core/application/tooling/quality/npm_audit_exceptions.py" \
    --lock package-lock.json --exceptions "$EXCEPTIONS_FILE" <"$OUTPUT_FILE"
}

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  audit_rc=0
  npm audit "$@" >"$OUTPUT_FILE" 2>&1 || audit_rc=$?
  cat "$OUTPUT_FILE"
  if [ "$audit_rc" -eq 0 ]; then
    exit 0
  fi
  if ! grep -Eq 'EAI_AGAIN|ECONNRESET|ETIMEDOUT|ENETUNREACH' "$OUTPUT_FILE"; then
    accepted_exceptions_only "$@" && exit 0
    exit "$audit_rc"
  fi
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    exit "$audit_rc"
  fi
  printf '[lint] transient npm audit transport failure; retrying (%s/%s)\n' \
    "$attempt" "$MAX_ATTEMPTS" >&2
  sleep "$RETRY_DELAY"
  attempt=$((attempt + 1))
done
