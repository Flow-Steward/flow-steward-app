#!/usr/bin/env bash
# Candidate-backed read probes for release groups A/E.
# Source-test references are intentionally forbidden here: every PASS must be
# derived from the packaged application selected by FS_SMOKE_BASE.
set -uo pipefail

GROUP="${1:?group required}"
shift
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
FS="${FS:-$SCRIPT_DIR/fsapi.sh}"
BASE="${FS_SMOKE_BASE:-https://localhost:8099}"
SEMANTIC_ASSERTIONS="$SCRIPT_DIR/semantic_assertions.py"
SEMANTIC_RUNNER="$SCRIPT_DIR/candidate_semantic_checks.py"
FAILED=0

probe_path() {
  case "$1" in
    A05|A07|E13) printf '/health/ready' ;;
    A14) printf '/api/auth/service-accounts?per_page=1' ;;
    A18) printf '/api/dashboard/summary?range=7d' ;;
    E06) printf '/api/tools?per_page=1&search=kb' ;;
    *) return 1 ;;
  esac
}

semantic_script_path() {
  case "$1" in
    A06) printf '%s' "${FS_SMOKE_EXTENSION_HEALTH_SCRIPT:-$SEMANTIC_RUNNER}" ;;
    A13) printf '%s' "${FS_SMOKE_DATASET_INPUT_SCRIPT:-$SEMANTIC_RUNNER}" ;;
    A19) printf '%s' "${FS_SMOKE_TRACE_REACTION_SCRIPT:-$SEMANTIC_RUNNER}" ;;
    A20) printf '%s' "${FS_SMOKE_EXECUTION_ENVELOPE_SCRIPT:-$SEMANTIC_RUNNER}" ;;
    A21) printf '%s' "${FS_SMOKE_WORKFLOW_REUSE_SCRIPT:-$SEMANTIC_RUNNER}" ;;
    A22) printf '%s' "${FS_SMOKE_SCHEDULE_LOCK_SCRIPT:-$SEMANTIC_RUNNER}" ;;
    A23) printf '%s' "${FS_SMOKE_HITL_SCRIPT:-$SEMANTIC_RUNNER}" ;;
    A24) printf '%s' "${FS_SMOKE_PUBLIC_MCP_SCRIPT:-$SEMANTIC_RUNNER}" ;;
    A25) printf '%s' "${FS_SMOKE_CALLBACK_SCRIPT:-$SEMANTIC_RUNNER}" ;;
    A26) printf '%s' "${FS_SMOKE_GUARDRAIL_SCRIPT:-$SEMANTIC_RUNNER}" ;;
    A27) printf '%s' "${FS_SMOKE_POPULATED_DELETE_SCRIPT:-$SEMANTIC_RUNNER}" ;;
    A29) printf '%s' "$SEMANTIC_RUNNER" ;;
    *) return 1 ;;
  esac
}

run_semantic_script() {
  local check_id="$1" script output
  script="$(semantic_script_path "$check_id" 2>/dev/null || true)"
  if [ -z "$script" ] || [ ! -x "$script" ]; then
    printf '%s FAIL required candidate semantic script is absent or not executable\n' "$check_id"
    return 1
  fi
  case "$script" in
    /tmp/*|/private/tmp/*)
      printf '%s FAIL semantic script must be a durable release input\n' "$check_id"
      return 1
      ;;
  esac
  output="$($script "$check_id" 2>/dev/null || true)"
  if CHECK_ID="$check_id" CANDIDATE_COMMIT="${FS_SMOKE_CANDIDATE_COMMIT:-}" \
    OUTPUT="$output" python3 - <<'PY' >/dev/null 2>&1
import json, os

payload = json.loads(os.environ["OUTPUT"])
assert payload.get("check_id") == os.environ["CHECK_ID"]
assert payload.get("candidate_commit") == os.environ["CANDIDATE_COMMIT"]
assert payload.get("passed") is True
assertions = payload.get("assertions")
assert isinstance(assertions, list) and assertions
assert all(isinstance(row, dict) and row.get("passed") is True for row in assertions)
PY
  then
    printf '%s PASS candidate=%s semantic-script=%s\n' "$check_id" "$BASE" "$script"
    return 0
  fi
  printf '%s FAIL candidate=%s semantic evidence is invalid or incomplete\n' "$check_id" "$BASE"
  return 1
}

for check_id in "$@"; do
  if [ "$GROUP" = E ]; then
    case "$check_id" in
      E09|E10|E11|E12)
        printf '%s FAIL deploy check must be executed by the deployment runner\n' "$check_id"
        FAILED=1
        continue
        ;;
    esac
  fi
  if semantic_script_path "$check_id" >/dev/null 2>&1; then
    if ! run_semantic_script "$check_id"; then
      FAILED=1
    fi
    continue
  fi
  path="$(probe_path "$check_id" 2>/dev/null || true)"
  if [ -z "$path" ]; then
    printf '%s FAIL no packaged-candidate probe is declared\n' "$check_id"
    FAILED=1
    continue
  fi
  response="$($FS GET "$path" 2>/dev/null || true)"
  code="$(printf '%s' "$response" | grep -o '__HTTP_[0-9]*__' | tail -1 | tr -cd '0-9')"
  body="${response%%__HTTP_*}"
  semantic_error=""
  if [[ "$code" == 2?? ]]; then
    semantic_error="$(printf '%s' "$body" | python3 "$SEMANTIC_ASSERTIONS" "$check_id" 2>&1 || true)"
  fi
  if [[ "$code" == 2?? ]] && [ -z "$semantic_error" ]
  then
    printf '%s PASS candidate=%s path=%s HTTP=%s semantic_assertion=passed\n' \
      "$check_id" "$BASE" "$path" "$code"
  else
    printf '%s FAIL candidate=%s path=%s HTTP=%s semantic_assertion=%s\n' \
      "$check_id" "$BASE" "$path" "${code:-000}" "${semantic_error:-request-failed}"
    FAILED=1
  fi
done

exit "$FAILED"
