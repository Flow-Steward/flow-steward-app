#!/usr/bin/env bash
# Shared fail-closed checks for destructive or stateful candidate operations.

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
CANDIDATE_STACK="$SCRIPT_DIR/candidate_stack.sh"

require_candidate_context() {
  if [ "${FS_SMOKE_ISOLATED:-}" != "1" ]; then
    printf 'FS_SMOKE_ISOLATED=1 is required for candidate operations\n' >&2
    return 2
  fi

  CANDIDATE_COMMIT="${FS_SMOKE_CANDIDATE_COMMIT:-}"
  COMPOSE_PROJECT="${FS_SMOKE_COMPOSE_PROJECT:-}"
  if ! printf '%s' "$CANDIDATE_COMMIT" | grep -Eq '^[0-9a-f]{40}$'; then
    printf 'FS_SMOKE_CANDIDATE_COMMIT must be a full lowercase Git commit\n' >&2
    return 2
  fi
  expected_project="fs-candidate-$(printf '%s' "$CANDIDATE_COMMIT" | cut -c1-12)"
  if [ "$COMPOSE_PROJECT" != "$expected_project" ]; then
    printf 'FS_SMOKE_COMPOSE_PROJECT must equal %s\n' "$expected_project" >&2
    return 2
  fi

  actual_commit="$(git -C "$REPO_ROOT" rev-parse HEAD^{commit})"
  if [ "$actual_commit" != "$CANDIDATE_COMMIT" ]; then
    printf 'checkout commit %s does not match candidate %s\n' "$actual_commit" "$CANDIDATE_COMMIT" >&2
    return 2
  fi
  if git -C "$REPO_ROOT" symbolic-ref -q HEAD >/dev/null 2>&1; then
    printf 'candidate operations require an exact detached checkout\n' >&2
    return 2
  fi

  export FS_RELEASE_CANDIDATE_COMMIT="$CANDIDATE_COMMIT"
  export FS_RELEASE_COMPOSE_PROJECT="$COMPOSE_PROJECT"
}

assert_container_project() {
  container="$1"
  [ -n "$container" ] || { printf 'candidate container name is required\n' >&2; return 2; }
  actual_project="$(docker inspect "$container" \
    --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)"
  if [ "$actual_project" != "$COMPOSE_PROJECT" ]; then
    printf 'container %s belongs to project %s, expected %s\n' \
      "$container" "${actual_project:-<unknown>}" "$COMPOSE_PROJECT" >&2
    return 2
  fi
}

candidate_stack() {
  "$CANDIDATE_STACK" "$@"
}

candidate_writer_services='fs-app fs-worker control-plane fs-mcp fs-cli'
original_services=''
maintenance_services=''
maintenance_active=0

begin_candidate_maintenance() {
  [ "$maintenance_active" -eq 0 ] || {
    printf 'candidate maintenance barrier is already active\n' >&2
    return 2
  }
  original_services="$(candidate_stack ps --services --filter status=running)"
  maintenance_services=''
  for service in $candidate_writer_services; do
    if printf '%s\n' "$original_services" | grep -Fxq "$service"; then
      maintenance_services="${maintenance_services}${maintenance_services:+ }$service"
    fi
  done
  if [ -n "$maintenance_services" ]; then
    # shellcheck disable=SC2086
    candidate_stack stop $maintenance_services >/dev/null
  fi
  remaining_writers="$(candidate_stack ps --services --filter status=running | \
    grep -E '^(fs-app|fs-worker|control-plane|fs-mcp|fs-cli)$' || true)"
  if [ -n "$remaining_writers" ]; then
    printf 'candidate maintenance barrier failed; writers remain active: %s\n' \
      "$(printf '%s' "$remaining_writers" | tr '\n' ' ')" >&2
    restore_candidate_services
    return 1
  fi
  maintenance_active=1
}

restore_candidate_services() {
  if [ -n "$maintenance_services" ]; then
    # Compose emitted these names for this exact project before maintenance.
    # shellcheck disable=SC2086
    candidate_stack up -d --no-build $maintenance_services >/dev/null
  fi
  maintenance_active=0
}

candidate_env_value() {
  key="$1"
  env_file="$2"
  awk -v expected="$key" '
    index($0, expected "=") == 1 { value = substr($0, length(expected) + 2) }
    END { print value }
  ' "$env_file"
}

