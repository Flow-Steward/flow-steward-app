#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
extract_browser_wait_line() {
  printf '%s\n' "$1" | tail -n 1 | tr -d '\r'
}
if [[ "${1:-}" == "--verify-exit-helper" ]]; then
  exec "${SCRIPT_DIR}/check-oauth-browser-exit.sh" 0 0
fi
if [[ "${1:-}" == "--verify-exit-extraction" ]]; then
  printf '%s' "$(extract_browser_wait_line "${2:-}")"
  exit 0
fi
BASE_COMPOSE="${ROOT_DIR}/infra/docker/docker-compose.yaml"
OAUTH_COMPOSE="${ROOT_DIR}/infra/docker/docker-compose.oauth-browser.yaml"
REQUESTED_PROJECT="${FS_COMPOSE_PROJECT_NAME:-fs-oauth-generic-20260830}"
PROJECT_NAME="${REQUESTED_PROJECT%-oauth-browser}-oauth-browser"

if [[ ! "${PROJECT_NAME}" =~ ^fs-oauth-generic-[a-z0-9-]+-oauth-browser$ ]]; then
  echo "The isolated project name must match fs-oauth-generic-*-oauth-browser." >&2
  exit 2
fi

compose=(docker compose --project-name "${PROJECT_NAME}" --file "${BASE_COMPOSE}" --file "${OAUTH_COMPOSE}")
repository_snapshot() {
  python3 -m core.application.tooling.quality.repository_snapshot \
    --repository-root "${ROOT_DIR}"
}
initial_snapshot="$(repository_snapshot)"
candidate_fingerprint="${initial_snapshot:0:16}"
export FS_OAUTH_ACCEPTANCE_APP_IMAGE="${PROJECT_NAME}-app:${candidate_fingerprint}"
export FS_OAUTH_ACCEPTANCE_AUDIT_IMAGE="${PROJECT_NAME}-audit:${candidate_fingerprint}"
export FS_OAUTH_ACCEPTANCE_FRONTEND_IMAGE="${PROJECT_NAME}-frontend:${candidate_fingerprint}"
candidate_images=(
  "${FS_OAUTH_ACCEPTANCE_APP_IMAGE}"
  "${FS_OAUTH_ACCEPTANCE_AUDIT_IMAGE}"
  "${FS_OAUTH_ACCEPTANCE_FRONTEND_IMAGE}"
)
# Cleanup may remove only resources created after this empty-start check. Even
# a stale resource from an earlier run is not owned by this invocation. Keep the
# check before installing the cleanup trap so refusal cannot delete a collision.
require_empty_inventory() {
  local resource="$1"
  local existing
  shift
  if ! existing="$("$@" 2>/dev/null)"; then
    echo "Could not inventory ${resource}; refusing OAuth browser startup." >&2
    return 1
  fi
  if [[ -n "${existing}" ]]; then
    echo "Refusing pre-existing ${resource}; OAuth browser startup requires empty resources." >&2
    return 1
  fi
}
require_empty_inventory "containers for project ${PROJECT_NAME}" \
  docker ps -aq --filter "label=com.docker.compose.project=${PROJECT_NAME}"
require_empty_inventory "networks for project ${PROJECT_NAME}" \
  docker network ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}"
require_empty_inventory "volumes for project ${PROJECT_NAME}" \
  docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}"
for candidate_image in "${candidate_images[@]}"; do
  require_empty_inventory "candidate image ${candidate_image}" \
    docker image ls --quiet --filter "reference=${candidate_image}"
done
rendered_compose=""

cleanup() {
  local attempt
  for attempt in $(seq 1 10); do
    "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
    if [[ -z "$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT_NAME}")" ]] \
      && [[ -z "$(docker network ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}")" ]] \
      && [[ -z "$(docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}")" ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}
verify_cleanup() {
  local containers networks volumes
  containers="$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT_NAME}")"
  networks="$(docker network ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}")"
  volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}")"
  if [[ -n "${containers}${networks}${volumes}" ]]; then
    echo "Isolated OAuth browser resources were not fully removed." >&2
    return 1
  fi
}
remove_candidate_images() {
  local image_name
  local removal_status=0
  for image_name in "${candidate_images[@]}"; do
    if docker image inspect "${image_name}" >/dev/null 2>&1; then
      docker image rm "${image_name}" >/dev/null 2>&1 || removal_status=$?
    fi
  done
  for image_name in "${candidate_images[@]}"; do
    if docker image inspect "${image_name}" >/dev/null 2>&1; then
      echo "Candidate image remains after OAuth browser cleanup: ${image_name}" >&2
      removal_status=1
    fi
  done
  return "${removal_status}"
}
cleanup_on_exit() {
  local cleanup_status=0
  local current_snapshot=""
  cleanup || cleanup_status=$?
  verify_cleanup || cleanup_status=$?
  if [[ -n "${rendered_compose}" ]]; then
    rm -f "${rendered_compose}"
  fi
  remove_candidate_images || cleanup_status=$?
  if ! current_snapshot="$(repository_snapshot)"; then
    echo "OAuth browser lane could not verify the repository worktree snapshot." >&2
    cleanup_status=1
  elif [[ "${current_snapshot}" != "${initial_snapshot}" ]]; then
    echo "OAuth browser lane changed the repository worktree." >&2
    cleanup_status=1
  fi
  return "${cleanup_status}"
}
trap cleanup_on_exit EXIT INT TERM

