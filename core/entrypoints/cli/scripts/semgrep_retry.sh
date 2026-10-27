#!/usr/bin/env bash
# Retry Semgrep only when remote rule-pack retrieval fails for an unmistakable
# transient transport reason. Findings and configuration errors stay red.
set -uo pipefail

MAX_ATTEMPTS="${FS_SEMGREP_MAX_ATTEMPTS:-3}"
RETRY_DELAY="${FS_SEMGREP_RETRY_DELAY_SECONDS:-2}"
case "$MAX_ATTEMPTS" in
  ''|*[!0-9]*) printf 'FS_SEMGREP_MAX_ATTEMPTS must be a positive integer\n' >&2; exit 2 ;;
esac
[ "$MAX_ATTEMPTS" -ge 1 ] || {
  printf 'FS_SEMGREP_MAX_ATTEMPTS must be at least 1\n' >&2
  exit 2
}
[ "$#" -gt 0 ] || {
  printf 'semgrep_retry.sh requires a command\n' >&2
  exit 2
}

OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/flow-steward-semgrep.XXXXXX")"
trap 'rm -f "$OUTPUT_FILE"' EXIT

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  semgrep_rc=0
  "$@" >"$OUTPUT_FILE" 2>&1 || semgrep_rc=$?
  cat "$OUTPUT_FILE"
  if [ "$semgrep_rc" -eq 0 ]; then
    exit 0
  fi
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ] \
    || ! grep -Eq 'EAI_AGAIN|ECONNRESET|ETIMEDOUT|ENETUNREACH|NameResolutionError|Temporary failure in name resolution|Failed to resolve|Could not resolve host' "$OUTPUT_FILE"; then
    exit "$semgrep_rc"
  fi
  printf '[lint] transient Semgrep transport failure; retrying (%s/%s)\n' \
    "$attempt" "$MAX_ATTEMPTS" >&2
  sleep "$RETRY_DELAY"
  attempt=$((attempt + 1))
done
