#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
REPO_ROOT="${ROOT_DIR}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      shift
      if [[ $# -eq 0 ]]; then
        echo "sync_env_artifacts.sh: --repo-root requires a path" >&2
        exit 2
      fi
      REPO_ROOT="$1"
      ;;
    *)
      echo "sync_env_artifacts.sh: unsupported argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ -f /.dockerenv ]] || grep -qE '(docker|containerd|kubepods)' /proc/1/cgroup 2>/dev/null; then
  cd "${REPO_ROOT}"
  exec python -m core.application.tooling.ops.sync_env_artifacts sync \
    --repo-root "${REPO_ROOT}"
fi

# shellcheck source=core/entrypoints/cli/scripts/docker_compose_env.sh
source "${SCRIPT_DIR}/docker_compose_env.sh"
CONTAINER_REPO_ROOT="/app"
if [[ "$(cd -- "${REPO_ROOT}" && pwd)" != "${ROOT_DIR}" ]]; then
  CONTAINER_REPO_ROOT="/workspace"
  fs_compose \
    run --no-deps --rm -T --volume "${REPO_ROOT}:${CONTAINER_REPO_ROOT}" fs-audit \
    python -m core.application.tooling.ops.sync_env_artifacts sync \
    --repo-root "${CONTAINER_REPO_ROOT}"
  exit $?
fi
fs_compose \
  run --no-deps --rm -T fs-audit \
  python -m core.application.tooling.ops.sync_env_artifacts sync \
  --repo-root "${CONTAINER_REPO_ROOT}"
