#!/usr/bin/env bash
# Explicit, idempotent database and worker-identity preparation for a candidate.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CANDIDATE_STACK="$SCRIPT_DIR/candidate_stack.sh"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
CANDIDATE_COMMIT="${FS_RELEASE_CANDIDATE_COMMIT:-$(git -C "$REPO_ROOT" rev-parse HEAD^{commit})}"
CANDIDATE_PROD_ENV="${FS_RELEASE_PROD_ENV:-$REPO_ROOT/.flow-steward/release/$CANDIDATE_COMMIT/production.env}"
case "$CANDIDATE_PROD_ENV" in
  /*) ;;
  *) CANDIDATE_PROD_ENV="$(CDPATH= cd -- "$(dirname -- "$CANDIDATE_PROD_ENV")" && pwd)/$(basename -- "$CANDIDATE_PROD_ENV")" ;;
esac
export FS_RELEASE_PROD_ENV="$CANDIDATE_PROD_ENV"
MODE="${1:-all}"
case "$MODE" in
  all|database|worker-identity) ;;
  *)
    printf 'usage: %s [all|database|worker-identity]\n' "$0" >&2
    exit 2
    ;;
esac

candidate_stack() {
  FS_RELEASE_PREPARE_MODE=1 "$CANDIDATE_STACK" "$@"
}

wait_for_database() {
  attempts="${FS_RELEASE_DATABASE_READY_ATTEMPTS:-60}"
  case "$attempts" in
    ''|*[!0-9]*|0)
      printf 'FS_RELEASE_DATABASE_READY_ATTEMPTS must be a positive integer\n' >&2
      return 2
      ;;
  esac
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if candidate_stack exec -T pg-fs pg_isready -U "${POSTGRES_USER:-flow_steward}" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf 'candidate database did not become ready\n' >&2
  return 1
}

prepare_database() {
  candidate_stack up -d --no-build traefik rabbitmq-fs pg-fs mailpit seaweedfs
  wait_for_database
  candidate_stack run --rm --no-deps -T memory-postgres-extensions
  candidate_stack --profile cli run --rm --no-deps -T --entrypoint python fs-cli \
    -m core.entrypoints.cli --non-interactive database upgrade --author release-candidate \
    --maintenance-confirmed --confirm --plain
}

prepare_worker_identity() {
  wait_for_database
  candidate_stack run --rm --no-deps -T \
    --volume "$CANDIDATE_PROD_ENV:/tmp/flow-steward-installer.env:rw" \
    --entrypoint python fs-app \
    -m core.entrypoints.cli.ops.worker_identity_bootstrap \
    --env-file /tmp/flow-steward-installer.env
  candidate_stack --profile cli run --rm --no-deps -T --entrypoint python fs-cli \
    -m core.entrypoints.cli.ops.candidate_runtime_preflight
}

case "$MODE" in
  all)
    prepare_database
    prepare_worker_identity
    ;;
  database) prepare_database ;;
  worker-identity) prepare_worker_identity ;;
esac

printf 'candidate prerequisites prepared mode=%s; runtime services were not started\n' "$MODE"