restore_candidate_images() {
  release_dir="$REPO_ROOT/.flow-steward/release/$CANDIDATE_COMMIT"
  candidate_env="$release_dir/candidate.env"
  image_archive="$release_dir/candidate-images.tar"
  image_manifest="$release_dir/candidate-images.json"
  [ -r "$candidate_env" ] || {
    printf 'candidate env is missing: %s\n' "$candidate_env" >&2
    return 2
  }

  app_image="$(candidate_env_value FS_APP_IMAGE "$candidate_env")"
  worker_image="$(candidate_env_value FS_WORKER_IMAGE "$candidate_env")"
  control_image="$(candidate_env_value FS_CONTROL_IMAGE "$candidate_env")"
  cli_image="$(candidate_env_value FS_CLI_IMAGE "$candidate_env")"
  mcp_image="$(candidate_env_value MCP_IMAGE "$candidate_env")"
  postgres_image="$(candidate_env_value POSTGRES_IMAGE "$candidate_env")"
  python3 "$REPO_ROOT/scripts/release/candidate_image_archive.py" verify \
    --archive "$image_archive" \
    --manifest "$image_manifest" \
    --candidate-commit "$CANDIDATE_COMMIT" \
    --image "fs-app=$app_image" \
    --image "fs-worker=$worker_image" \
    --image "control-plane=$control_image" \
    --image "fs-cli=$cli_image" \
    --image "fs-mcp=$mcp_image" \
    --image "postgres=$postgres_image" >/dev/null

  missing_archive_image=0
  for image in "$app_image" "$worker_image" "$control_image" \
    "$cli_image" "$mcp_image" "$postgres_image"; do
    if ! docker image inspect "$image" >/dev/null 2>&1; then
      missing_archive_image=1
    fi
  done
  if [ "$missing_archive_image" -eq 1 ]; then
    docker image load --input "$image_archive" >/dev/null
  fi
  for image in "$app_image" "$worker_image" "$control_image" \
    "$cli_image" "$mcp_image" "$postgres_image"; do
    docker image inspect "$image" >/dev/null 2>&1 || {
      printf 'candidate image archive did not restore recorded image: %s\n' "$image" >&2
      return 2
    }
  done

  provenance_file="$release_dir/candidate-provenance.json"
  infrastructure_images="$(python3 - "$provenance_file" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
images = payload.get("infrastructure_images") or {}
required = ("traefik", "rabbitmq", "mailpit", "seaweedfs")
missing = [name for name in required if not str(images.get(name) or "").strip()]
if missing:
    raise SystemExit(f"candidate provenance lacks infrastructure images: {', '.join(missing)}")
print("\n".join(str(images[name]).strip() for name in required))
PY
)" || return 2
  for image in $infrastructure_images; do
    docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image" >/dev/null
  done
}

preflight_candidate_operation() {
  release_dir="$REPO_ROOT/.flow-steward/release/$CANDIDATE_COMMIT"
  provenance_file="$release_dir/candidate-provenance.json"
  [ -r "$provenance_file" ] || {
    printf 'candidate provenance is missing: %s\n' "$provenance_file" >&2
    return 2
  }
  grep -Fq "\"candidate_commit\": \"$CANDIDATE_COMMIT\"" "$provenance_file" || {
    printf 'candidate provenance does not match commit %s\n' "$CANDIDATE_COMMIT" >&2
    return 2
  }
  source_manifest="$release_dir/candidate-source.json"
  [ -r "$source_manifest" ] || {
    printf 'candidate source identity is missing: %s\n' "$source_manifest" >&2
    return 2
  }
  if ! python3 "$REPO_ROOT/scripts/release/verify_candidate_provenance.py" \
    --root "$REPO_ROOT" \
    --provenance "$provenance_file" \
    --source-manifest "$source_manifest" \
    --candidate-commit "$CANDIDATE_COMMIT" \
    --verify-images
  then
    printf 'candidate complete provenance does not match release inputs for %s\n' \
      "$CANDIDATE_COMMIT" >&2
    return 2
  fi
  tls_cert_input="${FS_RELEASE_TLS_CERT:-$REPO_ROOT/.ssl/cert.pem}"
  tls_key_input="${FS_RELEASE_TLS_KEY:-$REPO_ROOT/.ssl/key.pem}"
  for tls_input in "$tls_cert_input" "$tls_key_input"; do
    [ -s "$tls_input" ] || {
      printf 'candidate TLS input is missing or empty: %s\n' "$tls_input" >&2
      return 2
    }
  done
  candidate_stack config --quiet >/dev/null
  available_blocks="$(df -Pk "$release_dir" | awk 'NR == 2 {print $4}')"
  case "$available_blocks" in
    ''|*[!0-9]*)
      printf 'candidate disk preflight could not determine available space\n' >&2
      return 2
      ;;
  esac
  minimum_blocks="${FS_RELEASE_MIN_AVAILABLE_KB:-1048576}"
  if [ "$available_blocks" -lt "$minimum_blocks" ]; then
    printf 'candidate disk preflight failed: insufficient available space\n' >&2
    return 2
  fi
}

wait_candidate_ready() {
  candidate_base="${FS_SMOKE_BASE:-}"
  if [ -z "$candidate_base" ]; then
    candidate_env="$REPO_ROOT/.flow-steward/release/$CANDIDATE_COMMIT/candidate.env"
    [ -r "$candidate_env" ] || {
      printf 'candidate env is missing: %s\n' "$candidate_env" >&2
      return 2
    }
    app_port="$(awk -F= '$1 == "APP_PORT" { value = substr($0, index($0, "=") + 1) } END { print value }' "$candidate_env")"
    case "$app_port" in
      ''|*[!0-9]*)
        printf 'candidate APP_PORT is missing or invalid in %s\n' "$candidate_env" >&2
        return 2
        ;;
    esac
    candidate_base="https://localhost:$app_port"
  fi
  readiness_url="${candidate_base%/}/health/ready"
  ready_attempts="${FS_SMOKE_RESTART_READY_ATTEMPTS:-60}"
  case "$ready_attempts" in
    ''|*[!0-9]*|0)
      printf 'FS_SMOKE_RESTART_READY_ATTEMPTS must be a positive integer\n' >&2
      return 2
      ;;
  esac
  for ((attempt = 1; attempt <= ready_attempts; attempt++)); do
    if curl -ksSf "$readiness_url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf 'candidate did not become ready: %s\n' "$readiness_url" >&2
  return 1
}
