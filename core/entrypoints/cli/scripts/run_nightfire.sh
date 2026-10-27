#!/bin/bash
# Run the staged Nightfire regression extension from the host machine.
#
# Usage:
#   core/entrypoints/cli/scripts/run_nightfire.sh
#   TEST_PROJECT_UI_ID=1 core/entrypoints/cli/scripts/run_nightfire.sh --fail-fast
#   core/entrypoints/cli/scripts/run_nightfire.sh --project-id 1 --with-cutover-smoke
#   core/entrypoints/cli/scripts/run_nightfire.sh --project-id 1 --with-full-ui

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
# shellcheck source=core/entrypoints/cli/scripts/docker_compose_env.sh
source "${ROOT_DIR}/core/entrypoints/cli/scripts/docker_compose_env.sh"
DOTENV_FILE="${ROOT_DIR}/${FS_ENV_FILE:-.env}"

read_dotenv_value() {
  local key="$1"
  local line=""

  if [[ ! -f "${DOTENV_FILE}" ]]; then
    return 1
  fi

  line="$(grep -E "^[[:space:]]*${key}=" "${DOTENV_FILE}" | tail -n 1 || true)"
  if [[ -z "${line}" ]]; then
    return 1
  fi

  line="${line#*=}"
  line="$(printf '%s' "${line}" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"

  if [[ "${line}" == \"*\" && "${line}" == *\" ]]; then
    line="${line:1:${#line}-2}"
  elif [[ "${line}" == \'*\' && "${line}" == *\' ]]; then
    line="${line:1:${#line}-2}"
  fi

  printf '%s' "${line}"
}

resolve_project_id() {
  local lane="$1"
  local project_id=""

  case "${lane}" in
    ui)
      project_id="${TEST_PROJECT_UI_ID:-}"
      if [[ -z "${project_id}" ]]; then
        project_id="$(read_dotenv_value TEST_PROJECT_UI_ID || true)"
      fi
      ;;
    backend)
      project_id="${TEST_PROJECT_BACKEND_ID:-}"
      if [[ -z "${project_id}" ]]; then
        project_id="$(read_dotenv_value TEST_PROJECT_BACKEND_ID || true)"
      fi
      ;;
  esac

  printf '%s' "${project_id}"
}

LANE="${NIGHTFIRE_LANE:-ui}"
PROJECT_ID=""
AUTH_ROLE="${NIGHTFIRE_AUTH_ROLE:-}"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lane)
      LANE="${2:-}"
      shift 2
      ;;
    --project-id)
      PROJECT_ID="${2:-}"
      shift 2
      ;;
    --auth-role)
      AUTH_ROLE="${2:-}"
      shift 2
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="$(resolve_project_id "$LANE")"
fi

if ! fs_compose ps --status running fs-app >/dev/null 2>&1; then
  echo "[run_nightfire] fs-app is not running; starting it now"
  fs_compose up -d fs-app
fi

if ! fs_compose ps --status running fs-test-runner >/dev/null 2>&1; then
  echo "[run_nightfire] fs-test-runner is not running; starting it now"
  fs_compose --profile test up -d fs-test-runner
fi

echo "[run_nightfire] lane=$LANE project_id=${PROJECT_ID:-auto} auth_role=${AUTH_ROLE:-auto}"

DOCKER_ENV_ARGS=(
  -e NIGHTFIRE_LANE="$LANE"
  -e COMPOSE_PROJECT_NAME="$(basename "$ROOT_DIR")"
)

if [[ -n "$PROJECT_ID" ]]; then
  if [[ "$LANE" == "ui" ]]; then
    DOCKER_ENV_ARGS+=(-e TEST_PROJECT_UI_ID="$PROJECT_ID")
  else
    DOCKER_ENV_ARGS+=(-e TEST_PROJECT_BACKEND_ID="$PROJECT_ID")
  fi
fi

if [[ -n "$AUTH_ROLE" ]]; then
  DOCKER_ENV_ARGS+=(-e NIGHTFIRE_AUTH_ROLE="$AUTH_ROLE")
fi

PY_ARGS=(
  --lane "$LANE"
)

if [[ -n "$PROJECT_ID" ]]; then
  PY_ARGS+=(--project-id "$PROJECT_ID")
fi

if [[ -n "$AUTH_ROLE" ]]; then
  PY_ARGS+=(--auth-role "$AUTH_ROLE")
fi

fs_compose exec -T \
  "${DOCKER_ENV_ARGS[@]}" \
  fs-app \
  python -m core.entrypoints.cli testing nightfire-regression \
  "${PY_ARGS[@]}" \
  ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
