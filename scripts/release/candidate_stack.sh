#!/usr/bin/env bash
# Operate the candidate using only durable repo-owned Compose and environment inputs.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
COMMIT="${FS_RELEASE_CANDIDATE_COMMIT:-$(git -C "$REPO_ROOT" rev-parse HEAD^{commit})}"
ENV_FILE="${FS_RELEASE_ENV_FILE:-$REPO_ROOT/.flow-steward/release/$COMMIT/candidate.env}"
RELEASE_DIR="$REPO_ROOT/.flow-steward/release/$COMMIT"
PROD_ENV="${FS_RELEASE_PROD_ENV:-$RELEASE_DIR/production.env}"
case "$PROD_ENV" in
  /*) ;;
  *) PROD_ENV="$(CDPATH= cd -- "$(dirname -- "$PROD_ENV")" && pwd)/$(basename -- "$PROD_ENV")" ;;
esac
FS_ENV_FILE="${FS_ENV_FILE:-$PROD_ENV}"
case "$FS_ENV_FILE" in
  /*) ;;
  *) FS_ENV_FILE="$(CDPATH= cd -- "$(dirname -- "$FS_ENV_FILE")" && pwd)/$(basename -- "$FS_ENV_FILE")" ;;
esac
export FS_ENV_FILE
FS_CANDIDATE_PROD_ENV="${FS_CANDIDATE_PROD_ENV:-$PROD_ENV}"
if [ -n "${FS_CANDIDATE_STATE_DIR:-}" ]; then
  :
elif [ -r "$RELEASE_DIR/candidate-state/stack.toml" ]; then
  FS_CANDIDATE_STATE_DIR="$RELEASE_DIR/candidate-state"
elif [ -n "${FS_RELEASE_PROD_ENV:-}" ]; then
  # Compatibility for an explicitly supplied operator env. Newly built release
  # candidates always take the immutable release-owned state branch above.
  FS_CANDIDATE_STATE_DIR="$REPO_ROOT/.flow-steward"
else
  printf 'candidate state is missing: %s\n' "$RELEASE_DIR/candidate-state" >&2
  exit 2
fi
export FS_CANDIDATE_PROD_ENV FS_CANDIDATE_STATE_DIR
FS_INSTALLER_STATE_DIR="$RELEASE_DIR"
export FS_INSTALLER_STATE_DIR
COMPOSE_FILE="$REPO_ROOT/infra/docker/docker-compose.prod.yaml"
COMPOSE_WORKING_DIR="$(CDPATH= cd -- "$(dirname -- "$COMPOSE_FILE")" && pwd)"
COMPOSE_PROFILES=""
if [ -r "$FS_CANDIDATE_STATE_DIR/compose.env" ]; then
  COMPOSE_PROFILES="$(awk -F= '$1 == "FS_COMPOSE_PROFILES" { value = substr($0, index($0, "=") + 1) } END { print value }' \
    "$FS_CANDIDATE_STATE_DIR/compose.env")"
fi
case "$COMPOSE_PROFILES" in
  *[!A-Za-z0-9_\ -]*)
    printf 'candidate compose profiles are invalid: %s\n' "$COMPOSE_PROFILES" >&2
    exit 2
    ;;
esac
COMPOSE_PROFILE_ARGS=()
for profile in $COMPOSE_PROFILES; do
  COMPOSE_PROFILE_ARGS+=(--profile "$profile")
done
FS_RELEASE_COMPOSE_PROJECT="${FS_RELEASE_COMPOSE_PROJECT:-fs-candidate-$(printf '%s' "$COMMIT" | cut -c1-12)}"
export FS_RELEASE_COMPOSE_PROJECT

case "$ENV_FILE" in
  "$REPO_ROOT"/.flow-steward/*) ;;
  /tmp/*|/private/tmp/*)
    printf 'candidate env must be durable, not %s\n' "$ENV_FILE" >&2
    exit 2
    ;;
esac
[ -r "$ENV_FILE" ] || { printf 'candidate env is missing: %s\n' "$ENV_FILE" >&2; exit 2; }
[ -r "$PROD_ENV" ] || { printf 'production env is missing: %s\n' "$PROD_ENV" >&2; exit 2; }
[ -r "$FS_CANDIDATE_STATE_DIR/stack.toml" ] || {
  printf 'candidate state is missing: %s\n' "$FS_CANDIDATE_STATE_DIR" >&2
  exit 2
}

candidate_compose() {
  env \
    -u SECURITY_PII_SALT \
    -u FS_METRICS_TOKEN \
    -u FS_WORKER_MCP_BEARER_TOKEN \
    -u MCP_BEARER_TOKEN \
    -u FS_DB_DSN \
    -u LANGGRAPH_POSTGRES_URI \
    -u POSTGRES_DB \
    -u POSTGRES_USER \
    -u POSTGRES_PASSWORD \
    -u RABBITMQ_USER \
    -u RABBITMQ_PASS \
    -u RABBITMQ_URL \
    docker compose \
    --project-name "$FS_RELEASE_COMPOSE_PROJECT" \
    --env-file "$PROD_ENV" \
    --env-file "$ENV_FILE" \
    --file "$COMPOSE_FILE" \
    ${COMPOSE_PROFILE_ARGS[@]+"${COMPOSE_PROFILE_ARGS[@]}"} \
    "$@"
}

candidate_env_has_value() {
  env_name="$1"
  awk -F= -v key="$env_name" '
    $1 == key { value = substr($0, index($0, "=") + 1) }
    END {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if (value == "\047\047" || value == "\"\"") value = ""
      exit(value == "")
    }
  ' "$PROD_ENV"
}

candidate_file_value() {
  value_file="$1"
  value_name="$2"
  awk -F= -v key="$value_name" '
    $1 == key { value = substr($0, index($0, "=") + 1) }
    END { print value }
  ' "$value_file"
}

print_candidate_prepare_steps() {
  printf 'candidate bring-up preflight failed; run these supported commands in order:\n' >&2
  printf '  FS_RELEASE_CANDIDATE_COMMIT=%s scripts/release/candidate_prepare.sh database\n' \
    "$COMMIT" >&2
  printf '  FS_RELEASE_CANDIDATE_COMMIT=%s scripts/release/candidate_prepare.sh worker-identity\n' \
    "$COMMIT" >&2
  printf '  FS_RELEASE_CANDIDATE_COMMIT=%s scripts/release/candidate_stack.sh up -d --no-build\n' \
    "$COMMIT" >&2
}

preflight_candidate_bringup() {
  failed=0
  provenance_file="$RELEASE_DIR/candidate-provenance.json"
  if [ ! -r "$provenance_file" ]; then
    printf 'candidate provenance is missing: %s\n' "$provenance_file" >&2
    failed=1
  elif ! grep -Fq "\"candidate_commit\": \"$COMMIT\"" "$provenance_file"; then
    printf 'candidate provenance does not match commit %s\n' "$COMMIT" >&2
    failed=1
  fi

  recorded_commit="$(candidate_file_value "$ENV_FILE" FS_BUILD_SHA)"
  if [ "$recorded_commit" != "$COMMIT" ]; then
    printf 'candidate env commit %s does not match requested commit %s\n' \
      "${recorded_commit:-<missing>}" "$COMMIT" >&2
    failed=1
  fi
  for image_mapping in \
    "fs-app:FS_APP_IMAGE" \
    "fs-worker:FS_WORKER_IMAGE" \
    "control-plane:FS_CONTROL_IMAGE" \
    "fs-cli:FS_CLI_IMAGE" \
    "fs-mcp:MCP_IMAGE"; do
    service_name="${image_mapping%%:*}"
    env_name="${image_mapping#*:}"
    image_digest="$(candidate_file_value "$ENV_FILE" "$env_name")"
    if ! printf '%s\n' "$image_digest" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
      printf 'candidate image digest is missing or invalid: %s\n' "$env_name" >&2
      failed=1
      continue
    fi
    if [ ! -r "$provenance_file" ] || \
      ! grep -Fq "\"$service_name\": \"$image_digest\"" "$provenance_file"; then
      printf 'candidate provenance digest mismatch: %s\n' "$service_name" >&2
      failed=1
    elif ! docker image inspect "$image_digest" >/dev/null 2>&1; then
      printf 'candidate image is unavailable locally: %s=%s\n' \
        "$service_name" "$image_digest" >&2
      failed=1
    fi
  done

  for env_name in SECURITY_PII_SALT FS_METRICS_TOKEN FS_WORKER_MCP_BEARER_TOKEN \
    FS_DB_DSN LANGGRAPH_POSTGRES_URI POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD \
    RABBITMQ_USER RABBITMQ_PASS RABBITMQ_URL; do
    if ! candidate_env_has_value "$env_name"; then
      printf 'candidate required input is missing or empty: %s in %s\n' \
        "$env_name" "$PROD_ENV" >&2
      failed=1
    fi
  done

  tls_cert_input="${FS_RELEASE_TLS_CERT:-$REPO_ROOT/.ssl/cert.pem}"
  tls_key_input="${FS_RELEASE_TLS_KEY:-$REPO_ROOT/.ssl/key.pem}"
  for required_file in "$tls_cert_input" "$tls_key_input"; do
    if [ ! -s "$required_file" ]; then
      printf 'candidate required file is missing or empty: %s\n' "$required_file" >&2
      failed=1
    fi
  done
  if printf '%s\n' "$COMPOSE_PROFILES" | grep -qw storage-seaweedfs && \
    [ ! -s "$RELEASE_DIR/runtime/seaweedfs-s3.json" ]; then
    printf 'candidate storage identity is missing: %s\n' \
      "$RELEASE_DIR/runtime/seaweedfs-s3.json" >&2
    failed=1
  fi
  extensions_dir="$(candidate_file_value "$ENV_FILE" FS_EXTENSIONS_DIR)"
  if [ -z "$extensions_dir" ] || [ ! -d "$extensions_dir" ]; then
    printf 'candidate materialized extensions are missing: %s\n' \
      "${extensions_dir:-<missing>}" >&2
    failed=1
  fi
  if [ "$failed" -ne 0 ]; then
    print_candidate_prepare_steps
    return 2
  fi

  candidate_compose config --quiet >/dev/null || {
    printf 'candidate Compose configuration is invalid\n' >&2
    print_candidate_prepare_steps
    return 2
  }
  project_containers="$(docker ps -aq \
    --filter "label=com.docker.compose.project=$FS_RELEASE_COMPOSE_PROJECT")"
  for container_id in $project_containers; do
    owner_root="$(docker inspect "$container_id" --format \
      '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
    owner_config="$(docker inspect "$container_id" --format \
      '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
    if [ "$owner_root" != "$COMPOSE_WORKING_DIR" ] || [ "$owner_config" != "$COMPOSE_FILE" ]; then
      printf 'candidate Compose project %s is owned by %s with config %s\n' \
        "$FS_RELEASE_COMPOSE_PROJECT" "${owner_root:-<unknown>}" \
        "${owner_config:-<unknown>}" >&2
      print_candidate_prepare_steps
      return 2
    fi
  done
  pg_container="$(candidate_compose ps --status running -q pg-fs)"
  if [ -z "$pg_container" ]; then
    printf 'candidate database service is not running for project %s\n' \
      "$FS_RELEASE_COMPOSE_PROJECT" >&2
    print_candidate_prepare_steps
    return 2
  fi
  if ! candidate_compose --profile cli run --rm --no-deps -T --entrypoint python fs-cli \
    -m core.entrypoints.cli.ops.candidate_runtime_preflight; then
    print_candidate_prepare_steps
    return 2
  fi
}

compose_command=""
skip_option_value=0
for argument in "$@"; do
  if [ "$skip_option_value" -eq 1 ]; then
    skip_option_value=0
    continue
  fi
  case "$argument" in
    --profile|-f|--file|--project-name|--env-file)
      skip_option_value=1
      ;;
    --profile=*|--file=*|--project-name=*|--env-file=*|-*) ;;
    *)
      compose_command="$argument"
      break
      ;;
  esac
done
if [ "${FS_RELEASE_PREPARE_MODE:-0}" != "1" ]; then
  case "$compose_command" in
    up|start|restart) preflight_candidate_bringup ;;
  esac
fi

candidate_compose "$@"
