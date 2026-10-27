#!/usr/bin/env bash
# core/entrypoints/cli/scripts/ci_pytest.sh
#
# Run the full tests/ suite under CI, deselecting tests listed in
# tests/CI_EXCLUDES.txt.  This is the canonical entrypoint for CI test runs;
# do NOT bypass it with a hand-curated allowlist of test files.
#
# Usage:
#   core/entrypoints/cli/scripts/ci_pytest.sh                          # run full suite, apply excludes
#   core/entrypoints/cli/scripts/ci_pytest.sh --only-excluded          # run ONLY the excluded tests (report mode)
#   core/entrypoints/cli/scripts/ci_pytest.sh -- tests/unit            # restrict to a subdirectory
#   core/entrypoints/cli/scripts/ci_pytest.sh --help
#
# Environment:
#   PYTHON_BIN                python interpreter (default: python3)
#   CI_EXCLUDE_FILE           exclude list path (default: tests/CI_EXCLUDES.txt)
#   CI_PYTEST_EXTRA_ARGS      additional args appended before the target
#
# Exit codes:
#   0   success
#   1   pytest reported failures
#   2   CLI or environment error

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
# shellcheck source=core/entrypoints/cli/scripts/docker_compose_env.sh
source "${ROOT_DIR}/core/entrypoints/cli/scripts/docker_compose_env.sh"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CI_EXCLUDE_FILE="${CI_EXCLUDE_FILE:-${ROOT_DIR}/tests/CI_EXCLUDES.txt}"

mode="normal"
passthrough_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --only-excluded)
      mode="only-excluded"
      shift
      ;;
    --help|-h)
      sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    --)
      shift
      passthrough_args+=("$@")
      break
      ;;
    *)
      passthrough_args+=("$1")
      shift
      ;;
  esac
done

if [[ ! -f "${CI_EXCLUDE_FILE}" ]]; then
  echo "[ci-pytest] ERROR: exclude file not found: ${CI_EXCLUDE_FILE}" >&2
  exit 2
fi

# Parse exclude file: strip blank/comment lines. Use a portable while-read
# loop instead of `mapfile` so the script runs on bash 3.x (macOS default).
exclude_entries=()
while IFS= read -r line; do
  [[ -z "${line}" ]] && continue
  exclude_entries+=("${line}")
done < <(grep -Ev '^\s*(#|$)' "${CI_EXCLUDE_FILE}" | sed 's/[[:space:]]*$//')

# Validate that REMOVE_BY deadlines have not passed. Any expired ROOT_CAUSE
# header causes the whole run to fail — CI must never silently carry stale
# exclusions past their deadline.
today="$(date -u +%Y-%m-%d)"
expired_causes=()
while IFS= read -r line; do
  [[ "${line}" =~ ^#\ ROOT_CAUSE:\ +([A-Za-z0-9_.-]+) ]] || continue
  cause="${BASH_REMATCH[1]}"
  remove_by="$(awk -v c="${cause}" '
    $0 ~ "^# ROOT_CAUSE: +"c {found=1}
    found && $0 ~ /^# REMOVE_BY:/ { print $3; exit }
  ' "${CI_EXCLUDE_FILE}")"
  if [[ -z "${remove_by}" ]]; then
    echo "[ci-pytest] ERROR: ROOT_CAUSE ${cause} is missing REMOVE_BY" >&2
    exit 2
  fi
  if [[ "${remove_by}" < "${today}" ]]; then
    expired_causes+=("${cause} (REMOVE_BY=${remove_by})")
  fi
done < "${CI_EXCLUDE_FILE}"

if (( ${#expired_causes[@]} > 0 )); then
  echo "[ci-pytest] ERROR: expired ROOT_CAUSE entries in ${CI_EXCLUDE_FILE}:" >&2
  for e in "${expired_causes[@]}"; do
    echo "  - ${e}" >&2
  done
  echo "[ci-pytest] Resolve or extend each entry before re-running CI." >&2
  exit 2
fi

set +u
exclude_count=${#exclude_entries[@]}
set -u

if [[ "${mode}" == "only-excluded" ]]; then
  if (( exclude_count == 0 )); then
    echo "[ci-pytest] no excluded tests to run" >&2
    exit 0
  fi
  echo "[ci-pytest] REPORT MODE: running ${exclude_count} excluded tests (failures are NON-BLOCKING in this mode)"
  set +e
  set +u
  "${PYTHON_BIN}" -m pytest \
    -p no:randomly \
    --tb=short \
    ${CI_PYTEST_EXTRA_ARGS:-} \
    "${exclude_entries[@]}" \
    "${passthrough_args[@]}"
  rc=$?
  set -u
  set -e
  # Report-mode: do NOT fail the job; just surface the counts.
  if (( rc != 0 )); then
    echo "[ci-pytest] REPORT MODE: excluded tests had failures (rc=${rc}). This is informational."
  fi
  exit 0
fi

deselect_args=()
set +u
for nid in "${exclude_entries[@]}"; do
  deselect_args+=("--deselect" "${nid}")
done
if (( ${#passthrough_args[@]} == 0 )); then
  passthrough_args=("tests")
fi
set -u

echo "[ci-pytest] running full tests/ with ${exclude_count} tests deselected"
if (( ${#passthrough_args[@]} == 1 )) && [[ "${passthrough_args[0]}" == "tests" ]] && [[ -d "${ROOT_DIR}/tests/ui" ]]; then
  echo "[ci-pytest] splitting full suite into non-UI and UI pytest processes"
  set +e
  set +u
  "${PYTHON_BIN}" -m pytest \
    -p no:randomly \
    --tb=short \
    ${CI_PYTEST_EXTRA_ARGS:-} \
    "${deselect_args[@]}" \
    tests \
    --ignore=tests/ui
  non_ui_rc=$?
  "${PYTHON_BIN}" -m pytest \
    -p no:randomly \
    --tb=short \
    ${CI_PYTEST_EXTRA_ARGS:-} \
    "${deselect_args[@]}" \
    tests/ui
  ui_rc=$?
  set -u
  set -e
  if (( non_ui_rc != 0 )); then
    exit "${non_ui_rc}"
  fi
  exit "${ui_rc}"
fi
set +u
exec "${PYTHON_BIN}" -m pytest \
  -p no:randomly \
  --tb=short \
  ${CI_PYTEST_EXTRA_ARGS:-} \
  "${deselect_args[@]}" \
  "${passthrough_args[@]}"
