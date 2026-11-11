#!/usr/bin/env bash
set -euo pipefail

# Cleanup is intentionally unavailable on developer machines and persistent
# self-hosted runners. GitHub destroys this job's VM even after forced teardown.
if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_ENVIRONMENT:-}" != github-hosted \
   || "${GITHUB_REPOSITORY:-}" != Flow-Steward/flow-steward-app \
   || ! "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ \
   || ! "${GITHUB_RUN_ATTEMPT:-}" =~ ^[0-9]+$ \
   || ! "${GITHUB_JOB:-}" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo 'This adapter requires an identified GitHub-hosted quality job.' >&2
  exit 2
fi

cd "${GITHUB_WORKSPACE:?}"
project="fs-quality-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${GITHUB_JOB}"
compose=(docker compose -p "${project}" -f infra/docker/docker-compose.yaml
  -f infra/docker/docker-compose.quality-hosted.yaml)

case "${1:-}" in
  check-candidate)
    if [[ "$(git rev-parse HEAD)" != "${GITHUB_SHA:?}" \
       || -n "$(git status --porcelain --untracked-files=all)" ]]; then
      echo 'Wrong or dirty candidate checkout; refusing verification.' >&2
      exit 1
    fi
    git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main
    if ! git merge-base --is-ancestor refs/remotes/origin/main HEAD; then
      main_sha="$(git rev-parse --short refs/remotes/origin/main)"
      case "${QUALITY_MODE:-release}" in
        release|release-sonar)
          # Release evidence must describe the exact commit that will ship.
          echo "::error::main is now ${main_sha} and ${GITHUB_SHA:0:7} does not include it (another pull request was merged). Release evidence must include main. Update this branch from origin/main, resolve conflicts and push a new candidate." >&2
          exit 1
          ;;
        *)
          # A branch or single-check result still describes this commit. Failing
          # it whenever any other pull request lands threw away green runs; the
          # pull request itself shows whether the branch is behind main.
          echo "::warning::main is now ${main_sha} and ${GITHUB_SHA:0:7} does not include it. This ${QUALITY_MODE} result describes this branch only; update from main before a release run."
          exit 0
          ;;
      esac
    fi
    printf 'Clean candidate %s includes main %s\n' "${GITHUB_SHA}" "$(git rev-parse origin/main)"
    ;;
  prepare)
    cp .env.example .env
    mkdir -p logs/quality/hosted "${RUNNER_TEMP:?}/fs-quality-packages"
    {
      printf 'FS_STACK_ENV_FILE=/dev/null\n'
      printf 'FS_COMPOSE_PROJECT_NAME=%s\n' "${project}"
      printf 'FS_COMPOSE_FILES=infra/docker/docker-compose.yaml infra/docker/docker-compose.quality-hosted.yaml\n'
      printf 'FS_QUALITY_CACHE_ROOT=%s/fs-quality-packages\n' "${RUNNER_TEMP}"
      printf 'SONAR_IMAGE=sonarqube:26.9.0.129388-community\n'
    } >> "${GITHUB_ENV:?}"
    printf 'audit_digest=%s\n' "$(bash core/entrypoints/cli/scripts/lint.sh --audit-toolchain-digest)" >> "${GITHUB_OUTPUT:?}"
    printf 'audit_image=%s-fs-audit:latest\n' "${project}" >> "${GITHUB_OUTPUT}"
    df -h .
    ;;
  prepare-postgres)
    # pg-audit consumes the same image as pg-fs but has no build definition.
    # Build the canonical image explicitly on clean runners; do not start pg-fs.
    "${compose[@]}" build pg-fs
    ;;
  diagnostics)
    mkdir -p logs/quality/hosted
    "${compose[@]}" ps --all > logs/quality/hosted/containers.txt
    "${compose[@]}" logs --no-color sonarqube > logs/quality/hosted/sonarqube.log 2>&1
    docker system df > logs/quality/hosted/docker-disk.txt
    df -h . > logs/quality/hosted/host-disk.txt
    ;;
  cleanup)
    # Exact run identity; no prune, no prefix matching, no shared cache volume.
    "${compose[@]}" --profile audit --profile quality down --volumes --remove-orphans
    remaining="$(docker ps -aq --filter "label=com.docker.compose.project=${project}")"
    # Interrupted `compose run --rm` one-offs can outlive `down`. Record exact
    # IDs and recheck ownership before removing each, including on cancellation.
    while IFS= read -r container_id; do
      [[ -n "${container_id}" ]] || continue
      if ! owner="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "${container_id}")"; then
        continue # Auto-removal may have completed since the inventory.
      fi
      if [[ "${owner}" != "${project}" ]]; then
        echo "Refusing cleanup of foreign container ${container_id}." >&2
        exit 1
      fi
      printf 'Removing owned leftover container %s (%s)\n' "${container_id}" "${owner}"
      if ! docker rm -f "${container_id}"; then
        # Auto-removal can race this call too. The final inventory, including
        # daemon errors, is authoritative; a retained container still fails.
        echo "Rechecking removal of ${container_id}." >&2
      fi
    done <<< "${remaining}"
    remaining="$(docker ps -aq --filter "label=com.docker.compose.project=${project}")"
    if [[ -n "${remaining}" ]]; then
      printf 'Owned quality containers remain after cleanup:\n%s\n' "${remaining}" >&2
      exit 1
    fi
    ;;
  prepare-artifacts)
    # Atomic evidence files are deliberately 0600. Hand only this job's
    # generated outputs back to the host UID after container work has stopped.
    for output in logs/quality logs/coverage "${RUNNER_TEMP:?}/fs-quality-packages"; do
      if [[ -d "${output}" ]]; then
        sudo chown -R "$(id -u):$(id -g)" "${output}"
      fi
    done
    ;;
  *)
    echo 'Usage: hosted-quality.sh check-candidate|prepare|prepare-postgres|diagnostics|cleanup|prepare-artifacts' >&2
    exit 2
    ;;
esac
