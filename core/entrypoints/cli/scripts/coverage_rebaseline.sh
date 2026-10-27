#!/usr/bin/env bash
# coverage_rebaseline.sh — reproducible coverage measurement for each scope.
#
# Usage (run from repo root or inside fs-audit container):
#
#   ./core/entrypoints/cli/scripts/coverage_rebaseline.sh --unit
#       Runs tests/unit/ with full --cov=. coverage.
#       Output: logs/coverage/unit/coverage.xml  (terminal summary also printed)
#
#   ./core/entrypoints/cli/scripts/coverage_rebaseline.sh --critical-matrix
#       Runs the tests that directly target high-risk modules.
#       Uses narrow --cov=<module> flags so the report reflects only the
#       modules we care about, not the entire project.
#       Output: logs/coverage/critical-matrix/coverage.xml  (terminal summary also printed)
#
#   ./core/entrypoints/cli/scripts/coverage_rebaseline.sh --all
#       Runs both modes in sequence.
#
# Internal:
#   ./core/entrypoints/cli/scripts/coverage_rebaseline.sh --inside --unit
#       Hidden flag used when the host delegates execution into fs-audit.
#
# Environment:
#   FS_COV_FAIL_UNDER   Minimum coverage % to pass (default: 0, off).
#   PYTHON_BIN            Python binary (default: python, falls back to python3).
#
# The script is designed to be run both on the host (via docker compose run)
# and directly inside the fs-audit container.  The paths inside the container
# are /app/... so coverage.xml source paths will contain /app — lint.sh --sonar
# normalises this automatically.
#
# Why two modes?
#   Unit-only coverage (--unit) includes all source files under --cov=. but
#   the denominator is the entire codebase, which inflates near-zero numbers for
#   untested packages.  --critical-matrix targets only the modules with tests,
#   producing coverage numbers that are actually meaningful for those files.
#   See TESTING_GAP_ANALYSIS.md §Measurement Scope for the full rationale.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
# shellcheck source=core/entrypoints/cli/scripts/docker_compose_env.sh
source "${ROOT_DIR}/core/entrypoints/cli/scripts/docker_compose_env.sh"
cd "${ROOT_DIR}"

COV_FAIL_UNDER="${FS_COV_FAIL_UNDER:-0}"
INSIDE=0

if [[ "${1:-}" == "--inside" ]]; then
  INSIDE=1
  shift
fi

# ── Docker delegation (host → fs-audit container) ──────────────────────────
# pytest-cov is installed inside fs-audit (requirements-audit.txt), not on
# the host. When run from the host, delegate to the container automatically.
# Use the hidden --inside flag to bypass delegation when the container invokes
# the same script recursively.

_is_inside_docker() {
  [[ -f /.dockerenv ]] || [[ "${INSIDE}" == "1" ]]
}

if ! _is_inside_docker; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "[coverage] ERROR: docker not found. Install Docker or run inside fs-audit." >&2
    exit 1
  fi
  echo "[coverage] Delegating to fs-audit container (pytest-cov lives there)..."
  audit_infra_started=0
  cleanup_audit_test_infra() {
    if [[ "${audit_infra_started}" -eq 1 ]]; then
      fs_compose rm -s -f pg-audit rabbitmq-audit >/dev/null 2>&1 || true
      audit_infra_started=0
    fi
  }
  trap cleanup_audit_test_infra EXIT INT TERM
  fs_compose rm -s -f pg-audit rabbitmq-audit >/dev/null 2>&1 || true
  audit_infra_started=1
  fs_compose up -d --wait pg-audit rabbitmq-audit
  fs_compose run --no-deps --rm -T fs-audit \
    python -m core.application.tooling.db.init_db
  fs_compose run --no-deps --rm -T fs-audit \
    env FS_COV_FAIL_UNDER="${COV_FAIL_UNDER}" \
    bash /app/core/entrypoints/cli/scripts/coverage_rebaseline.sh --inside "$@"
  coverage_exit_code=$?
  cleanup_audit_test_infra
  exit "${coverage_exit_code}"
fi

# ── Inside container ──────────────────────────────────────────────────────────
if command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "[coverage] ERROR: python / python3 not found in PATH." >&2
  exit 1
fi

# ── helpers ──────────────────────────────────────────────────────────────────