rendered_compose="$(mktemp -t flow-steward-oauth-compose.XXXXXX)"
"${compose[@]}" config --format json >"${rendered_compose}"
if ! python3 - "${rendered_compose}" <<'PY'
import json
import sys

config = json.load(open(sys.argv[1], encoding="utf-8"))
services = config["services"]

# The closure is computed, not listed. A hand-written list cannot notice a
# service the base graph adds later: `frontend-assets` was inherited through
# fs-app's dependencies, carried a host bind of the repository into this
# isolated stack, and was invisible to the check that exists to catch exactly
# that. The lane starts `oauth-browser`, so everything it can reach is in scope.
ROOT_SERVICE = "oauth-browser"
closure = set()
pending = [ROOT_SERVICE]
while pending:
    name = pending.pop()
    if name in closure or name not in services:
        continue
    closure.add(name)
    pending.extend(services[name].get("depends_on") or {})

offenders = []
for name in sorted(closure):
    service = services[name]
    if service.get("ports"):
        offenders.append(f"{name}: publishes ports")
    binds = [
        volume.get("source")
        for volume in service.get("volumes", [])
        if volume.get("type") == "bind"
    ]
    if binds:
        offenders.append(f"{name}: host bind {binds}")

if offenders:
    print(chr(10).join(offenders), file=sys.stderr)
    raise SystemExit(1)
print(f"isolation verified across {len(closure)} reachable services", file=sys.stderr)
PY
then
  rm -f "${rendered_compose}"
  echo "OAuth browser Compose rendered a host bind mount or published port." >&2
  exit 1
fi
rm -f "${rendered_compose}"
rendered_compose=""

cleanup
docker image rm "${candidate_images[@]}" >/dev/null 2>&1 || true
docker build --pull=false --target app-runtime \
  --build-arg "BUILD_SHA=${candidate_fingerprint}" \
  --tag "${FS_OAUTH_ACCEPTANCE_APP_IMAGE}" \
  --file "${ROOT_DIR}/infra/docker/Dockerfile" "${ROOT_DIR}"
docker build --pull=false --target audit \
  --build-arg "BUILD_SHA=${candidate_fingerprint}" \
  --tag "${FS_OAUTH_ACCEPTANCE_AUDIT_IMAGE}" \
  --file "${ROOT_DIR}/infra/docker/Dockerfile" "${ROOT_DIR}"
docker build --pull=false --target frontend-development \
  --build-arg "BUILD_SHA=${candidate_fingerprint}" \
  --tag "${FS_OAUTH_ACCEPTANCE_FRONTEND_IMAGE}" \
  --file "${ROOT_DIR}/infra/docker/Dockerfile" "${ROOT_DIR}"

if ! "${compose[@]}" up --detach --no-build --pull never oauth-browser; then
  # Captured before the cleanup trap removes the stack. Without this the only
  # evidence of a startup failure is "container X is unhealthy", and the run
  # that produced it no longer exists to be inspected.
  for diagnostic_service in oauth-db-schema-init oauth-db-upgrade pg-fs oauth-browser-seed fs-app traefik; do
    echo "--- ${diagnostic_service} logs ---" >&2
    "${compose[@]}" logs --no-color --tail 200 "${diagnostic_service}" >&2 || true
  done
  echo "--- health history ---" >&2
  "${compose[@]}" ps --format '{{.Name}}' 2>/dev/null | while read -r container_name; do
    [[ -n "${container_name}" ]] || continue
    docker inspect --format \
      '{{.Name}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}{{if .State.Health}} log={{range .State.Health.Log}}[exit={{.ExitCode}} {{.Output}}]{{end}}{{end}}' \
      "${container_name}" >&2 2>/dev/null || true
  done
  echo "OAuth browser dependencies failed before the browser runner started." >&2
  exit 1
fi
browser_wait_status=0
browser_wait_output="$("${compose[@]}" wait oauth-browser)" || browser_wait_status=$?
browser_exit_code="$(extract_browser_wait_line "${browser_wait_output}")"
if ! "${SCRIPT_DIR}/check-oauth-browser-exit.sh" \
  "${browser_wait_status}" "${browser_exit_code}"; then
  "${compose[@]}" logs --no-color oauth-db-upgrade >&2 || true
  "${compose[@]}" logs --no-color oauth-browser >&2 || true
  echo "OAuth browser service failed (wait=${browser_wait_status}, exit=${browser_exit_code:-missing})." >&2
  exit 1
fi
cleanup
verify_cleanup
cleanup_on_exit
trap - EXIT INT TERM
