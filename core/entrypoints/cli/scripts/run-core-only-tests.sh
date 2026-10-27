#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
workspace="$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-core-only.XXXXXX")"
coverage_export_dir=""
cleanup() { rm -rf "${workspace}"; }
trap cleanup EXIT

if [[ "${1:-}" == "--coverage-export-dir" ]]; then
  if [[ $# -lt 3 || -z "${2:-}" ]]; then
    echo "--coverage-export-dir requires a destination and a command" >&2
    exit 2
  fi
  coverage_export_dir="$2"
  shift 2
fi

for path in .github core tests assets db docs examples infra locales scripts; do
  if [[ -e "${repo_root}/${path}" ]]; then
    cp -R "${repo_root}/${path}" "${workspace}/${path}"
  fi
done
# Core ships no extensions. The workspace gets the empty install root a fresh
# installation has, so code that resolves the default extensions root finds it.
mkdir -p "${workspace}/extensions"
for path in AGENTS.md CODE_OF_CONDUCT.md CONTRIBUTING.md Dockerfile \
  LICENSE NOTICE README.md SECURITY.md VERSION babel.cfg \
  cli.py eslint.config.mjs install.sh package.json package-lock.json pyproject.toml \
  pytest.ini requirements-audit.txt requirements-cli.lock requirements-cli.txt \
  requirements-mcp.lock requirements-mcp.txt \
  requirements-runtime.lock requirements-runtime.txt requirements-semgrep.txt \
  requirements.txt serve.py tailwind.config.js zap.yaml flow-steward .dockerignore .gitignore \
  .gitattributes .secrets.baseline \
  .pre-commit-config.yaml \
  .env.example .env.example.prod .env.example.ops .env.example.test \
  .env.example.memory .env.example.quality; do
  if [[ -f "${repo_root}/${path}" ]]; then
    cp "${repo_root}/${path}" "${workspace}/${path}"
  fi
done
find "${workspace}" -type d \( \
  -name .mypy_cache -o -name .pytest_cache -o -name .ruff_cache -o \
  -name __pycache__ -o -name build -o -name dist -o -name node_modules \
\) -prune -exec rm -rf {} +
if [[ -d "${repo_root}/assets/frontend/dist" ]]; then
  mkdir -p "${workspace}/assets/frontend"
  cp -R "${repo_root}/assets/frontend/dist" "${workspace}/assets/frontend/dist"
fi

test -z "$(find "${workspace}/extensions" -type f \
  ! -name extension.yaml \
  -print -quit)"
test ! -e "${workspace}/.env"
test ! -e "${workspace}/.env.prod"
test ! -e "${workspace}/.ssl"
mkdir -p "${workspace}/backups" "${workspace}/logs/coverage/unit"
test -z "$(find "${workspace}/backups" -mindepth 1 -print -quit)"

export PYTHONPATH="${workspace}"
export ENCRYPTION_KEY="${ENCRYPTION_KEY:-unit-test-encryption-key}"
# The outer quality Compose project identifies disposable gate resources, not
# the product installation exercised by unit tests. Keep it from changing
# default installation identities in proxy, doctor, backup, and restore tests.
export FS_COMPOSE_PROJECT_NAME="flow-steward"
unset COMPOSE_PROJECT_NAME
# Tests that validate shipped extension bundles may read immutable source
# artifacts explicitly without copying executable extension code into the
# isolated core-only workspace or adding it to PYTHONPATH.
export PYTEST_SOURCE_REPO_ROOT="${repo_root}"
if [[ "${1:-}" == */* && "${1}" != /* ]]; then
  command_path="${repo_root}/${1}"
  shift
  set -- "${command_path}" "$@"
fi
cd "${workspace}"
set +e
"$@"
command_status=$?
set -e

if [[ -n "${coverage_export_dir}" ]]; then
  mkdir -p "${coverage_export_dir}"
  for coverage_artifact in coverage.xml coverage.txt; do
    if [[ -f "${workspace}/logs/coverage/unit/${coverage_artifact}" ]]; then
      cp \
        "${workspace}/logs/coverage/unit/${coverage_artifact}" \
        "${coverage_export_dir}/${coverage_artifact}"
    fi
  done
fi

exit "${command_status}"
