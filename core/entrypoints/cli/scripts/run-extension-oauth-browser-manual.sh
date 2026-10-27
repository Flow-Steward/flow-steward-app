#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
BASE_COMPOSE="${ROOT_DIR}/infra/docker/docker-compose.yaml"
OAUTH_COMPOSE="${ROOT_DIR}/infra/docker/docker-compose.oauth-browser.yaml"
MANUAL_COMPOSE="${ROOT_DIR}/infra/docker/docker-compose.oauth-browser-manual.yaml"
PROJECT_NAME="${FS_COMPOSE_PROJECT_NAME:-}"
MANUAL_PORT="${FS_OAUTH_MANUAL_PORT:-}"
PROVIDER_PORT="${FS_OAUTH_MANUAL_PROVIDER_PORT:-}"
action="${1:-}"

if [[ ! "${PROJECT_NAME}" =~ ^fs-oauth-generic-manual-[a-z0-9-]+$ ]]; then
  echo "Set a unique FS_COMPOSE_PROJECT_NAME matching fs-oauth-generic-manual-*." >&2
  exit 2
fi
if [[ ! "${MANUAL_PORT}" =~ ^[0-9]+$ || "${MANUAL_PORT}" -lt 1024 || "${MANUAL_PORT}" -gt 65535 ]]; then
  echo "Set FS_OAUTH_MANUAL_PORT to an unused port from 1024 through 65535." >&2
  exit 2
fi
if [[ ! "${PROVIDER_PORT}" =~ ^[0-9]+$ || "${PROVIDER_PORT}" -lt 1024 || "${PROVIDER_PORT}" -gt 65535 || "${PROVIDER_PORT}" == "${MANUAL_PORT}" ]]; then
  echo "Set FS_OAUTH_MANUAL_PROVIDER_PORT to a different unused port from 1024 through 65535." >&2
  exit 2
fi
export FS_OAUTH_PROVIDER_INTERNAL_PORT="${PROVIDER_PORT}"
export FS_OAUTH_PROVIDER_BASE_URL="https://oauth.127.0.0.1.nip.io:${PROVIDER_PORT}"

compose=(
  docker compose --project-name "${PROJECT_NAME}"
  --file "${BASE_COMPOSE}"
  --file "${OAUTH_COMPOSE}"
  --file "${MANUAL_COMPOSE}"
)

remove_images() {
  local repository
  local image_ids=()
  for repository in app audit frontend; do
    while IFS= read -r image_id; do
      [[ -n "${image_id}" ]] && image_ids+=("${image_id}")
    done < <(docker image ls --quiet --filter "reference=${PROJECT_NAME}-${repository}:*")
  done
  if [[ ${#image_ids[@]} -gt 0 ]]; then
    docker image rm "${image_ids[@]}" >/dev/null
  fi
}

if [[ "${action}" == "down" ]]; then
  export FS_OAUTH_ACCEPTANCE_APP_IMAGE="${PROJECT_NAME}-app:cleanup"
  export FS_OAUTH_ACCEPTANCE_AUDIT_IMAGE="${PROJECT_NAME}-audit:cleanup"
  export FS_OAUTH_ACCEPTANCE_FRONTEND_IMAGE="${PROJECT_NAME}-frontend:cleanup"
  "${compose[@]}" down --volumes --remove-orphans
  remove_images
  test -z "$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT_NAME}")"
  test -z "$(docker network ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}")"
  test -z "$(docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}")"
  exit 0
fi
if [[ "${action}" == "ca" ]]; then
  export FS_OAUTH_ACCEPTANCE_APP_IMAGE="${PROJECT_NAME}-app:manual"
  export FS_OAUTH_ACCEPTANCE_AUDIT_IMAGE="${PROJECT_NAME}-audit:manual"
  export FS_OAUTH_ACCEPTANCE_FRONTEND_IMAGE="${PROJECT_NAME}-frontend:manual"
  "${compose[@]}" cp oauth-cert-init:/certs/ca.crt "/tmp/${PROJECT_NAME}-ca.crt"
  printf '/tmp/%s-ca.crt\n' "${PROJECT_NAME}"
  openssl x509 -in "/tmp/${PROJECT_NAME}-ca.crt" -noout -fingerprint -sha256
  exit 0
fi
if [[ "${action}" != "up" ]]; then
  echo "Usage: run-extension-oauth-browser-manual.sh up|ca|down" >&2
  exit 2
