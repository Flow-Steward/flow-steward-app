#!/usr/bin/env bash
# Derive one finalized candidate's smoke inputs and run safe or release verification.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
MODE="${1:-}"
case "$MODE" in
  --safe|--release) ;;
  *) printf 'usage: %s [--safe|--release]\n' "$0" >&2; exit 2 ;;
esac

CANDIDATE_COMMIT="${FS_RELEASE_CANDIDATE_COMMIT:-$(git -C "$REPO_ROOT" rev-parse HEAD^{commit})}"
case "$CANDIDATE_COMMIT" in
  *[!0-9a-f]*|'') printf 'candidate commit must be a full lowercase Git SHA\n' >&2; exit 2 ;;
esac
[ "${#CANDIDATE_COMMIT}" -eq 40 ] || {
  printf 'candidate commit must be a full lowercase Git SHA\n' >&2
  exit 2
}
if [ "$MODE" = --release ] && git -C "$REPO_ROOT" symbolic-ref -q HEAD >/dev/null 2>&1; then
  printf 'release verification requires an exact detached checkout\n' >&2
  exit 2
fi
RELEASE_DIR="${FS_RELEASE_DIR:-$REPO_ROOT/.flow-steward/release/$CANDIDATE_COMMIT}"
CANDIDATE_ENV="$RELEASE_DIR/candidate.env"
PRODUCTION_ENV="$RELEASE_DIR/production.env"
PROVENANCE="$RELEASE_DIR/candidate-provenance.json"
SOURCE_MANIFEST="$RELEASE_DIR/candidate-source.json"
SMOKE_RUNNER="${FS_RELEASE_SMOKE_RUNNER:-$REPO_ROOT/scripts/smoke/release-smoke.sh}"
RUN_ID="${FS_RELEASE_RUN_ID:-verify-$(printf '%s' "$CANDIDATE_COMMIT" | cut -c1-12)-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
[ -r "$CANDIDATE_ENV" ] || { printf 'candidate env is missing: %s\n' "$CANDIDATE_ENV" >&2; exit 2; }
[ -r "$PRODUCTION_ENV" ] || { printf 'candidate production env is missing: %s\n' "$PRODUCTION_ENV" >&2; exit 2; }
[ -r "$PROVENANCE" ] || { printf 'candidate provenance is missing: %s\n' "$PROVENANCE" >&2; exit 2; }
[ -r "$SOURCE_MANIFEST" ] || { printf 'candidate source identity is missing: %s\n' "$SOURCE_MANIFEST" >&2; exit 2; }
[ -x "$SMOKE_RUNNER" ] || { printf 'candidate smoke runner is not executable: %s\n' "$SMOKE_RUNNER" >&2; exit 2; }

python3 "$REPO_ROOT/scripts/release/verify_candidate_provenance.py" \
  --root "$REPO_ROOT" \
  --provenance "$PROVENANCE" \
  --source-manifest "$SOURCE_MANIFEST" \
  --candidate-commit "$CANDIDATE_COMMIT" \
  --verify-images

candidate_env_value() {
  awk -F= -v key="$1" '
    $1 == key { value = substr($0, index($0, "=") + 1) }
    END { print value }
  ' "$CANDIDATE_ENV"
}

production_env_value() {
  awk -F= -v key="$1" '
    $1 == key { value = substr($0, index($0, "=") + 1) }
    END { print value }
  ' "$PRODUCTION_ENV"
}

bootstrap_enabled="$(production_env_value FS_AUTH_BOOTSTRAP_ENABLED)"
bootstrap_token="$(production_env_value FS_AUTH_BOOTSTRAP_TOKEN)"
bootstrap_expiry="$(production_env_value FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT)"
if [ "$bootstrap_enabled" != 0 ] || [ -n "$bootstrap_token" ] || [ -n "$bootstrap_expiry" ]; then
  printf 'candidate bootstrap is not finalized; disable bootstrap and clear temporary token state before verification\n' >&2
  exit 2
fi

