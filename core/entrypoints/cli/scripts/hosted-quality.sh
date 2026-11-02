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
      echo 'Update this branch from origin/main, resolve conflicts and push a new candidate.' >&2
      exit 1
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
    if [[ -n "${remaining}" ]]; then
      echo 'Owned quality containers remain after cleanup.' >&2
      exit 1
    fi
    ;;
  *)
    echo 'Usage: hosted-quality.sh check-candidate|prepare|diagnostics|cleanup' >&2
    exit 2
    ;;
esac