print_usage() {
  echo "Usage: $0 [--inside] --unit | --critical-matrix | --all"
  echo ""
  echo "  --unit             Run tests/unit/ with full project coverage."
  echo "  --critical-matrix  Run direct-target coverage for high-risk modules."
  echo "  --all              Run both modes in sequence."
}

banner() {
  echo ""
  echo "════════════════════════════════════════════════════════════════"
  echo "  $*"
  echo "════════════════════════════════════════════════════════════════"
}

run_unit() {
  banner "Coverage mode: unit"
  mkdir -p logs/coverage/unit

  "${PYTHON_BIN}" -m pytest -q tests/unit \
    --cov=. \
    --cov-report=term-missing \
    --cov-report=xml:logs/coverage/unit/coverage.xml \
    --cov-fail-under="${COV_FAIL_UNDER}"

  echo ""
  echo "[coverage] Unit report written to:"
  echo "           logs/coverage/unit/coverage.xml"
  echo "           logs/coverage/unit/coverage.xml  (terminal output above)"
}

run_critical_matrix() {
  banner "Coverage mode: critical-matrix"
  mkdir -p logs/coverage/critical-matrix

  # ── Covered modules ──────────────────────────────────────────────────────
  # Each entry is a dot-path suitable for --cov=<module>.
  # A module is included here only when a dedicated test file exists.
  # Add new entries as Workstreams 5/6 land their tests.
  #
  # Current state (2026-04-12):
  #   ✓  workflows.gate_handler              — tests/unit/test_workflows_gate_handler.py
  #   ✓  core.safe_eval                      — tests/unit/test_safe_eval.py
  #   ✓  orchestrator.routing_node           — tests/unit/test_routing_node.py          (W4)
  #   ✓  tools.integration_tools             — tests/unit/test_integration_tools.py    (W5)
  #   ✓  security.scanners.base              — tests/unit/test_scanners_base.py        (W5)
  #   ✓  security.zip_guard                  — tests/unit/test_zip_guard.py            (W5)

  local cov_modules=(
    workflows.gate_handler
    core.safe_eval
    orchestrator.routing_node
    tools.integration_tools
    security.scanners.base
    security.zip_guard
  )

  local test_files=(
    tests/unit/test_workflows_gate_handler.py
    tests/unit/test_safe_eval.py
    tests/unit/test_routing_node.py
    tests/unit/test_integration_tools.py
    tests/unit/test_scanners_base.py
    tests/unit/test_zip_guard.py
  )

  # Build --cov flags
  local cov_flags=()
  for mod in "${cov_modules[@]}"; do
    cov_flags+=(--cov="${mod}")
  done

  # Filter to only test files that exist
  local existing_tests=()
  for tf in "${test_files[@]}"; do
    if [[ -f "${tf}" ]]; then
      existing_tests+=("${tf}")
    else
      echo "[coverage] WARNING: test file not found, skipping: ${tf}" >&2
    fi
  done

  if [[ ${#existing_tests[@]} -eq 0 ]]; then
    echo "[coverage] ERROR: no critical-matrix test files found." >&2
    exit 1
  fi

  "${PYTHON_BIN}" -m pytest -q "${existing_tests[@]}" \
    "${cov_flags[@]}" \
    --cov-report=term-missing \
    --cov-report=xml:logs/coverage/critical-matrix/coverage.xml \
    --cov-fail-under="${COV_FAIL_UNDER}"

  echo ""
  echo "[coverage] Critical-matrix report written to:"
  echo "           logs/coverage/critical-matrix/coverage.xml"
  echo "           logs/coverage/critical-matrix/coverage.xml  (terminal output above)"
  echo ""
  echo "[coverage] Modules covered in this report:"
  for mod in "${cov_modules[@]}"; do
    echo "           • ${mod}"
  done
  echo ""
  echo "[coverage] All W4 + W5 modules covered. Next: Workstream 6 (phantom test fixes)."
}

# ── main ─────────────────────────────────────────────────────────────────────

if [[ $# -eq 0 ]]; then
  print_usage
  exit 1
fi

MODE="${1}"
case "${MODE}" in
  --unit)
    run_unit
    ;;
  --critical-matrix)
    run_critical_matrix
    ;;
  --all)
    run_unit
    run_critical_matrix
    ;;
  --help|-h)
    print_usage
    exit 0
    ;;
  *)
    echo "[coverage] Unknown mode: ${MODE}" >&2
    print_usage
    exit 1
    ;;
esac