recorded_commit="$(candidate_env_value FS_BUILD_SHA)"
[ "$recorded_commit" = "$CANDIDATE_COMMIT" ] || {
  printf 'candidate env commit mismatch: expected %s, got %s\n' \
    "$CANDIDATE_COMMIT" "${recorded_commit:-missing}" >&2
  exit 2
}
app_port="$(candidate_env_value APP_PORT)"
case "$app_port" in ''|*[!0-9]*) printf 'candidate APP_PORT is missing or invalid\n' >&2; exit 2 ;; esac
mailpit_port="$(candidate_env_value MAILPIT_HTTP_PORT)"
case "$mailpit_port" in
  ''|*[!0-9]*) printf 'candidate MAILPIT_HTTP_PORT is missing or invalid\n' >&2; exit 2 ;;
esac

provenance_values="$(python3 - "$PROVENANCE" "$CANDIDATE_COMMIT" <<'PY'
import json
import re
import sys

path, expected_commit = sys.argv[1:]
payload = json.load(open(path, encoding="utf-8"))
if payload.get("candidate_commit") != expected_commit:
    raise SystemExit("candidate provenance commit mismatch")
digests = payload.get("image_digests")
if not isinstance(digests, dict):
    raise SystemExit("candidate provenance image_digests are missing")
ordered = []
for service in ("fs-app", "fs-worker", "control-plane", "fs-cli", "fs-mcp"):
    digest = str(digests.get(service) or "").lower()
    if not digest:
        continue
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SystemExit(f"candidate provenance digest is invalid: {service}")
    if digest not in ordered:
        ordered.append(digest)
for required in ("fs-app", "fs-cli", "fs-mcp"):
    if not str(digests.get(required) or ""):
        raise SystemExit(f"candidate provenance digest is missing: {required}")
print(",".join(ordered))
PY
)" || exit 2

FS_SMOKE_COMPOSE_PROJECT="fs-candidate-$(printf '%s' "$CANDIDATE_COMMIT" | cut -c1-12)"
container_for() {
  service="$1"
  containers="$(docker ps \
    --filter "label=com.docker.compose.project=$FS_SMOKE_COMPOSE_PROJECT" \
    --filter "label=com.docker.compose.service=$service" \
    --filter status=running --format '{{.Names}}')"
  count="$(printf '%s\n' "$containers" | awk 'NF { count += 1 } END { print count + 0 }')"
  [ "$count" -eq 1 ] || {
    printf 'candidate service must have exactly one running container: %s count=%s\n' \
      "$service" "$count" >&2
    return 2
  }
  printf '%s\n' "$containers"
}

FS_SMOKE_APP_CONTAINER="$(container_for fs-app)"
FS_SMOKE_CLI_CONTAINER="$(container_for fs-cli)"
FS_SMOKE_PG_CONTAINER="$(container_for pg-fs)"
FS_SMOKE_WORKER_CONTAINER="$(container_for fs-worker)"
FS_SMOKE_MAILPIT_CONTAINER="$(container_for mailpit)"
FS_SMOKE_RABBITMQ_CONTAINER="$(container_for rabbitmq-fs)"
container_for fs-mcp >/dev/null
container_for control-plane >/dev/null

for protected_name in FS_SMOKE_EMAIL FS_SMOKE_PASSWORD FS_SMOKE_BEARER_TOKEN \
  FS_SMOKE_MODEL_PROVIDER FS_SMOKE_MODEL; do
  [ -n "${!protected_name:-}" ] || {
    printf 'protected candidate verification input is missing: %s\n' "$protected_name" >&2
    exit 2
  }
done
case "$FS_SMOKE_MODEL_PROVIDER" in custom:*) ;; *)
  printf 'FS_SMOKE_MODEL_PROVIDER must be an account-scoped custom route\n' >&2
  exit 2
;; esac

