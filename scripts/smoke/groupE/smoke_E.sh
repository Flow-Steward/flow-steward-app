#!/usr/bin/env bash
# Group E: packaged operator/deployment checks. E09-E12 execute real operations
# against the explicitly named isolated candidate; all other checks use the
# candidate-backed API probe runner.
set -uo pipefail

DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$DIR/../.." && pwd)"
FAILED=0
CLI_CONTAINER="${FS_SMOKE_CLI_CONTAINER:-}"
APP_CONTAINER="${FS_SMOKE_APP_CONTAINER:-}"
BASE="${FS_SMOKE_BASE:-https://localhost:8099}"
INTERNAL_BASE="${FS_SMOKE_INTERNAL_BASE:-http://fs-app:8099}"
CUTOVER_RUNNER="${FS_SMOKE_CUTOVER_SCRIPT:-$DIR/candidate_semantic_checks.py}"

say_pass() { printf '%s PASS %s\n' "$1" "$2"; }
say_fail() { printf '%s FAIL %s\n' "$1" "$2"; FAILED=1; }

require_durable_script() {
  local value="$1"
  [ -n "$value" ] && [ -x "$value" ] || return 1
  case "$value" in
    "$REPO_ROOT"/*) return 0 ;;
    /tmp/*|/private/tmp/*) return 1 ;;
  esac
  return 0
}

run_cli() {
  [ -n "$CLI_CONTAINER" ] || return 125
  docker exec "$CLI_CONTAINER" python -m core.entrypoints.cli "$@"
}

run_authenticated_cli() {
  [ -n "$CLI_CONTAINER" ] || return 125
  [ -n "${FS_SMOKE_BEARER_TOKEN:-}" ] || return 125
  printf '%s\n' "$FS_SMOKE_BEARER_TOKEN" | docker exec -i "$CLI_CONTAINER" \
    sh -c '
      IFS= read -r MCP_BEARER_TOKEN
      export MCP_BEARER_TOKEN
      exec python -m core.entrypoints.cli "$@"
    ' sh "$@"
}

e01() {
  local command mode
  for command in "config validate" status doctor; do
    for mode in default plain json; do
      case "$mode" in
        default) run_authenticated_cli $command ;;
        plain) run_authenticated_cli $command --plain ;;
        json) run_authenticated_cli --json --non-interactive $command ;;
      esac >/dev/null 2>&1 || {
        say_fail E01 "candidate CLI failed command='$command' mode=$mode"
        return
      }
    done
  done
  say_pass E01 "config validate, status, and doctor passed in default/plain/JSON modes"
}

e09() {
  local upgrade_script="${FS_SMOKE_UPGRADE_SCRIPT:-}"
  if ! require_durable_script "$upgrade_script"; then
    say_fail E09 "FS_SMOKE_UPGRADE_SCRIPT must name a durable executable outside temporary directories"
    return
  fi
  if "$upgrade_script" >/dev/null 2>&1; then
    say_pass E09 "candidate database upgrade completed on the isolated data plane"
  else
    say_fail E09 "candidate database upgrade failed or FS_SMOKE_CLI_CONTAINER is unavailable"
  fi
}

e10() {
  local baseline="/tmp/release-smoke-restore-parity.json"
  local rollback_script="${FS_SMOKE_ROLLBACK_SCRIPT:-}"
  if ! require_durable_script "$rollback_script"; then
    say_fail E10 "FS_SMOKE_ROLLBACK_SCRIPT must name a durable executable outside temporary directories"
    return
  fi
  if run_cli database restore-parity-capture --output "$baseline" --confirm --plain >/dev/null 2>&1 \
    && "$rollback_script" \
    && run_cli database restore-parity-verify --baseline "$baseline" --confirm --plain >/dev/null 2>&1; then
    say_pass E10 "rollback script restored the isolated data plane with parity verified"
  else
    say_fail E10 "rollback or restore-parity verification failed"
  fi
}

e11() {
  local rebuild_script="${FS_SMOKE_REBUILD_SCRIPT:-}"
  if ! require_durable_script "$rebuild_script"; then
    say_fail E11 "FS_SMOKE_REBUILD_SCRIPT must name a durable executable outside temporary directories"
    return
  fi
  if "$rebuild_script"; then
    local metadata revision image_id project
    metadata="$(docker inspect "$APP_CONTAINER" \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}|{{.Image}}|{{index .Config.Labels "com.docker.compose.project"}}' \
      2>/dev/null || true)"
    revision="${metadata%%|*}"
    metadata="${metadata#*|}"; image_id="${metadata%%|*}"; project="${metadata#*|}"
    if [ "$revision" = "${FS_SMOKE_CANDIDATE_COMMIT:-}" ] \
      && [ "$project" = "${FS_SMOKE_COMPOSE_PROJECT:-}" ] \
      && [ -n "$image_id" ]; then
      say_pass E11 "rebuild preserved commit=$revision image=$image_id project=$project"
      return
    fi
    say_fail E11 "rebuilt container provenance mismatch commit=$revision image=$image_id project=$project"
  else
    say_fail E11 "candidate rebuild script failed"
  fi
}

e12() {
  local restart_script="${FS_SMOKE_RESTART_SCRIPT:-}"
  if ! require_durable_script "$restart_script"; then
    say_fail E12 "FS_SMOKE_RESTART_SCRIPT must name a durable executable outside temporary directories"
    return
  fi
  if "$restart_script" \
    && curl -skSf "$BASE/health" >/dev/null \
    && "$CUTOVER_RUNNER" E12; then
    say_pass E12 "restart recovered and PAT/password cutover smoke completed"
  else
    say_fail E12 "restart, PAT/password session verification, or cutover smoke failed"
  fi
}

e14() {
  local stack_script="$REPO_ROOT/scripts/release/candidate_stack.sh"
  local fixture_root="$REPO_ROOT/.flow-steward/release-preflight-$$"
  local candidate_env="$fixture_root.env"
  local production_env="$fixture_root.production.env"
  local state_dir="$fixture_root.state"
  local output before after rc=0
  if [ ! -x "$stack_script" ]; then
    say_fail E14 "packaged candidate_stack.sh is unavailable"
    return
  fi
  mkdir -p "$state_dir"
  chmod 700 "$state_dir"
  printf 'FS_BUILD_SHA=%s\n' "${FS_SMOKE_CANDIDATE_COMMIT:-}" >"$candidate_env"
  printf 'SECURITY_PII_SALT=release-preflight-fixture-secret\n' >"$production_env" # pragma: allowlist secret - disposable preflight fixture
  printf 'environment = "production"\n' >"$state_dir/stack.toml"
  chmod 600 "$candidate_env" "$production_env" "$state_dir/stack.toml"
  before="$(docker ps -aq --filter "label=com.docker.compose.project=${FS_SMOKE_COMPOSE_PROJECT:-}" | sort)"
  output="$(
    FS_RELEASE_CANDIDATE_COMMIT="${FS_SMOKE_CANDIDATE_COMMIT:-}" \
    FS_RELEASE_ENV_FILE="$candidate_env" \
    FS_RELEASE_PROD_ENV="$production_env" \
    FS_CANDIDATE_STATE_DIR="$state_dir" \
      "$stack_script" up -d --no-build 2>&1
  )" || rc=$?
  after="$(docker ps -aq --filter "label=com.docker.compose.project=${FS_SMOKE_COMPOSE_PROJECT:-}" | sort)"
  rm -f -- "$candidate_env" "$production_env" "$state_dir/stack.toml"
  rmdir -- "$state_dir" 2>/dev/null || true
  if [ "$rc" = 2 ] \
    && [ "$before" = "$after" ] \
    && printf '%s' "$output" | grep -Fq 'candidate_prepare.sh database' \
    && printf '%s' "$output" | grep -Fq 'candidate_prepare.sh worker-identity' \
    && printf '%s' "$output" | grep -Fq 'candidate_stack.sh up -d --no-build' \
    && ! printf '%s' "$output" | grep -Fq 'release-preflight-fixture-secret'; then
    say_pass E14 "incomplete durable input exited 2 before candidate Compose mutation"
  else
    say_fail E14 "preflight rc=$rc containers_changed=$([ "$before" = "$after" ] && printf no || printf yes)"
  fi
}

TESTS=("$@")
[ "${#TESTS[@]}" -gt 0 ] || TESTS=(E01 E02 E03 E04 E05 E06 E07 E08 E09 E10 E11 E12 E13 E14)
for check_id in "${TESTS[@]}"; do
  case "$check_id" in
    E01) e01 ;;
    E09) e09 ;;
    E10) e10 ;;
    E11) e11 ;;
    E12) e12 ;;
    E14) e14 ;;
    *)
      "$DIR/run_referenced_group.sh" E "$check_id" || FAILED=1
      ;;
  esac
done

exit "$FAILED"