fi
if [[ -n "$(git -C "${ROOT_DIR}" status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "Manual Chrome audit requires a clean committed worktree." >&2
  exit 2
fi

revision="$(git -C "${ROOT_DIR}" rev-parse --short=16 HEAD)"
export FS_OAUTH_ACCEPTANCE_APP_IMAGE="${PROJECT_NAME}-app:${revision}"
export FS_OAUTH_ACCEPTANCE_AUDIT_IMAGE="${PROJECT_NAME}-audit:${revision}"
export FS_OAUTH_ACCEPTANCE_FRONTEND_IMAGE="${PROJECT_NAME}-frontend:${revision}"

cleanup_failure() {
  "${compose[@]}" logs --no-color oauth-db-upgrade >&2 || true
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  remove_images || true
}
trap cleanup_failure EXIT INT TERM

"${compose[@]}" config --format json | python3 -c '
import json, sys
config = json.load(sys.stdin)
started = {"extension-dependencies", "frontend-build", "fs-app", "fs-mcp", "oauth-app-volumes-init", "oauth-browser-seed", "oauth-cert-init", "oauth-db-schema-init", "oauth-db-upgrade", "oauth-provider", "pg-fs", "rabbitmq-fs", "traefik"}
ports = config["services"]["traefik"].get("ports", [])
assert len(ports) == 1
port = ports[0]
assert port["host_ip"] == "127.0.0.1" and str(port["published"]) == __import__("os").environ["FS_OAUTH_MANUAL_PORT"]
provider_ports = config["services"]["oauth-provider"].get("ports", [])
assert len(provider_ports) == 1
provider_port = provider_ports[0]
assert provider_port["host_ip"] == "127.0.0.1" and str(provider_port["published"]) == __import__("os").environ["FS_OAUTH_MANUAL_PROVIDER_PORT"]
for name in started:
    service = config["services"][name]
    if name not in {"traefik", "oauth-provider"}:
        assert not service.get("ports")
    assert all(mount.get("type") != "bind" for mount in service.get("volumes", []))
'
docker build --network=none --pull=false --target app-runtime \
  --build-arg "BUILD_SHA=${revision}" --tag "${FS_OAUTH_ACCEPTANCE_APP_IMAGE}" \
  --file "${ROOT_DIR}/infra/docker/Dockerfile" "${ROOT_DIR}"
docker build --network=none --pull=false --target audit \
  --build-arg "BUILD_SHA=${revision}" --tag "${FS_OAUTH_ACCEPTANCE_AUDIT_IMAGE}" \
  --file "${ROOT_DIR}/infra/docker/Dockerfile" "${ROOT_DIR}"
docker build --network=none --pull=false --target frontend-development \
  --build-arg "BUILD_SHA=${revision}" --tag "${FS_OAUTH_ACCEPTANCE_FRONTEND_IMAGE}" \
  --file "${ROOT_DIR}/infra/docker/Dockerfile" "${ROOT_DIR}"
"${compose[@]}" up --detach --no-build --pull never oauth-browser-seed
seed_wait_status=0
seed_wait_output="$("${compose[@]}" wait oauth-browser-seed)" || seed_wait_status=$?
seed_exit_code="$(printf '%s\n' "${seed_wait_output}" | tail -n 1)"
"${SCRIPT_DIR}/check-oauth-browser-exit.sh" "${seed_wait_status}" "${seed_exit_code}"

trap - EXIT INT TERM
printf 'Ready URL: https://flow-steward.localhost:%s/app/extensions/fixture.extension-oauth\n' "${MANUAL_PORT}"
printf 'Operator: oauth-operator@example.test / OAuthAcceptanceOperator_123!\n'
printf 'Viewer: oauth-viewer@example.test / OAuthAcceptanceViewer_123!\n'
printf 'Export CA: FS_COMPOSE_PROJECT_NAME=%q FS_OAUTH_MANUAL_PORT=%q FS_OAUTH_MANUAL_PROVIDER_PORT=%q %q ca\n' "${PROJECT_NAME}" "${MANUAL_PORT}" "${PROVIDER_PORT}" "$0"
printf 'Cleanup: FS_COMPOSE_PROJECT_NAME=%q FS_OAUTH_MANUAL_PORT=%q FS_OAUTH_MANUAL_PROVIDER_PORT=%q %q down\n' "${PROJECT_NAME}" "${MANUAL_PORT}" "${PROVIDER_PORT}" "$0"