FS_SMOKE_BASE="https://localhost:$app_port"
FS_SMOKE_INTERNAL_BASE="http://fs-app:8099"
FS_SMOKE_MAILPIT_BASE="http://localhost:$mailpit_port"
FS_SMOKE_DB_HOST="pg-fs"
FS_SMOKE_DB_PASSWORD="$(production_env_value POSTGRES_PASSWORD)"
FS_SMOKE_PG_PORT="5432"
[ -n "$FS_SMOKE_DB_PASSWORD" ] || {
  printf 'candidate POSTGRES_PASSWORD is missing or empty\n' >&2
  exit 2
}
FS_SMOKE_IMAGE_DIGESTS="$provenance_values"
FS_SMOKE_CANDIDATE_COMMIT="$CANDIDATE_COMMIT"
FS_SMOKE_ISOLATED=1
FS_SMOKE_MCP_CA_FILE="${FS_SMOKE_MCP_CA_FILE:-$REPO_ROOT/.ssl/cert.pem}"
[ -r "$FS_SMOKE_MCP_CA_FILE" ] || {
  printf 'candidate MCP CA file is not readable: %s\n' "$FS_SMOKE_MCP_CA_FILE" >&2
  exit 2
}
FS_SMOKE_UPGRADE_SCRIPT="$REPO_ROOT/scripts/release/candidate_upgrade.sh"
FS_SMOKE_ROLLBACK_SCRIPT="$REPO_ROOT/scripts/release/candidate_rollback.sh"
FS_SMOKE_REBUILD_SCRIPT="$REPO_ROOT/scripts/release/candidate_rebuild.sh"
FS_SMOKE_RESTART_SCRIPT="$REPO_ROOT/scripts/release/candidate_restart.sh"
export FS_SMOKE_BASE FS_SMOKE_INTERNAL_BASE FS_SMOKE_MAILPIT_BASE FS_SMOKE_DB_HOST
export FS_SMOKE_DB_PASSWORD FS_SMOKE_PG_PORT
export FS_SMOKE_IMAGE_DIGESTS FS_SMOKE_CANDIDATE_COMMIT FS_SMOKE_COMPOSE_PROJECT
export FS_SMOKE_APP_CONTAINER FS_SMOKE_CLI_CONTAINER FS_SMOKE_PG_CONTAINER
export FS_SMOKE_WORKER_CONTAINER FS_SMOKE_MAILPIT_CONTAINER FS_SMOKE_RABBITMQ_CONTAINER
export FS_SMOKE_ISOLATED FS_SMOKE_MCP_CA_FILE FS_SMOKE_UPGRADE_SCRIPT
export FS_SMOKE_ROLLBACK_SCRIPT FS_SMOKE_REBUILD_SCRIPT FS_SMOKE_RESTART_SCRIPT

if [ "$MODE" = --safe ]; then
  FS_SMOKE_PROFILES=oss,tenant
  FS_SMOKE_DEPLOY=0
  FS_SMOKE_VERIFY_CLEAN_EXIT=1
  export FS_SMOKE_PROFILES FS_SMOKE_DEPLOY FS_SMOKE_VERIFY_CLEAN_EXIT
  verify_args=(
    python3 -m core.entrypoints.cli.ops.candidate_harness verify
    --root "$REPO_ROOT"
    --candidate "$CANDIDATE_COMMIT"
    --release-dir "$RELEASE_DIR"
    --run-id "$RUN_ID"
    --mode safe
    --runner "$SMOKE_RUNNER"
  )
  if [ -n "${FS_RELEASE_EVIDENCE_ROOT:-}" ]; then
    verify_args+=(--evidence-root "$FS_RELEASE_EVIDENCE_ROOT")
  fi
  "${verify_args[@]}"
  exit $?
fi

[ "${FS_SMOKE_DEPLOY:-0}" = 1 ] || {
  printf 'release verification requires explicit FS_SMOKE_DEPLOY=1\n' >&2
  exit 2
}
[ -n "${FS_SMOKE_PROJECT_ID:-}" ] || {
  printf 'release verification requires explicit FS_SMOKE_PROJECT_ID\n' >&2
  exit 2
}
verify_args=(
  python3 -m core.entrypoints.cli.ops.candidate_harness verify
  --root "$REPO_ROOT"
  --candidate "$CANDIDATE_COMMIT"
  --release-dir "$RELEASE_DIR"
  --run-id "$RUN_ID"
  --mode release
  --runner "$SMOKE_RUNNER"
)
if [ -n "${FS_RELEASE_EVIDENCE_ROOT:-}" ]; then
  verify_args+=(--evidence-root "$FS_RELEASE_EVIDENCE_ROOT")
fi
"${verify_args[@]}"
