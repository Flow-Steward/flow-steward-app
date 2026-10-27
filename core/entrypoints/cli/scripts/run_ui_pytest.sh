#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
# shellcheck source=core/entrypoints/cli/scripts/docker_compose_env.sh
source "${ROOT_DIR}/core/entrypoints/cli/scripts/docker_compose_env.sh"

if [[ $# -eq 0 ]]; then
  cat <<'EOF'
Usage:
  core/entrypoints/cli/scripts/run_ui_pytest.sh tests/ui/test_workflow_builder_contracts.py -q

Runs pytest inside `fs-test-runner`, captures the full log on the host, then prints
the tail after completion. This avoids long-lived streamed output becoming the
source of flaky local SIGKILL/137 failures during heavy Playwright suites.
EOF
  exit 1
fi

timestamp="$(date +%Y%m%d_%H%M%S)_$$"
host_log_dir="${ROOT_DIR}/logs/ui"
host_log_file="${host_log_dir}/pytest_ui_${timestamp}.log"

mkdir -p "${host_log_dir}"

printf -v pytest_args ' %q' "$@"

# Playwright drives the app through a real browser, so the page origin must be a
# "potentially trustworthy" origin or Chromium ignores the app's security
# headers (Cross-Origin-Opener-Policy) and logs a console warning that the UI
# tests treat as a runtime error. Route the browser through Traefik's HTTPS
# entrypoint (self-signed cert; the UI browser context already ignores TLS
# errors) so COOP/COEP are honored exactly as they are in CI/production.
# Exporting TEST_RUNNER_BASE_URL makes compose interpolate APP_BASE_URL for the
# fs-test-runner service (see infra/docker/docker-compose.yaml).
export TEST_RUNNER_BASE_URL="${TEST_RUNNER_BASE_URL:-https://traefik:${APP_PORT:-8099}}"

# The strict Content-Security-Policy (script-src 'self') blocks injecting
# axe-core from the CDN *or* a local file (inline injection is disallowed), so
# the accessibility gate cannot run in this runner. Honor the suite's sanctioned
# escape hatch: skip those tests instead of failing. The accessibility gate runs
# in the dedicated CI job where axe is served same-origin.
ui_extra_env=(-e "AXE_SKIP_ON_CDN_FAIL=${AXE_SKIP_ON_CDN_FAIL:-1}")

# The runner takes its role credentials from the repository .env, which names the
# development stack's pre-seeded e2e users. Pointing TEST_RUNNER_BASE_URL at a
# different deployment — a packaged release candidate, for instance — is useless
# unless the credentials can follow, otherwise every UI test skips with "UI lane
# requires pre-seeded role credentials from .env". Forward these when the caller
# sets them, so the lane can run against a deployment that has its own admin.
for ui_cred_var in \
  E2E_ADMIN_USERNAME E2E_ADMIN_PASSWORD \
  E2E_OPERATOR_USERNAME E2E_OPERATOR_PASSWORD \
  E2E_VIEWER_USERNAME E2E_VIEWER_PASSWORD \
  E2E_UI_ROLE; do
  if [[ -n "${!ui_cred_var:-}" ]]; then
    ui_extra_env+=(-e "${ui_cred_var}=${!ui_cred_var}")
  fi
done

set +e
fs_compose run --rm -T "${ui_extra_env[@]}" fs-test-runner sh -lc "python -m pytest${pytest_args}" \
  >"${host_log_file}" 2>&1
status=$?
set -e

echo "Saved full UI pytest log to ${host_log_file}"
echo "Last 200 lines:"
tail -n 200 "${host_log_file}" || true

exit "${status}"
