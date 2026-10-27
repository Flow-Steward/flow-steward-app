#!/bin/sh
set -eu

printf '%s\n' "Flow Steward CLI: preparing management runtime..." >&2

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/../../../.." && pwd)
FS_DOCKER_HOST_PROJECT_ROOT=$root_dir
export FS_DOCKER_HOST_PROJECT_ROOT
if [ -n "${FS_INSTALLER_STATE_DIR:-}" ]; then
  case "$FS_INSTALLER_STATE_DIR" in
    /*) ;;
    *)
      printf '%s\n' "FS_INSTALLER_STATE_DIR must be an absolute directory path." >&2
      exit 2
      ;;
  esac
  if [ ! -d "$FS_INSTALLER_STATE_DIR" ]; then
    printf '%s\n' "FS_INSTALLER_STATE_DIR does not exist or is not a directory." >&2
    exit 2
  fi
  state_dir=$(CDPATH= cd -- "$FS_INSTALLER_STATE_DIR" 2>/dev/null && pwd -P) || exit 2
else
  state_dir="$root_dir/.flow-steward"
fi
state_env="$state_dir/compose.env"
log_dir="$state_dir/logs"
secret_input_dir="$state_dir/secret-input"
runtime_dir="$state_dir/runtime"
runtime_identity_helper="$root_dir/core/infrastructure/cli_runtime/runtime_identity.sh"
repair_lock_dir="$runtime_dir/cli-runtime.lock"
repair_lock_owned=0
runtime_log=""
# A plain doctor writes nothing to the installation (the doctor task, condition 1): its
# configuration section keeps its transient files in a directory the doctor made for it.
scratch_dir=$runtime_dir
if [ "${FS_DOCTOR_SECTION:-}" = "configuration" ] && [ -d "${FS_DOCTOR_SCRATCH_DIR:-}" ]; then
  scratch_dir=$FS_DOCTOR_SCRATCH_DIR
fi
runtime_capture="$scratch_dir/cli-runtime-$$.capture"
# The inner configuration run of `doctor` exits with this when the launcher itself could
# not run the checks (no Docker, run as root, no CLI runtime): the outer doctor then says
# so the way doctor always has, since the installation checks need the same Docker.
doctor_blocked_code=9
compact_mode=0
cli_host_bindings=0
cli_artifact_binding=0

trim_value() {
  value=$1
  while :; do
    case "$value" in
      [[:space:]]*) value=${value#?} ;;
      *) break ;;
    esac
  done
  while :; do
    case "$value" in
      *[[:space:]]) value=${value%?} ;;
      *) break ;;
    esac
  done
  printf '%s' "$value"
}

strip_quotes() {
  value=$1
  case "$value" in
    \"*\")
      value=${value#\"}
      value=${value%\"}
      ;;
    \'*\')
      value=${value#\'}
      value=${value%\'}
      ;;
  esac
  printf '%s' "$value"
}

resolve_repo_path() {
  case "$1" in
    /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$root_dir" "$1" ;;
  esac
}

normalize_absolute_path() {
  path_value=$1
  path_base=${2:-$root_dir}
  case "$path_value" in
    /*) absolute_path=$path_value ;;
    *) absolute_path="$path_base/$path_value" ;;
  esac

  if [ -d "$absolute_path" ]; then
    CDPATH= cd -- "$absolute_path" 2>/dev/null && pwd -P
    return
  fi

  path_parent=$(dirname -- "$absolute_path")
  path_name=$(basename -- "$absolute_path")
  if [ -d "$path_parent" ]; then
    normalized_parent=$(CDPATH= cd -- "$path_parent" 2>/dev/null && pwd -P) || return 1
    printf '%s/%s' "$normalized_parent" "$path_name"
    return
  fi

  printf '%s\n' "$absolute_path" | awk -F/ '
    {
      count = 0
      for (field_index = 1; field_index <= NF; field_index += 1) {
        if ($field_index == "" || $field_index == ".") {
          continue
        }
        if ($field_index == "..") {
          if (count > 0) {
            count -= 1
          }
          continue
        }
        parts[++count] = $field_index
      }
      if (count == 0) {
        printf "/"
        next
      }
      for (field_index = 1; field_index <= count; field_index += 1) {
        printf "/%s", parts[field_index]
      }
    }
  '
}

normalize_inspected_mounts() {
  mount_tab=$(printf '\t')
  while IFS="$mount_tab" read -r mount_type mount_source mount_destination mount_rw; do
    [ -n "$mount_type" ] || continue
    case "$mount_source" in
      /host_mnt/*) mount_source=${mount_source#/host_mnt} ;;
    esac
    if [ "$mount_type" = "bind" ] && [ "$mount_destination" != "/var/run/docker.sock" ]; then
      mount_source=$(normalize_absolute_path "$mount_source" "/") || return 1
    fi
    printf '%s\t%s\t%s\t%s\n' \
      "$mount_type" "$mount_source" "$mount_destination" "$mount_rw"
  done
}

normalized_compose_paths() {
  files=${FS_COMPOSE_FILES:-infra/docker/docker-compose.yaml}
  normalized_files=""
  for compose_file in $files; do
    normalized_file=$(normalize_absolute_path "$compose_file" "$root_dir") || return 1
    if [ -z "$normalized_files" ]; then
      normalized_files=$normalized_file
    else
      normalized_files="$normalized_files:$normalized_file"
    fi
  done
  [ -n "$normalized_files" ] || return 1
  printf '%s' "$normalized_files"
}

read_active_revision() {
  revision_path="$state_dir/active-revision.json"
  if [ ! -e "$revision_path" ]; then
    printf '%s' "initial"
    return 0
  fi
  if [ ! -f "$revision_path" ] || [ -L "$revision_path" ]; then
    return 1
  fi

  awk '
    BEGIN {
      depth = 0
      in_string = 0
      escaped = 0
      invalid = 0
      revision_count = 0
      pending_revision_key = 0
      expect_revision_value = 0
      last_significant = ""
    }
    {
      if (in_string) {
        invalid = 1
      }
      for (position = 1; position <= length($0); position += 1) {
        character = substr($0, position, 1)
        if (in_string) {
          if (escaped) {
            token = token character
            escaped = 0
          } else if (character == "\\") {
            token = token character
            escaped = 1
          } else if (character == "\"") {
            in_string = 0
            if (string_role == "key") {
              pending_revision_key = (token == "revision")
            } else if (string_role == "revision_value") {
              revision = token
              revision_count += 1
            }
            last_significant = "\""
          } else {
            token = token character
          }
          continue
        }

        if (character == "\"") {
          if (pending_revision_key) {
            invalid = 1
          }
          in_string = 1
          escaped = 0
          token = ""
          if (depth == 1 && expect_revision_value) {
            string_role = "revision_value"
            expect_revision_value = 0
          } else if (depth == 1 && (last_significant == "{" || last_significant == ",")) {
            string_role = "key"
          } else {
            string_role = "other"
          }
          continue
        }

        if (character ~ /[[:space:]]/) {
          continue
        }
        if (pending_revision_key) {
          if (depth == 1 && character == ":") {
            pending_revision_key = 0
            expect_revision_value = 1
            last_significant = character
            continue
          }
          invalid = 1
        }
        if (expect_revision_value) {
          invalid = 1
        }
        if (character == "{" || character == "[") {
          depth += 1
        } else if (character == "}" || character == "]") {
          depth -= 1
          if (depth < 0) {
            invalid = 1
          }
        }
        last_significant = character
      }
    }
    END {
      if (invalid || in_string || depth != 0 || revision_count != 1 || length(revision) != 64 || revision ~ /[^0123456789abcdef]/) {
        exit 1
      }
      printf "%s", revision
    }
  ' "$revision_path"
}

read_stack_state() {
  if [ ! -f "$state_env" ]; then
    return 0
  fi

  while IFS= read -r raw_line || [ -n "$raw_line" ]; do
    line=$(trim_value "$raw_line")
    case "$line" in
      ""|\#*) continue ;;
    esac
    case "$line" in
      export[[:space:]]*) line=$(trim_value "${line#export}") ;;
    esac
    case "$line" in
      *=*) ;;
      *) continue ;;
    esac
    key=$(trim_value "${line%%=*}")
    raw_value=$(trim_value "${line#*=}")
    value=$(strip_quotes "$raw_value")
    case "$key" in
      FS_COMPOSE_FILES|FS_COMPOSE_PROFILES|FS_COMPOSE_PROJECT_NAME|FS_DEPLOYMENT_PROFILE|FS_ENV_FILE|FS_STACK_ENVIRONMENT|FS_FILE_STORAGE)
        export "$key=$value"
        ;;
    esac
  done < "$state_env"
}

read_selected_cli_image() {
  configured_app_image=""
  configured_cli_image=""
  configured_compact_image=""
  configured_control_image=""
  configured_frontend_image=""
  configured_mcp_image=""
  configured_worker_image=""
  configured_candidate_state_dir=""
  configured_candidate_prod_env=""
  configured_install_mode=""
  configured_extensions_dir=""
  configured_backups_dir=""
  configured_logs_dir=""
  selected_env_path=$(resolve_repo_path "${FS_ENV_FILE:-.env}")
  if [ ! -f "$selected_env_path" ]; then
    return 0
  fi

  while IFS= read -r raw_line || [ -n "$raw_line" ]; do
    case "$raw_line" in
      *FS_APP_IMAGE*|*FS_CLI_IMAGE*|*FS_COMPACT_IMAGE*|*FS_CONTROL_IMAGE*|*FS_FRONTEND_IMAGE*|*FS_WORKER_IMAGE*|*MCP_IMAGE*|*FS_CANDIDATE_STATE_DIR*|*FS_CANDIDATE_PROD_ENV*|*FS_INSTALL_MODE*|*FS_EXTENSIONS_DIR*|*FS_BACKUPS_DIR*|*FS_LOGS_DIR*) ;;
      *) continue ;;
    esac
    line=$(trim_value "$raw_line")
    case "$line" in
      ""|\#*) continue ;;
      *=*) ;;
      *) continue ;;
    esac
    key=$(trim_value "${line%%=*}")
    raw_value=$(trim_value "${line#*=}")
    case "$key" in
      FS_APP_IMAGE)
        configured_app_image=$(trim_value "$(strip_quotes "$raw_value")")
        ;;
      FS_CLI_IMAGE)
        configured_cli_image=$(trim_value "$(strip_quotes "$raw_value")")
        ;;
      FS_COMPACT_IMAGE)
        configured_compact_image=$(trim_value "$(strip_quotes "$raw_value")")
        ;;
      FS_CONTROL_IMAGE)
        configured_control_image=$(trim_value "$(strip_quotes "$raw_value")")
        ;;
      FS_FRONTEND_IMAGE)
        configured_frontend_image=$(trim_value "$(strip_quotes "$raw_value")")
        ;;
      FS_WORKER_IMAGE)
        configured_worker_image=$(trim_value "$(strip_quotes "$raw_value")")
        ;;
      MCP_IMAGE)
        configured_mcp_image=$(trim_value "$(strip_quotes "$raw_value")")
        ;;
      FS_CANDIDATE_STATE_DIR)
        configured_candidate_state_dir=$(trim_value "$(strip_quotes "$raw_value")")
        ;;
      FS_CANDIDATE_PROD_ENV)
        configured_candidate_prod_env=$(trim_value "$(strip_quotes "$raw_value")")
        ;;
      FS_INSTALL_MODE)
        configured_install_mode=$(trim_value "$(strip_quotes "$raw_value")")
        ;;
      FS_EXTENSIONS_DIR) configured_extensions_dir=$(strip_quotes "$raw_value") ;;
      FS_BACKUPS_DIR) configured_backups_dir=$(strip_quotes "$raw_value") ;;
      FS_LOGS_DIR) configured_logs_dir=$(strip_quotes "$raw_value") ;;
    esac
  done < "$selected_env_path"

  if [ "${FS_DEPLOYMENT_PROFILE:-}" = "compact" ]; then
    compact_mode=1
    if [ -n "$configured_cli_image" ]; then
      FS_CLI_IMAGE=$configured_cli_image
      export FS_CLI_IMAGE
    elif [ -n "$configured_compact_image" ]; then
      # Compatibility for installations created before Compact published the
      # canonical management/CLI role independently.
      FS_CLI_IMAGE=$configured_compact_image
      export FS_CLI_IMAGE
    fi
    return 0
  fi

  if [ -n "$configured_cli_image" ]; then
    FS_CLI_IMAGE=$configured_cli_image
    export FS_CLI_IMAGE
  fi
}

fail() {
  message=$1
  code=${2:-1}
  printf '%s\n' "$message" >&2
  exit "$code"
}

render_doctor_precondition() {
  check_id=$1
  title=$2
  detail=$3
  remediation=$4
  if [ "$doctor_json_requested" -eq 1 ]; then
    printf '%s\n' "{\"schema\":\"flow-steward.cli.v1\",\"ok\":false,\"command\":\"doctor\",\"error\":{\"code\":\"precondition_failed\",\"message\":\"Core readiness is blocked.\",\"field_issues\":[],\"hint\":\"$remediation\",\"retryable\":true,\"details\":{\"status\":\"blocked\",\"findings\":[{\"check_id\":\"$check_id\",\"area\":\"core\",\"title\":\"$title\",\"status\":\"failed\",\"detail\":\"$detail\",\"remediation\":\"$remediation\"}],\"core_passed\":0,\"core_failed\":1,\"optional_warnings\":0,\"recommendations\":0,\"process_exit_code\":4}},\"operation_id\":null,\"log_path\":null}"
  else
    printf '%s\n' "Core readiness"
    printf 'failed: %s — %s\n' "$check_id" "$title"
    printf '  Detail: %s\n' "$detail"
    printf '  Next: %s\n' "$remediation"
  fi
  [ "${FS_DOCTOR_SECTION:-}" = "configuration" ] && exit "$doctor_blocked_code"
  exit 4
}

collect_host_diagnostics_payload() {
  command -v python3 >/dev/null 2>&1 || return 1
  (
    cd "$root_dir"
    python3 -c 'import json, sys; from pathlib import Path; from core.infrastructure.installer.host_diagnostics import collect_host_diagnostics; print(json.dumps(collect_host_diagnostics(Path(sys.argv[1])), separators=(",", ":")))' "$root_dir"
  )
}

check_architecture() {
  arch=$(uname -m 2>/dev/null || printf unknown)
  case "$arch" in
    x86_64|amd64|arm64|aarch64) ;;
    *)
      if [ "$doctor_requested" -eq 1 ]; then
        render_doctor_precondition \
          "host_platform" \
          "Host platform" \
          "Host architecture is unsupported for this first release." \
          "Run Flow Steward on amd64 or arm64 hardware."
      fi
      fail "Unsupported Docker host architecture '$arch' for this first release." 2
      ;;
  esac
}

check_local_docker_context() {
  case "${DOCKER_HOST:-}" in
    ""|unix://*|npipe://*) ;;
    *)
      if [ "$doctor_requested" -eq 1 ]; then
        render_doctor_precondition \
          "docker_context" \
          "Docker context and socket" \
          "The configured Docker endpoint is remote and unsupported." \
          "Select a local Docker context backed by a Unix socket."
      fi
      fail "Remote Docker endpoints are not supported for the first-release CLI launcher. Use a local Docker Unix socket." 2
      ;;
  esac

  if [ "$#" -gt 0 ]; then
    context_host=$1
  else
    context_host=$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || printf '')
  fi
  if [ -n "$context_host" ]; then
    case "$context_host" in
      unix://*|npipe://*) ;;
      *)
        if [ "$doctor_requested" -eq 1 ]; then
          render_doctor_precondition \
            "docker_context" \
            "Docker context and socket" \
            "The active Docker context uses an unsupported remote endpoint." \
            "Select a local Docker context backed by a Unix socket."
        fi
        fail "Remote Docker endpoints are not supported for the first-release CLI launcher. Use a local Docker Unix socket." 2
        ;;
    esac
  fi
}

cleanup_docker_preflight() {
  for preflight_pid in ${docker_preflight_pids:-}; do
    kill "$preflight_pid" 2>/dev/null || true
    wait "$preflight_pid" 2>/dev/null || true
  done
  docker_preflight_pids=""
  if [ -n "${docker_preflight_dir:-}" ]; then
    rm -f \
      "$docker_preflight_dir/context" \
      "$docker_preflight_dir/info" \
      "$docker_preflight_dir/compose"
    rmdir "$docker_preflight_dir" 2>/dev/null || true
    docker_preflight_dir=""
  fi
}

collect_docker_preflight() {
  docker_preflight_dir="$scratch_dir/docker-preflight-$$"
  if ! mkdir "$docker_preflight_dir" 2>/dev/null; then
    fail "Flow Steward CLI could not create its owner-only Docker preflight state." 6
  fi
  chmod 700 "$docker_preflight_dir"

  docker context inspect --format '{{.Endpoints.docker.Host}}' \
    > "$docker_preflight_dir/context" 2>/dev/null &
  context_pid=$!
  docker info --format '{{.SecurityOptions}}' \
    > "$docker_preflight_dir/info" 2>/dev/null &
  info_pid=$!
  docker compose version --short \
    > "$docker_preflight_dir/compose" 2>/dev/null &
  compose_pid=$!
  docker_preflight_pids="$context_pid $info_pid $compose_pid"

  context_status=0
  info_status=0
  compose_status=0
  wait "$context_pid" || context_status=$?
  wait "$info_pid" || info_status=$?
  wait "$compose_pid" || compose_status=$?
  docker_preflight_pids=""

  context_host=""
  docker_security_options=""
  compose_version=""
  IFS= read -r context_host < "$docker_preflight_dir/context" || context_host=""
  IFS= read -r docker_security_options < "$docker_preflight_dir/info" || docker_security_options=""
  IFS= read -r compose_version < "$docker_preflight_dir/compose" || compose_version=""
  cleanup_docker_preflight
}

check_rootless_boundary() {
  security_options=$1
  case "$security_options" in
    *rootless*)
      if [ "$doctor_requested" -eq 1 ]; then
        render_doctor_precondition \
          "docker_rootless" \
          "Docker rootless mode" \
          "Rootless Docker is unsupported because the CLI runtime must join the local Docker socket group." \
          "Use a supported rootful local Docker Engine or Docker Desktop installation."
      fi
      fail "Rootless Docker is not supported for this first release because the CLI container must join the local Docker socket group." 2
      ;;
  esac
}

read_version() {
  if [ ! -f "$root_dir/VERSION" ]; then
    if [ "$doctor_requested" -eq 1 ]; then
      render_doctor_precondition \
        "doctor_runtime" \
        "Doctor runtime" \
        "The VERSION file is missing, so the installed CLI runtime cannot be selected safely." \
        "Restore the release files and rerun Doctor."
    fi
    fail "VERSION file is missing; cannot select the Flow Steward CLI image tag." 2
  fi
  IFS= read -r version < "$root_dir/VERSION"
  version=$(trim_value "$version")
  if [ -z "$version" ]; then
    if [ "$doctor_requested" -eq 1 ]; then
      render_doctor_precondition \
        "doctor_runtime" \
        "Doctor runtime" \
        "The VERSION file is empty, so the installed CLI runtime cannot be selected safely." \
        "Restore the release files and rerun Doctor."
    fi
    fail "VERSION file is empty; cannot select the Flow Steward CLI image tag." 2
  fi
  FS_CLI_IMAGE="flow-steward-cli:$version"
  export FS_CLI_IMAGE
}

git_revision() {
  if revision=$(git -C "$root_dir" rev-parse HEAD 2>/dev/null); then
    printf '%s' "$revision"
    return 0
  fi
  printf '%s' "unknown"
}

print_new_build_output() {
  if [ "${verbose:-0}" != "1" ]; then
    return 0
  fi
  current_lines=$(wc -l < "$bootstrap_log" | tr -d ' ')
  if [ "$current_lines" -gt "${printed_lines:-0}" ]; then
    start_line=$((printed_lines + 1))
    sed -n "${start_line},${current_lines}p" "$bootstrap_log" >&2
    printed_lines=$current_lines
  fi
}

build_cli_image() {
  bootstrap_log="$log_dir/cli-bootstrap-$(date +%Y%m%d%H%M%S)-$$.log"
  : > "$bootstrap_log"
  printed_lines=0
  last_size=0
  last_status=$(date +%s)

  docker build \
    --target cli \
    --build-arg "FS_BUILD_REVISION=$git_revision_value" \
    -t "$FS_CLI_IMAGE" \
    -f "$root_dir/infra/docker/Dockerfile" \
    "$root_dir" > "$bootstrap_log" 2>&1 &
  build_pid=$!

  while kill -0 "$build_pid" 2>/dev/null; do
    sleep 1
    print_new_build_output
    now=$(date +%s)
    size=$(wc -c < "$bootstrap_log" | tr -d ' ')
    if [ "$size" != "$last_size" ]; then
      last_size=$size
      last_status=$now
      continue
    fi
    if [ $((now - last_status)) -ge 5 ]; then
      printf 'Still building Flow Steward CLI image; full output is retained at %s\n' "$bootstrap_log" >&2
      last_status=$now
    fi
  done

  set +e
  wait "$build_pid"
  build_status=$?
  set -e
  print_new_build_output
  if [ "$build_status" -ne 0 ]; then
    printf 'Failed to build Flow Steward CLI image (exit %s). Bootstrap log: %s\n' "$build_status" "$bootstrap_log" >&2
    printf '%s\n' "Last safe build output:" >&2
    tail -n 20 "$bootstrap_log" >&2 || true
    exit "$build_status"
  fi
}

is_exact_image_id() {
  image_ref=$1
  case "$image_ref" in
    sha256:*) image_digest=${image_ref#sha256:} ;;
    *) return 1 ;;
  esac
  [ "${#image_digest}" -eq 64 ] || return 1
  case "$image_digest" in
    *[!0-9a-f]*) return 1 ;;
  esac
}

is_digest_qualified_image_ref() {
  image_ref=$1
  case "$image_ref" in
    *@sha256:*) image_digest=${image_ref##*@sha256:} ;;
    *) return 1 ;;
  esac
  [ "${#image_digest}" -eq 64 ] || return 1
  case "$image_digest" in
    *[!0-9a-f]*) return 1 ;;
  esac
}

is_immutable_image_ref() {
  is_exact_image_id "$1" || is_digest_qualified_image_ref "$1"
}

complete_exact_prebuilt_image_set_configured() {
  for image_ref in \
    "$configured_app_image" \
    "$configured_worker_image" \
    "$configured_control_image" \
    "$configured_mcp_image" \
    "$configured_cli_image"; do
    is_immutable_image_ref "$image_ref" || return 1
  done
  case "${FS_STACK_ENVIRONMENT:-}" in
    development|local_development)
      is_immutable_image_ref "$configured_frontend_image" || return 1
      ;;
    *)
      if [ -n "$configured_frontend_image" ]; then
        is_immutable_image_ref "$configured_frontend_image" || return 1
      fi
      ;;
  esac
}

compact_pinned_runtime_image_resolved() {
  # For Compact the exact image id in the installed environment is the authority.
  # The deployment directory is not necessarily a Git checkout - on a provider it
  # usually is not - so its revision cannot decide whether the image is current.
  [ "$compact_mode" -eq 1 ] || return 1
  is_immutable_image_ref "$configured_compact_image" || return 1
  is_immutable_image_ref "$FS_CLI_IMAGE" || return 1
  [ -n "${expected_image_id:-}" ] || return 1
  is_exact_image_id "$expected_image_id" || return 1
  if is_exact_image_id "$FS_CLI_IMAGE"; then
    [ "$expected_image_id" = "$FS_CLI_IMAGE" ]
  else
    is_digest_qualified_image_ref "$FS_CLI_IMAGE"
  fi
}

ensure_cli_image() {
  image_metadata=$(docker image inspect "$FS_CLI_IMAGE" --format '{{.Id}}|{{ index .Config.Labels "org.opencontainers.image.revision" }}' 2>/dev/null || printf '')
  case "$image_metadata" in
    *'|'*)
      expected_image_id=${image_metadata%%|*}
      image_revision=${image_metadata#*|}
      ;;
    *)
      expected_image_id=""
      image_revision=""
      ;;
  esac
  if compact_pinned_runtime_image_resolved; then
    return 0
  fi
  if [ "$image_revision" = "$git_revision_value" ]; then
    return 0
  fi
  if [ "$compact_mode" -eq 1 ]; then
    fail "The exact installed Compact image is missing or stale. Rerun ./install.sh." 4
  fi
  if complete_exact_prebuilt_image_set_configured; then
    return 0
  fi
  case "${FS_STACK_ENVIRONMENT:-}" in
    development|local_development) ;;
    *)
      fail "The official production CLI image set is incomplete or invalid. Rerun ./install.sh; local image builds are disabled for production." 4
      ;;
  esac
  if source_cli_build_inputs_unchanged "$image_revision" "$git_revision_value"; then
    return 0
  fi
  build_cli_image
}

source_cli_build_inputs_unchanged() {
  installed_revision=$1
  candidate_revision=$2
  case "${FS_STACK_ENVIRONMENT:-}" in
    development|local_development) ;;
    *) return 1 ;;
  esac
  case "$configured_install_mode" in
    source|source_git_tag) ;;
    *) return 1 ;;
  esac
  changed_cli_inputs=$(git -C "$root_dir" diff --name-only \
    "$installed_revision..$candidate_revision" -- \
    infra/docker/Dockerfile requirements-cli.lock 2>/dev/null) || return 1
  [ -z "$changed_cli_inputs" ]
}

docker_socket_gid() {
  socket_path=${DOCKER_SOCKET:-/var/run/docker.sock}
  if [ "$(uname -s 2>/dev/null || printf unknown)" = "Darwin" ]; then
    printf '%s' "0"
    return 0
  fi
  if [ -S "$socket_path" ] || [ -e "$socket_path" ]; then
    if gid=$(stat -L -c %g "$socket_path" 2>/dev/null); then
      printf '%s' "$gid"
      return 0
    fi
    if gid=$(stat -L -f %g "$socket_path" 2>/dev/null); then
      printf '%s' "$gid"
      return 0
    fi
  fi
  printf '%s' "0"
}

compose_file_args() {
  files=${FS_COMPOSE_FILES:-infra/docker/docker-compose.yaml}
  args=""
  for compose_file in $files; do
    resolved=$(resolve_repo_path "$compose_file")
    args="$args -f $resolved"
  done
  printf '%s' "$args"
}

compose_profile_args() {
  profiles="${FS_COMPOSE_PROFILES:-} cli"
  args=""
  seen_cli=0
  for profile in $profiles; do
    [ -z "$profile" ] && continue
    if [ "$profile" = "cli" ]; then
      if [ "$seen_cli" -eq 1 ]; then
        continue
      fi
      seen_cli=1
    fi
    args="$args --profile $profile"
  done
  printf '%s' "$args"
}

compose_env_file() {
  env_file=${FS_ENV_FILE:-.env}
  resolved=$(resolve_repo_path "$env_file")
  if [ -f "$resolved" ]; then
    printf '%s' "$resolved"
  fi
}

select_cli_host_bindings() {
  cli_host_bindings=0
  cli_artifact_binding=0
  [ "$compact_mode" -eq 0 ] || return 0
  selected_files=${FS_COMPOSE_FILES:-infra/docker/docker-compose.yaml}
  case " $selected_files " in
    *"/scalable/data-compact_volume.yaml "*) return 0 ;;
  esac
  cli_host_bindings=1
  case " $selected_files " in
    *"/scalable/cli-host-directories.yaml "*) ;;
    *) selected_files="$selected_files infra/docker/scalable/cli-host-directories.yaml" ;;
  esac
  if [ "${FS_FILE_STORAGE:-bundled}" = "local_fs" ]; then
    cli_artifact_binding=1
    case " $selected_files " in
      *"/scalable/cli-host-directories-artifacts.yaml "*) ;;
      *) selected_files="$selected_files infra/docker/scalable/cli-host-directories-artifacts.yaml" ;;
    esac
  fi
  FS_COMPOSE_FILES=$selected_files
  export FS_COMPOSE_FILES
}

prepare_runtime_context() {
  select_cli_host_bindings
  compose_files_args=$(compose_file_args)
  compose_profiles_args=$(compose_profile_args)
  env_file_path=$(compose_env_file)
  compose_quiet_args="--ansi never --progress quiet"
  FS_COMPOSE_PROJECT_NAME=${FS_COMPOSE_PROJECT_NAME:-flow-steward}
  FS_STACK_ENVIRONMENT=${FS_STACK_ENVIRONMENT:-unconfigured}

  selected_env_file=${FS_ENV_FILE:-.env}
  normalized_env_file=$(normalize_absolute_path "$selected_env_file" "$root_dir") || return 1
  if [ -f "$normalized_env_file" ]; then
    env_file_path=$normalized_env_file
    env_file_identity=$normalized_env_file
  else
    env_file_path=""
    env_file_identity=absent
  fi
  FS_ENV_FILE=$normalized_env_file

  normalized_compose_files_value=$(normalized_compose_paths) || return 1
  normalized_root=$(normalize_absolute_path "$root_dir" "$root_dir") || return 1
  normalized_installer_state=$(normalize_absolute_path "$state_dir" "$root_dir") || return 1
  normalized_runtime_dir=$(normalize_absolute_path "$runtime_dir" "$root_dir") || return 1
  if [ "$cli_host_bindings" -eq 1 ]; then
    FS_EXTENSIONS_DIR=$(normalize_absolute_path "${configured_extensions_dir:-$root_dir/extensions}" "$root_dir/infra/docker") || return 1
    FS_BACKUPS_DIR=$(normalize_absolute_path "${configured_backups_dir:-$root_dir/backups}" "$root_dir/infra/docker") || return 1
    export FS_EXTENSIONS_DIR FS_BACKUPS_DIR
    if [ "$cli_artifact_binding" -eq 1 ]; then
      FS_LOGS_DIR=$(normalize_absolute_path "${configured_logs_dir:-$root_dir/logs}" "$root_dir/infra/docker") || return 1
      export FS_LOGS_DIR
    fi
  fi
  normalized_candidate_state_dir=""
  normalized_candidate_prod_env=""
  if [ "$FS_STACK_ENVIRONMENT" = "production" ]; then
    case "$configured_candidate_state_dir" in
      /*) ;;
      *) return 1 ;;
    esac
    case "$configured_candidate_prod_env" in
      /*) ;;
      *) return 1 ;;
    esac
    normalized_candidate_state_dir=$(
      normalize_absolute_path "$configured_candidate_state_dir" "$root_dir"
    ) || return 1
    normalized_candidate_prod_env=$(
      normalize_absolute_path "$configured_candidate_prod_env" "$root_dir"
    ) || return 1
    [ -d "$normalized_candidate_state_dir" ] || return 1
    [ -f "$normalized_candidate_prod_env" ] || return 1
    [ "$normalized_candidate_prod_env" = "$normalized_env_file" ] || return 1
  fi
  FS_DOCKER_HOST_PROJECT_ROOT=$normalized_root
  FS_INSTALLER_STATE_DIR=$normalized_installer_state

  export FS_COMPOSE_PROJECT_NAME FS_ENV_FILE
  export FS_STACK_ENVIRONMENT FS_DOCKER_HOST_PROJECT_ROOT
  export FS_INSTALLER_STATE_DIR
}

compose_command() {
  if [ -n "$env_file_path" ]; then
    docker compose -p "$FS_COMPOSE_PROJECT_NAME" $compose_files_args $compose_quiet_args \
      --env-file "$env_file_path" $compose_profiles_args "$@"
    return
  fi
  docker compose -p "$FS_COMPOSE_PROJECT_NAME" $compose_files_args $compose_quiet_args \
    $compose_profiles_args "$@"
}

calculate_runtime_identity() {
  if [ ! -x "$runtime_identity_helper" ]; then
    if [ "$doctor_requested" -eq 1 ]; then
      render_doctor_precondition \
        "doctor_runtime" \
        "Doctor runtime" \
        "The CLI runtime identity helper is unavailable." \
        "Restore the installation files and rerun Doctor."
    fi
    fail "Flow Steward CLI runtime identity helper is unavailable. Restore the installation files and rerun ./install.sh." 2
  fi
  if ! active_revision=$(read_active_revision); then
    if [ "$doctor_requested" -eq 1 ]; then
      render_doctor_precondition \
        "doctor_runtime" \
        "Doctor runtime" \
        "The CLI runtime identity state is invalid." \
        "Restore owner-only installation state and rerun Doctor."
    fi
    fail "Flow Steward CLI runtime identity state is invalid. Restore owner-only installation state and rerun ./install.sh." 2
  fi

  if ! FS_CLI_RUNTIME_REVISION=$(
    "$runtime_identity_helper" \
      --compose-project "$FS_COMPOSE_PROJECT_NAME" \
      --stack-environment "$FS_STACK_ENVIRONMENT" \
      --active-revision "$active_revision" \
      --compose-files "$normalized_compose_files_value" \
      --env-file "$env_file_identity" \
      --host-project-root "$normalized_root" \
      --host-uid "$FS_HOST_UID" \
      --host-gid "$FS_HOST_GID" \
      --docker-gid "$FS_DOCKER_GID" \
      2>/dev/null
  ); then
    if [ "$doctor_requested" -eq 1 ]; then
      render_doctor_precondition \
        "doctor_runtime" \
        "Doctor runtime" \
        "The CLI runtime identity could not be calculated." \
        "Install a SHA-256 utility, restore installation state, and rerun Doctor."
    fi
    fail "Flow Steward CLI runtime identity could not be calculated. Install a SHA-256 utility, restore installation state, and rerun ./install.sh." 2
  fi
  if [ "${#FS_CLI_RUNTIME_REVISION}" -ne 64 ]; then
    if [ "$doctor_requested" -eq 1 ]; then
      render_doctor_precondition \
        "doctor_runtime" \
        "Doctor runtime" \
        "The CLI runtime identity is invalid." \
        "Restore installation state and rerun Doctor."
    fi
    fail "Flow Steward CLI runtime identity could not be calculated. Install a SHA-256 utility, restore installation state, and rerun ./install.sh." 2
  fi
  case "$FS_CLI_RUNTIME_REVISION" in
    *[!0123456789abcdef]*)
      if [ "$doctor_requested" -eq 1 ]; then
        render_doctor_precondition \
          "doctor_runtime" \
          "Doctor runtime" \
          "The CLI runtime identity is invalid." \
          "Restore installation state and rerun Doctor."
      fi
      fail "Flow Steward CLI runtime identity could not be calculated. Install a SHA-256 utility, restore installation state, and rerun ./install.sh." 2
      ;;
  esac
  export FS_CLI_RUNTIME_REVISION
}

ensure_runtime_log() {
  if [ -n "$runtime_log" ]; then
    return 0
  fi
  runtime_log="$log_dir/cli-runtime-$(date +%Y%m%d%H%M%S)-$$.log"
  : > "$runtime_log"
  chmod 600 "$runtime_log"
}

redact_runtime_output() {
  sed \
    -e 's/\([Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn]\)[[:space:]]*:[[:space:]]*[Bb][Ee][Aa][Rr][Ee][Rr][[:space:]][^[:space:]]*/\1: Bearer [REDACTED]/g' \
    -e 's/\([A-Za-z0-9_]*[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd][A-Za-z0-9_]*\)[[:space:]]*[:=][[:space:]]*[^[:space:]]*/\1=[REDACTED]/g' \
    -e 's/\([A-Za-z0-9_]*[Tt][Oo][Kk][Ee][Nn][A-Za-z0-9_]*\)[[:space:]]*[:=][[:space:]]*[^[:space:]]*/\1=[REDACTED]/g' \
    -e 's/\([A-Za-z0-9_]*[Ss][Ee][Cc][Rr][Ee][Tt][A-Za-z0-9_]*\)[[:space:]]*[:=][[:space:]]*[^[:space:]]*/\1=[REDACTED]/g' \
    -e 's/\([A-Za-z0-9_]*[Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn][A-Za-z0-9_]*\)[[:space:]]*[:=][[:space:]]*[^[:space:]]*/\1=[REDACTED]/g'
}

append_runtime_capture() {
  phase=$1
  ensure_runtime_log
  printf '[%s]\n' "$phase" >> "$runtime_log"
  if [ -s "$runtime_capture" ]; then
    redact_runtime_output < "$runtime_capture" >> "$runtime_log"
  else
    printf '%s\n' "(no technical output)" >> "$runtime_log"
  fi
}

cleanup_runtime_capture() {
  if [ -f "$runtime_capture" ]; then
    : > "$runtime_capture"
    rm -f "$runtime_capture"
  fi
}

resolve_expected_image_id() {
  if [ -n "${expected_image_id:-}" ]; then
    return 0
  fi
  : > "$runtime_capture"
  chmod 600 "$runtime_capture"
  if ! expected_image_id=$(docker image inspect "$FS_CLI_IMAGE" --format '{{.Id}}' 2>"$runtime_capture"); then
    append_runtime_capture "image-inspect"
    bootstrap_failure "Docker could not resolve the exact CLI image ID."
  fi
  if [ -z "$expected_image_id" ]; then
    printf '%s\n' "Docker returned an empty exact image ID." > "$runtime_capture"
    append_runtime_capture "image-inspect"
    bootstrap_failure "Docker could not resolve the exact CLI image ID."
  fi
}

release_repair_lock() {
  if [ "$repair_lock_owned" -ne 1 ]; then
    return 0
  fi
  lock_owner=""
  if [ -f "$repair_lock_dir/pid" ]; then
    IFS= read -r lock_owner < "$repair_lock_dir/pid" || lock_owner=""
  fi
  if [ "$lock_owner" = "$$" ]; then
    rm -f "$repair_lock_dir/pid"
    rmdir "$repair_lock_dir" 2>/dev/null || true
  fi
  repair_lock_owned=0
}

acquire_repair_lock() {
  lock_started=$(date +%s)
  lock_heartbeat=0
  while :; do
    if mkdir "$repair_lock_dir" 2>/dev/null; then
      chmod 700 "$repair_lock_dir"
      if ! printf '%s\n' "$$" > "$repair_lock_dir/pid"; then
        rmdir "$repair_lock_dir" 2>/dev/null || true
        fail "Flow Steward CLI could not record repair-lock ownership." 6
      fi
      chmod 600 "$repair_lock_dir/pid"
      repair_lock_owned=1
      return 0
    fi

    lock_owner=""
    if [ -d "$repair_lock_dir" ] && [ ! -L "$repair_lock_dir" ] && [ -f "$repair_lock_dir/pid" ]; then
      IFS= read -r lock_owner < "$repair_lock_dir/pid" || lock_owner=""
    fi
    if [ -z "$lock_owner" ] && [ -d "$repair_lock_dir" ] && [ ! -L "$repair_lock_dir" ]; then
      sleep 1
      if [ -f "$repair_lock_dir/pid" ]; then
        IFS= read -r lock_owner < "$repair_lock_dir/pid" || lock_owner=""
      fi
    fi
    case "$lock_owner" in
      ""|*[!0123456789]*)
        stale_lock=1
        ;;
      *)
        if kill -0 "$lock_owner" 2>/dev/null; then
          stale_lock=0
        else
          stale_lock=1
        fi
        ;;
    esac
    if [ "$stale_lock" -eq 1 ] && [ -d "$repair_lock_dir" ] && [ ! -L "$repair_lock_dir" ]; then
      rm -f "$repair_lock_dir/pid"
      rmdir "$repair_lock_dir" 2>/dev/null || true
      continue
    fi

    lock_now=$(date +%s)
    lock_elapsed=$((lock_now - lock_started))
    if [ "$lock_elapsed" -ge 10 ]; then
      fail "Flow Steward CLI runtime repair lock timed out after 10 seconds. Retry the command after the other launcher finishes." 6
    fi
    if [ "$lock_heartbeat" -eq 0 ]; then
      printf '%s\n' "Waiting for another Flow Steward CLI launcher to repair the runtime..." >&2
      lock_heartbeat=1
    fi
    sleep 1
  done
}

inspect_runtime_container_record() {
  inspected_container=$1
  inspect_format='{{.State.Running}}|{{.Image}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.flow-steward.cli-runtime"}}|{{index .Config.Labels "com.flow-steward.cli-runtime-contract"}}|{{index .Config.Labels "com.flow-steward.cli-runtime-revision"}}|{{.Config.User}}|{{.HostConfig.Privileged}}|{{range .HostConfig.GroupAdd}}{{printf "%s," .}}{{end}}{{printf "\n"}}{{range .Mounts}}{{printf "%s\t%s\t%s\t%t\n" .Type .Source .Destination .RW}}{{end}}'
  container_inspect_status=0
  if [ "$doctor_requested" -eq 1 ]; then
    inspected_runtime=$(docker container inspect "$inspected_container" --format "$inspect_format" 2>/dev/null) || container_inspect_status=$?
  else
    : > "$runtime_capture"
    chmod 600 "$runtime_capture"
    inspected_runtime=$(docker container inspect "$inspected_container" --format "$inspect_format" 2>"$runtime_capture") || container_inspect_status=$?
  fi
}

inspect_cli_runtime() {
  inspection_cause="The fs-cli container is missing."
  container_ids="$FS_COMPOSE_PROJECT_NAME-fs-cli-1"
  inspect_runtime_container_record "$container_ids"
  if [ "${container_inspect_status:-0}" -ne 0 ]; then
    container_lookup_status=0
    if [ "$doctor_requested" -eq 1 ]; then
      container_ids=$(compose_command ps -q --all fs-cli 2>/dev/null) || container_lookup_status=$?
    else
      : > "$runtime_capture"
      chmod 600 "$runtime_capture"
      container_ids=$(compose_command ps -q --all fs-cli 2>"$runtime_capture") || container_lookup_status=$?
    fi
    if [ "${container_lookup_status:-0}" -ne 0 ]; then
      inspection_cause="Docker Compose could not resolve the fs-cli container."
      return 1
    fi
    case "$container_ids" in
      ""|*'
'*|*[!A-Za-z0-9]*)
        inspection_cause="Docker Compose did not resolve exactly one fs-cli container."
        return 1
        ;;
    esac
    inspect_runtime_container_record "$container_ids"
    if [ "${container_inspect_status:-0}" -ne 0 ]; then
      inspection_cause="Docker could not inspect the fs-cli container."
      return 1
    fi
  fi
  inspected_header=""
  while IFS= read -r inspected_line; do
    inspected_header=$inspected_line
    break
  done <<EOF
$inspected_runtime
EOF
  inspected_extra=""
  previous_ifs=$IFS
  IFS='|' read -r \
    inspected_running inspected_image_id inspected_project inspected_service \
    inspected_runtime_label inspected_contract_label inspected_revision_label \
    inspected_user inspected_privileged inspected_group_add inspected_extra <<EOF
$inspected_header
EOF
  IFS=$previous_ifs
  if [ -n "$inspected_extra" ]; then
    inspection_cause="Docker returned an invalid fs-cli inspection record."
    return 1
  fi

  if \
    [ "$inspected_image_id" != "$expected_image_id" ] ||
    [ "$inspected_project" != "$FS_COMPOSE_PROJECT_NAME" ] ||
    [ "$inspected_service" != "fs-cli" ] ||
    [ "$inspected_runtime_label" != "true" ] ||
    [ "$inspected_contract_label" != "1" ] ||
    [ "$inspected_revision_label" != "$FS_CLI_RUNTIME_REVISION" ] ||
    [ "$inspected_user" != "$FS_HOST_UID:$FS_HOST_GID" ] ||
    [ "$inspected_privileged" != "false" ] ||
    [ "$inspected_group_add" != "$FS_DOCKER_GID," ]; then
    inspection_cause="The fs-cli runtime identity does not match the installed stack."
    return 1
  fi

  inspected_mounts=$(
    printf '%s\n' "$inspected_runtime" |
      sed -n '2,$p' |
      normalize_inspected_mounts |
      LC_ALL=C sort
  )
  app_mount_source=$normalized_root
  runtime_mount_source=$normalized_runtime_dir
  expected_mounts=$(
    printf 'bind\t%s\t%s\ttrue\n' "$app_mount_source" "$normalized_root"
    printf 'bind\t%s\t/var/run/docker.sock\ttrue\n' "/var/run/docker.sock"
    printf 'bind\t%s\t/run/flow-steward-runtime\ttrue\n' "$runtime_mount_source"
    if [ "$cli_host_bindings" -eq 1 ]; then
      printf 'bind\t%s\t/app/extensions\ttrue\n' "$FS_EXTENSIONS_DIR"
      printf 'bind\t%s\t/app/backups\ttrue\n' "$FS_BACKUPS_DIR"
      if [ "$cli_artifact_binding" -eq 1 ]; then
        printf 'bind\t%s\t/app/logs/artifacts\ttrue\n' "$FS_LOGS_DIR/artifacts"
      fi
    fi
    if [ "$FS_STACK_ENVIRONMENT" = "production" ]; then
      printf 'bind\t%s\t/run/flow-steward-candidate-state\ttrue\n' \
        "$normalized_candidate_state_dir"
      printf 'bind\t%s\t/app/.env.prod\ttrue\n' "$normalized_candidate_prod_env"
    fi
  )
  expected_mounts=$(printf '%s\n' "$expected_mounts" | LC_ALL=C sort)
  if [ "$inspected_mounts" != "$expected_mounts" ]; then
    if [ "$doctor_requested" -ne 1 ]; then
      {
        printf '%s\n' "[inspected-mounts]"
        printf '%s\n' "$inspected_mounts"
        printf '%s\n' "[expected-mounts]"
        printf '%s\n' "$expected_mounts"
      } > "$runtime_capture"
      append_runtime_capture "mount-contract-comparison"
    fi
    inspection_cause="The fs-cli runtime mounts do not match the installed stack."
    return 1
  fi
  if [ "$inspected_running" = "false" ]; then
    inspection_cause="The exact fs-cli container is stopped."
    return 2
  fi
  if [ "$inspected_running" != "true" ]; then
    inspection_cause="The fs-cli container running state is invalid."
    return 1
  fi
  inspection_cause=""
  return 0
}

run_logged_compose() {
  phase=$1
  shift
  : > "$runtime_capture"
  chmod 600 "$runtime_capture"
  set +e
  compose_command "$@" > "$runtime_capture" 2>&1
  compose_status=$?
  set -e
  append_runtime_capture "$phase"
  return "$compose_status"
}

bootstrap_failure() {
  bootstrap_cause=$1
  release_repair_lock
  cleanup_runtime_capture
  printf '%s\n' "Flow Steward CLI runtime bootstrap failed." >&2
  printf 'Cause: %s\n' "$bootstrap_cause" >&2
  printf '%s\n' "Remediation: rerun ./install.sh, then retry ./flow-steward." >&2
  if [ -n "$runtime_log" ]; then
    printf 'Technical log: %s\n' "$runtime_log" >&2
  fi
  exit 8
}

process_command_line() {
  # /proc where there is one (Linux, including hosts without procps), ps elsewhere.
  if [ -r "/proc/$1/cmdline" ]; then
    tr '\000' ' ' < "/proc/$1/cmdline"
  else
    ps -p "$1" -o command= 2>/dev/null
  fi
}

install_sh_running() {
  # AC17-R162 (L1): the whole of an ./install.sh run, not only its container. Before the
  # container exists install.sh builds or checks the management runtime, which on an
  # update of the sources can take longer than the auditor's 25 s: nothing held the
  # lock yet, and `stack stop` stopped the stack under the update. install.sh records
  # its host process in this file from its start (installer-host.pid).
  install_sh_host_running && return 0
  if installer_runtime_running; then
    installer_running_as="installer container $(printf '%.12s' "$installer_cid")"
    return 0
  fi
  return 1
}

install_sh_host_running() {
  # The host half of install_sh_running: a live ./install.sh process. doctor --fix asks
  # only this -- an installer container nobody runs any more is what it repairs.
  installer_host_record="$state_dir/installer-host.pid"
  if [ -s "$installer_host_record" ]; then
    installer_host_pid=""
    IFS= read -r installer_host_pid < "$installer_host_record" || true
    case "$installer_host_pid" in
      '' | *[!0-9]*) ;;
      *)
        if kill -0 "$installer_host_pid" 2>/dev/null &&
          process_command_line "$installer_host_pid" | grep -q 'install\.sh'; then
          installer_running_as="process $installer_host_pid"
          return 0
        fi
        ;;
    esac
  fi
  return 1
}

installer_runtime_running() {
  # AC17-R162 (L1): ./install.sh recreates fs-cli itself. A command that repaired fs-cli
  # at the same moment made the installer fail on "removal of container", which
  # released its lock, and the command -- `stack stop` -- then ran unopposed. The
  # installer records its container here while it runs (install.sh --cidfile).
  installer_cidfile="$state_dir/installer-runtime.cid"
  [ -s "$installer_cidfile" ] || return 1
  IFS= read -r installer_cid < "$installer_cidfile" || [ -n "${installer_cid:-}" ] || return 1
  [ -n "$installer_cid" ] || return 1
  [ "$(docker container inspect "$installer_cid" --format '{{.State.Running}}' 2>/dev/null)" = "true" ]
}

lifecycle_operation_active_in_runtime() {
  # AC17-R156: the lifecycle lock is a flock held inside the running fs-cli, so only a
  # process there can see it. Any failure to ask means "not known to be running".
  active_lifecycle_operation=$(
    compose_command exec -T \
      -e "FS_DOCKER_HOST_PROJECT_ROOT=$FS_DOCKER_HOST_PROJECT_ROOT" \
      fs-cli python -m core.entrypoints.cli.lifecycle_lock_probe 2>/dev/null
  ) || return 1
  [ -n "$active_lifecycle_operation" ]
}

prepare_cli_transfer_root() {
  [ "$cli_host_bindings" -eq 1 ] || return 0
  selected_transfer_root=${configured_backups_dir:-$root_dir/backups}
  case "$selected_transfer_root" in
    /*) ;;
    *) selected_transfer_root="$root_dir/infra/docker/$selected_transfer_root" ;;
  esac
  # Normalization resolves directory symlinks; reject the configured leaf before
  # checking its normalized Docker source so a symlink cannot bypass this guard.
  if [ -L "$selected_transfer_root" ]; then
    bootstrap_failure "FS_BACKUPS_DIR must not be a symlink: $selected_transfer_root"
  fi
  if [ "$FS_BACKUPS_DIR" = "$root_dir/backups" ] &&
    [ ! -e "$FS_BACKUPS_DIR" ] && [ ! -L "$FS_BACKUPS_DIR" ]; then
    (umask 077; mkdir "$FS_BACKUPS_DIR") ||
      bootstrap_failure "FS_BACKUPS_DIR could not be created: $FS_BACKUPS_DIR"
  fi
  if [ -L "$FS_BACKUPS_DIR" ] || [ ! -d "$FS_BACKUPS_DIR" ] ||
    [ ! -r "$FS_BACKUPS_DIR" ] || [ ! -w "$FS_BACKUPS_DIR" ] ||
    [ ! -x "$FS_BACKUPS_DIR" ]; then
    bootstrap_failure "FS_BACKUPS_DIR must be an existing readable, writable, traversable directory, not a symlink: $FS_BACKUPS_DIR"
  fi
}

ensure_cli_runtime() {
  inspection_status=0
  if inspect_cli_runtime; then
    return 0
  else
    inspection_status=$?
  fi

  acquire_repair_lock
  if inspect_cli_runtime; then
    release_repair_lock
    return 0
  else
    inspection_status=$?
  fi

  ensure_runtime_log
  printf '[initial-inspection]\n%s\n' "$inspection_cause" >> "$runtime_log"
  append_runtime_capture "initial-inspection-technical"
  if install_sh_running; then
    printf '[installer-running]\n%s\n' "$installer_running_as" >> "$runtime_log"
    release_repair_lock
    fail "./install.sh is running ($installer_running_as) and replaces this CLI runtime itself; rerun this once it has finished. Nothing was changed." 6
  fi
  # A running fs-cli whose identity went stale may be the one running `config apply`
  # (which is what makes it stale); replacing it would kill that operation.
  if [ "$inspection_status" -eq 1 ] && [ "${inspected_running:-}" = "true" ] &&
    lifecycle_operation_active_in_runtime; then
    printf '[lifecycle-operation-active]\n%s\n' "$active_lifecycle_operation" >> "$runtime_log"
    release_repair_lock
    printf 'Lifecycle operation %s is running in the current CLI runtime; this command runs there, and the runtime is refreshed by a later command.\n' \
      "$active_lifecycle_operation" >&2
    return 0
  fi
  prepare_cli_transfer_root
  if [ "$inspection_status" -eq 2 ]; then
    if run_logged_compose "bootstrap-attempt-1-start" start fs-cli; then
      if inspect_cli_runtime; then
        release_repair_lock
        return 0
      fi
      first_bootstrap_cause=$inspection_cause
      append_runtime_capture "verification-attempt-1-start-technical"
    else
      first_bootstrap_cause="The stopped fs-cli container could not be started."
    fi
  else
    if run_logged_compose "bootstrap-attempt-1" up -d --no-deps --no-build --force-recreate fs-cli; then
      if inspect_cli_runtime; then
        release_repair_lock
        return 0
      fi
      first_bootstrap_cause=$inspection_cause
      append_runtime_capture "verification-attempt-1-technical"
    else
      first_bootstrap_cause="The first fs-cli start attempt failed."
    fi
  fi
  printf '[verification-attempt-1]\n%s\n' "$first_bootstrap_cause" >> "$runtime_log"

  run_logged_compose "invalid-container-removal" rm -sf fs-cli || true

  if run_logged_compose "bootstrap-attempt-2" up -d --no-deps --no-build --force-recreate fs-cli; then
    if inspect_cli_runtime; then
      release_repair_lock
      return 0
    fi
    final_bootstrap_cause=$inspection_cause
    append_runtime_capture "verification-attempt-2-technical"
  else
    final_bootstrap_cause="The final fs-cli start attempt failed."
  fi
  printf '[verification-attempt-2]\n%s\n' "$final_bootstrap_cause" >> "$runtime_log"
  bootstrap_failure "$final_bootstrap_cause"
}

exec_compose_command() {
  release_repair_lock
  cleanup_runtime_capture
  trap - EXIT HUP INT TERM
  if [ -n "$env_file_path" ]; then
    exec docker compose -p "$FS_COMPOSE_PROJECT_NAME" $compose_files_args $compose_quiet_args \
      --env-file "$env_file_path" $compose_profiles_args "$@"
  fi
  exec docker compose -p "$FS_COMPOSE_PROJECT_NAME" $compose_files_args $compose_quiet_args \
    $compose_profiles_args "$@"
}

run_cli_command() {

  if [ "$doctor_requested" -eq 1 ]; then
    if [ -n "${NO_COLOR:-}" ]; then
      printf '%s' "$host_diagnostics_payload" | exec_compose_command exec -T \
        -e "NO_COLOR=$NO_COLOR" \
        -e "TERM=${TERM:-}" \
        -e "COLORTERM=${COLORTERM:-}" \
        -e "FS_DOCKER_HOST_PROJECT_ROOT=$FS_DOCKER_HOST_PROJECT_ROOT" \
        -e "FS_CLI_HOST_NO_COLOR=$NO_COLOR" \
        fs-cli python -m core.entrypoints.cli.lifecycle_click_app "$@"
      return
    fi
    printf '%s' "$host_diagnostics_payload" | exec_compose_command exec -T \
      -e "TERM=${TERM:-}" \
      -e "COLORTERM=${COLORTERM:-}" \
      -e "FS_DOCKER_HOST_PROJECT_ROOT=$FS_DOCKER_HOST_PROJECT_ROOT" \
      -e "FS_CLI_HOST_NO_COLOR=" \
      fs-cli python -m core.entrypoints.cli.lifecycle_click_app "$@"
    return
  fi

  if [ -t 0 ] && [ -t 1 ]; then
    no_tty_flag=""
  else
    no_tty_flag="-T"
  fi

  if [ -n "$no_tty_flag" ]; then
    if [ -n "${NO_COLOR:-}" ]; then
      exec_compose_command exec "$no_tty_flag" \
        -e "NO_COLOR=$NO_COLOR" \
        -e "TERM=${TERM:-}" \
        -e "COLORTERM=${COLORTERM:-}" \
        -e "FS_DOCKER_HOST_PROJECT_ROOT=$FS_DOCKER_HOST_PROJECT_ROOT" \
        -e "FS_CLI_HOST_NO_COLOR=$NO_COLOR" \
        fs-cli python -m core.entrypoints.cli.lifecycle_click_app "$@"
    fi
    exec_compose_command exec "$no_tty_flag" \
      -e "TERM=${TERM:-}" \
      -e "COLORTERM=${COLORTERM:-}" \
      -e "FS_DOCKER_HOST_PROJECT_ROOT=$FS_DOCKER_HOST_PROJECT_ROOT" \
      -e "FS_CLI_HOST_NO_COLOR=" \
      fs-cli python -m core.entrypoints.cli.lifecycle_click_app "$@"
  fi
  if [ -n "${NO_COLOR:-}" ]; then
    exec_compose_command exec \
      -e "NO_COLOR=$NO_COLOR" \
      -e "TERM=${TERM:-}" \
      -e "COLORTERM=${COLORTERM:-}" \
      -e "FS_DOCKER_HOST_PROJECT_ROOT=$FS_DOCKER_HOST_PROJECT_ROOT" \
      -e "FS_CLI_HOST_NO_COLOR=$NO_COLOR" \
      fs-cli python -m core.entrypoints.cli.lifecycle_click_app "$@"
  fi
  exec_compose_command exec \
    -e "TERM=${TERM:-}" \
    -e "COLORTERM=${COLORTERM:-}" \
    -e "FS_DOCKER_HOST_PROJECT_ROOT=$FS_DOCKER_HOST_PROJECT_ROOT" \
    -e "FS_CLI_HOST_NO_COLOR=" \
    fs-cli python -m core.entrypoints.cli.lifecycle_click_app "$@"
}

run_isolated_cli_command() {
  isolated_no_tty_flag=""
  isolated_buildx_dir=/tmp/flow-steward-buildx # pragma: allowlist secret - metadata path
  if ! [ -t 0 ] || ! [ -t 1 ]; then
    isolated_no_tty_flag="-T"
  fi
  set -- --entrypoint python \
    -e "TERM=${TERM:-}" \
    -e "COLORTERM=${COLORTERM:-}" \
    -e "FS_DOCKER_HOST_PROJECT_ROOT=$FS_DOCKER_HOST_PROJECT_ROOT" \
    -e "FS_CLI_HOST_NO_COLOR=${NO_COLOR:-}" \
    -e "BUILDX_CONFIG=$isolated_buildx_dir" \
    fs-cli -m core.entrypoints.cli.lifecycle_click_app "$@"
  if [ -n "${DOCKER_CONFIG:-}" ]; then
    isolated_auth_dir=$(normalize_absolute_path "$DOCKER_CONFIG" "$(pwd -P)")
    isolated_auth_file="$isolated_auth_dir/config.json"
    if [ ! -f "$isolated_auth_file" ] || [ ! -r "$isolated_auth_file" ]; then
      printf '%s\n' "DOCKER_CONFIG must contain a readable config.json for release upgrade." >&2
      exit 2
    fi
    case "$isolated_auth_dir" in
      *:*)
        printf '%s\n' "DOCKER_CONFIG cannot contain a colon in a bind-mount path." >&2
        exit 2
        ;;
    esac
    # Preserve the host path for nested Docker clients, but never let them write
    # registry credentials or inherit a host-only Docker/Buildx context.
    set -- --volume "$isolated_auth_file:$isolated_auth_file:ro" \
      -e "DOCKER_CONFIG=$isolated_auth_dir" -e "DOCKER_CONTEXT=default" "$@"
  fi
  exec_compose_command run --rm --no-deps $isolated_no_tty_flag "$@"
}

run_compact_platform_command() {
  compact_word_count=0
  compact_word_1=""
  compact_word_2=""
  compact_word_3=""
  compact_json=0
  compact_plain=0
  compact_tui=0
  compact_no_color=0
  compact_non_interactive=0
  compact_verbose=0
  compact_confirm=0
  compact_confirmation=""
  compact_operation_id=""
  compact_passphrase_stdin=0
  compact_maintenance_confirmed=0
  compact_dry_run=0
  compact_author=""
  compact_target_version=""
  compact_timeout_seconds=""
  compact_baseline=""
  compact_output=""
  compact_label=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --json) compact_json=1 ;;
      --plain) compact_plain=1 ;;
      --tui) compact_tui=1 ;;
      --no-color) compact_no_color=1 ;;
      --non-interactive) compact_non_interactive=1 ;;
      --verbose) compact_verbose=1 ;;
      --confirm)
        if [ "$compact_word_1" = "clone" ] && [ "$compact_word_2" = "restore" ]; then
          [ "$#" -ge 2 ] || fail "clone restore --confirm requires a value." 2
          compact_confirmation=$2
          shift
        else
          compact_confirm=1
        fi
        ;;
      --confirm=*) compact_confirmation=${1#--confirm=} ;;
      --passphrase-stdin) compact_passphrase_stdin=1 ;;
      --dry-run) compact_dry_run=1 ;;
      --maintenance-confirmed) compact_maintenance_confirmed=1 ;;
      --author|--target-version|--timeout-seconds|--operation-id)
        [ "$#" -ge 2 ] || fail "$1 requires a value." 2
        case "$1" in
          --author) compact_author=$2 ;;
          --target-version) compact_target_version=$2 ;;
          --timeout-seconds) compact_timeout_seconds=$2 ;;
          --operation-id) compact_operation_id=$2 ;;
        esac
        shift
        ;;
      --author=*) compact_author=${1#--author=} ;;
      --target-version=*) compact_target_version=${1#--target-version=} ;;
      --timeout-seconds=*) compact_timeout_seconds=${1#--timeout-seconds=} ;;
      --operation-id=*) compact_operation_id=${1#--operation-id=} ;;
      --baseline|--output|--output-directory|--label)
        [ "$#" -ge 2 ] || fail "$1 requires a value." 2
        case "$1" in
          --baseline) compact_baseline=$2 ;;
          --output|--output-directory) compact_output=$2 ;;
          --label) compact_label=$2 ;;
        esac
        shift
        ;;
      --baseline=*) compact_baseline=${1#--baseline=} ;;
      --output=*) compact_output=${1#--output=} ;;
      --output-directory=*) compact_output=${1#--output-directory=} ;;
      --label=*) compact_label=${1#--label=} ;;
      --*) fail "Unsupported Compact platform option: $1" 2 ;;
      *)
        compact_word_count=$((compact_word_count + 1))
        case "$compact_word_count" in
          1) compact_word_1=$1 ;;
          2) compact_word_2=$1 ;;
          3) compact_word_3=$1 ;;
          *) fail "Unsupported Compact platform command." 2 ;;
        esac
        ;;
    esac
    shift
  done

  compact_operation=""
  compact_backup=""
  case "$compact_word_count:$compact_word_1:$compact_word_2" in
    1:status:) compact_operation=status ;;
    1:doctor:) compact_operation=doctor ;;
    # AC17-R113: the launcher keeps its own allowlist, checked before the image is
    # asked anything, so a command absent here is refused whatever the Python
    # registry says. That is the same shape as the AC17-R50 note in gateway.py.
    1:proxy-fragment:) compact_operation=proxy-fragment ;;
    2:maintenance:status) compact_operation=maintenance-status ;;
    2:maintenance:enable) compact_operation=maintenance-enable ;;
    2:maintenance:disable) compact_operation=maintenance-disable ;;
    2:release:info) compact_operation=release-info ;;
    2:release:check) compact_operation=release-check ;;
    2:release:upgrade) compact_operation=release-upgrade ;;
    2:database:status) compact_operation=database-status ;;
    2:database:upgrade) compact_operation=database-upgrade ;;
    2:database:restore-parity-capture)
      compact_operation=database-restore-parity-capture
      [ -n "$compact_output" ] || fail "database restore-parity-capture requires --output." 2
      [ "$compact_confirm" -eq 1 ] || fail "database restore-parity-capture requires --confirm." 2
      ;;
    2:database:restore-parity-verify)
      compact_operation=database-restore-parity-verify
      [ -n "$compact_baseline" ] || fail "database restore-parity-verify requires --baseline." 2
      [ "$compact_confirm" -eq 1 ] || fail "database restore-parity-verify requires --confirm." 2
      ;;
    2:backup:create) compact_operation=backup-create ;;
    2:backup:list) compact_operation=backup-list ;;
    2:clone:create) compact_operation=clone-create ;;
    3:clone:restore)
      compact_operation=clone-restore
      compact_backup=$compact_word_3
      if [ "$compact_non_interactive" -eq 1 ]; then
        [ -n "$compact_confirmation" ] || fail "non-interactive clone restore requires --confirm TARGET." 2
      fi
      ;;
    3:backup:verify)
      compact_operation=backup-verify
      compact_backup=$compact_word_3
      ;;
    *) fail "Unsupported Compact platform command." 2 ;;
  esac
  if [ -n "$compact_operation_id" ] && [ "$compact_operation" != "clone-restore" ]; then
    fail "--operation-id is supported only for clone restore." 2
  fi

  if [ "$compact_operation" = "database-upgrade" ]; then
    [ "$compact_maintenance_confirmed" -eq 1 ] && [ "$compact_confirm" -eq 1 ] || \
      fail "database-upgrade requires --maintenance-confirmed and --confirm" 2
    if [ -n "$compact_target_version$compact_timeout_seconds$compact_baseline$compact_output$compact_label$compact_confirmation" ] || \
      [ "$compact_passphrase_stdin" -eq 1 ] || [ "$compact_dry_run" -eq 1 ]; then
      fail "Unsupported Compact database upgrade option." 2
    fi
    set -- database upgrade
  else
    set -- "$compact_operation"
  fi
  if [ -n "$compact_backup" ]; then
    if [ "$compact_operation" = "clone-restore" ]; then
      set -- "$@" --archive "$compact_backup"
    else
      set -- "$@" --backup "$compact_backup"
    fi
  fi
  if [ -n "$compact_baseline" ]; then
    set -- "$@" --baseline "$compact_baseline"
  fi
  if [ -n "$compact_output" ]; then
    set -- "$@" --output "$compact_output"
  fi
  if [ -n "$compact_label" ]; then
    set -- "$@" --label "$compact_label"
  fi
  if [ "$compact_maintenance_confirmed" -eq 1 ]; then
    set -- "$@" --maintenance-confirmed
  fi
  if [ -n "$compact_author" ]; then
    set -- "$@" --author "$compact_author"
  fi
  if [ -n "$compact_target_version" ]; then
    set -- "$@" --target-version "$compact_target_version"
  fi
  if [ -n "$compact_timeout_seconds" ]; then
    set -- "$@" --timeout-seconds "$compact_timeout_seconds"
  fi
  if [ "$compact_dry_run" -eq 1 ]; then
    set -- "$@" --dry-run
  fi
  if [ "$compact_confirm" -eq 1 ]; then
    set -- "$@" --confirm
  fi
  if [ -n "$compact_confirmation" ]; then
    set -- "$@" --confirmation "$compact_confirmation"
  fi
  if [ -n "$compact_operation_id" ]; then
    set -- "$@" --operation-id "$compact_operation_id"
  fi
  if [ "$compact_passphrase_stdin" -eq 1 ]; then
    set -- "$@" --passphrase-stdin
  fi
  if [ "$compact_json" -eq 1 ]; then
    set -- "$@" --json
  fi
  if [ "$compact_plain" -eq 1 ]; then
    set -- "$@" --plain
  fi
  if [ "$compact_tui" -eq 1 ]; then
    set -- "$@" --tui
  fi
  if [ "$compact_no_color" -eq 1 ]; then
    set -- "$@" --no-color
  fi
  if [ "$compact_non_interactive" -eq 1 ]; then
    set -- "$@" --non-interactive
  fi
  if [ "$compact_verbose" -eq 1 ]; then
    set -- "$@" --verbose
  fi
  if [ -t 0 ] && [ -t 1 ]; then
    compact_no_tty_flag=""
  else
    compact_no_tty_flag="-T"
  fi
  # Database upgrades need the release's complete product dependency graph while
  # writers are stopped. The native adapter restores Compact's persisted runtime
  # secrets before invoking the same canonical database service and seed operation.
  if [ "$compact_operation" = "database-upgrade" ]; then
    if [ -n "${NO_COLOR:-}" ]; then
      exec_compose_command run --rm --no-deps $compact_no_tty_flag \
        -e "NO_COLOR=$NO_COLOR" \
        -e "TERM=${TERM:-}" \
        -e "COLORTERM=${COLORTERM:-}" \
        -e "FS_CLI_OPERATION_LOG_ROOT=/app/logs/cli" \
        -e "FS_CLI_HOST_NO_COLOR=$NO_COLOR" \
        --entrypoint python \
        compact-runtime -m core.entrypoints.cli.compact_container_cli "$@"
    fi
    exec_compose_command run --rm --no-deps $compact_no_tty_flag \
      -e "TERM=${TERM:-}" \
      -e "COLORTERM=${COLORTERM:-}" \
      -e "FS_CLI_OPERATION_LOG_ROOT=/app/logs/cli" \
      -e "FS_CLI_HOST_NO_COLOR=" \
      --entrypoint python \
      compact-runtime -m core.entrypoints.cli.compact_container_cli "$@"
  fi
  # Compact's host-side maintenance client owns host mutation/installation
  # guards, then delegates only the marker operation to the native operator.
  # Direct runtime exec here would hide unfinished host lifecycle journals.
  if [ "$compact_operation" = "doctor" ] && [ "${FS_DOCTOR_SECTION:-}" = "configuration" ]; then
    exec_compose_command run --rm --no-deps $compact_no_tty_flag \
      -e FS_DOCTOR_SECTION=configuration fs-cli "$@"
  fi
  exec_compose_command run --rm --no-deps $compact_no_tty_flag fs-cli "$@"
}

render_doctor_help() {
  cat <<'EOF'
Usage: flow-steward doctor [OPTIONS] [ids ...]

Check the installed configuration and installation without changing them.
Use --fix to repair the selected check ids, or all repairable checks if omitted.

Options:
  --fix              Repair installation findings instead of only diagnosing.
  --confirm          Confirm repairs without an interactive prompt.
  --json             Emit the diagnosis or repair result as JSON.
  --plain            Accept the global plain-output option.
  --tui              Accept the global display option; Doctor uses plain output.
  --no-color         Accept the global no-color option.
  --verbose          Accept the global verbose option.
  --non-interactive  Accept the global non-interactive option.
  -h, --help         Show this message without diagnostics or installer actions.
EOF
}

render_compact_help() {
  cat <<'EOF'
Usage: flow-steward [OPTIONS] COMMAND [ARGS]...

Manage an installed Flow Steward Compact deployment.

Options:
  --json             Emit structured JSON when supported.
  --plain            Use line-oriented output (default).
  --tui              Opt in to full-screen forms and rich output where supported.
  --no-color         Disable color output.
  --non-interactive  Never prompt for input.
  --verbose          Show verbose launcher output.
  -h, --help         Show this message and exit.

Platform commands:
  status
  doctor
  proxy-fragment
  maintenance status|enable|disable
  release info|check|upgrade
  database status|upgrade|restore-parity-capture|restore-parity-verify
  backup create|list|verify
  clone create|restore

Product commands (run `flow-steward COMMAND --help` for each):
  users
  projects
  workflows
  jobs
  reports
  extensions
  localization

Not available on Compact, because they run in tooling services this deployment
does not include: quality lint, quality release-gate, testing, extensions test.

Dispatched to a service a Compact deployment does not define, although the
runtime itself can do the work (AC17-R59): extensions install, extensions
uninstall.

Refused on a Compact installation, because they are dispatched to a retained CLI
container this profile does not have: config, stack (AC17-R140). To update a
Compact installation, rerun ./install.sh from updated sources; to switch it to
Scalable, run ./install.sh --deployment-profile scalable.

Refused by this launcher on a Compact installation, because it has no dispatch
for this command on this profile: environment. AC17-R115: it failed here
without being listed, so an operator met a flat refusal after reading a help
that implied they would work.
EOF
}

render_compact_release_help() {
  case "${1:-}" in
    upgrade)
      cat <<'EOF'
Usage: flow-steward release upgrade [OPTIONS]

Activate an exact newer official release while maintenance remains enabled.

Options:
  --target-version X.Y.Z  Select an exact stable release (default: latest).
  --timeout-seconds N     Bound the installer handoff (default: 60).
  --dry-run               Resolve and review without activation.
  --confirm               Confirm the reviewed release activation.
  --json                   Emit one JSON document.
  --plain                  Use line-oriented output (default).
  --tui                    Opt in to rich output and supported full-screen forms.
  --non-interactive        Never prompt for input.
  -h, --help               Show this message and exit.
EOF
      ;;
    info|check)
      cat <<EOF
Usage: flow-steward release ${1} [OPTIONS]

Read the durable installed release state${1:+ and official release availability}.

Options:
  --json             Emit one JSON document.
  --plain            Use line-oriented output (default).
  --tui              Opt in to rich output.
  -h, --help         Show this message and exit.
EOF
      ;;
    *)
      cat <<'EOF'
Usage: flow-steward release COMMAND [OPTIONS]

Commands:
  release info
  release check
  release upgrade

Run `flow-steward release COMMAND --help` for command options.
EOF
      ;;
  esac
}

run_delegated_target_command() {
  delegated_target_service=$1
  shift

  if [ -t 0 ] && [ -t 1 ]; then
    delegated_no_tty_flag=""
  else
    delegated_no_tty_flag="-T"
  fi

  delegated_git_volume_args=""
  delegated_git_common_dir=$(git -C "$root_dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  if [ -n "$delegated_git_common_dir" ] && [ -d "$delegated_git_common_dir" ]; then
    delegated_git_volume_args="-v $delegated_git_common_dir:$delegated_git_common_dir:ro"
  fi

  # pytest and Playwright are intentionally absent from the public CLI image.
  # Execute the complete typed command in its owned tooling target so its normal
  # validation, confirmation, stdin handling, rendering, and exit code remain
  # unchanged while the repository (including Git metadata) stays available.
  case "$delegated_target_service" in
    fs-audit) delegated_runtime_environment="FS_QUALITY_RUNTIME_DELEGATED=1" ;;
    fs-test-runner) delegated_runtime_environment="FS_EXTENSION_TEST_RUNTIME=1" ;;
    *) fail "Unsupported delegated CLI runtime: $delegated_target_service" 2 ;;
  esac

  # A delegated runtime is a fresh container, so nothing the operator exported on
  # the host reaches it. `--test-env NAME` asks for one variable to be handed to
  # this run — an extension's own disposable test input, which Core does not know
  # by name and which has no place in product configuration.
  #
  # Only the NAME travels here. `docker compose run -e NAME` with no `=` takes the
  # value from this launcher's own environment, so no value is ever written to a
  # command line, a config file, or a log — and no `eval` is needed to read one.
  #
  # The flags are built by prepending onto "$@" rather than by expanding a string,
  # because an argument is arbitrary text and word splitting would tear it apart.
  set -- "$delegated_target_service" python -m core.entrypoints.cli.click_app "$@"
  if [ -n "$delegated_git_volume_args" ]; then
    set -- $delegated_git_volume_args "$@"
  fi

  delegated_forward_names=""
  delegated_take_next=0
  for delegated_arg in "$@"; do
    delegated_candidate=""
    if [ "$delegated_take_next" = "1" ]; then
      delegated_take_next=0
      delegated_candidate="$delegated_arg"
    else
      case "$delegated_arg" in
        --test-env) delegated_take_next=1 ;;
        --test-env=*) delegated_candidate="${delegated_arg#--test-env=}" ;;
      esac
    fi
    [ -n "$delegated_candidate" ] || continue
    # The same grammar the runtime applies: an uppercase letter, then uppercase
    # letters, digits and underscores. Ranges are spelled out so collation cannot
    # widen them, and a rejected name is reported rather than acted on.
    case "$delegated_candidate" in
      [ABCDEFGHIJKLMNOPQRSTUVWXYZ]*) ;;
      *)
        printf '[warn] --test-env: ignoring malformed name %s\n' "$delegated_candidate" >&2
        continue
        ;;
    esac
    case "$delegated_candidate" in
      *[!ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_]*)
        printf '[warn] --test-env: ignoring malformed name %s\n' "$delegated_candidate" >&2
        continue
        ;;
    esac
    # Reversed, so prepending below restores the operator's order.
    delegated_forward_names="$delegated_candidate $delegated_forward_names"
  done
  for delegated_forward_name in $delegated_forward_names; do
    set -- -e "$delegated_forward_name" "$@"
  done

  set -- run --no-deps --rm $delegated_no_tty_flag -e "$delegated_runtime_environment" "$@"
  exec_compose_command "$@"
}

run_bootstrap_recovery_command() {
  recovery_no_tty_flag=""
  if ! [ -t 0 ] || ! [ -t 1 ]; then
    recovery_no_tty_flag="-T"
  fi
  exec_compose_command exec $recovery_no_tty_flag \
    -e "FS_DOCKER_HOST_PROJECT_ROOT=$FS_DOCKER_HOST_PROJECT_ROOT" \
    -e "FS_INSTALLER_STATE_DIR=$FS_INSTALLER_STATE_DIR" \
    -e "FS_ENV_FILE=$FS_ENV_FILE" \
    -e "FS_COMPOSE_PROJECT_NAME=$FS_COMPOSE_PROJECT_NAME" \
    -e "FS_COMPOSE_FILES=$normalized_compose_files_value" \
    -e "FS_COMPOSE_PROFILES=${FS_COMPOSE_PROFILES:-}" \
    -e "FS_STACK_ENVIRONMENT=$FS_STACK_ENVIRONMENT" \
    -e "FS_CLI_HOST_NO_COLOR=${NO_COLOR:-}" \
    fs-cli python -m core.entrypoints.cli.bootstrap_recovery "$@"
}

run_product_command() {
  if [ -t 0 ] && [ -t 1 ]; then
    product_no_tty_flag=""
  else
    product_no_tty_flag="-T"
  fi

  # AC17-R51: the product CLI lives in the application container, and that container is
  # named per deployment profile. Hardcoding `fs-app` made every product command impossible
  # on Compact, where the service is `compact-runtime` -- which is why the Compact allowlist
  # had to gate them off in the first place. The code was always present and working.
  product_target_service="fs-app"
  if [ "$compact_mode" -eq 1 ]; then
    product_target_service="compact-runtime"
  fi

  # AC17-R160: an update whose release carries a migration stops at its schema check with
  # the running fs-app still on the previous image -- whose db/migrations lacks the new
  # migration, so `database upgrade` exec'd there applied nothing, and the installer
  # would not start the new image until it had. A one-shot container of the configured
  # fs-app image carries the release's own migrations and the same database environment;
  # with no update pending it is the image already running.
  if [ "$top_level_command" = "database" ] && [ "$compact_mode" -ne 1 ]; then
    if [ -n "${NO_COLOR:-}" ]; then
      exec_compose_command run --rm --no-deps $product_no_tty_flag \
        -e "NO_COLOR=$NO_COLOR" \
        -e "TERM=${TERM:-}" \
        -e "COLORTERM=${COLORTERM:-}" \
        -e "FS_CLI_OPERATION_LOG_ROOT=/app/logs/cli" \
        -e "FS_CLI_HOST_NO_COLOR=$NO_COLOR" \
        --entrypoint python \
        fs-app -m core.entrypoints.cli.click_app "$@"
    fi
    exec_compose_command run --rm --no-deps $product_no_tty_flag \
      -e "TERM=${TERM:-}" \
      -e "COLORTERM=${COLORTERM:-}" \
      -e "FS_CLI_OPERATION_LOG_ROOT=/app/logs/cli" \
      -e "FS_CLI_HOST_NO_COLOR=" \
      --entrypoint python \
      fs-app -m core.entrypoints.cli.click_app "$@"
  fi

  if [ -n "${NO_COLOR:-}" ]; then
    exec_compose_command exec $product_no_tty_flag \
      -e "NO_COLOR=$NO_COLOR" \
      -e "TERM=${TERM:-}" \
      -e "COLORTERM=${COLORTERM:-}" \
      -e "FS_CLI_OPERATION_LOG_ROOT=/app/logs/cli" \
      -e "FS_CLI_HOST_NO_COLOR=$NO_COLOR" \
      "$product_target_service" python -m core.entrypoints.cli.click_app "$@"
  fi
  exec_compose_command exec $product_no_tty_flag \
    -e "TERM=${TERM:-}" \
    -e "COLORTERM=${COLORTERM:-}" \
    -e "FS_CLI_OPERATION_LOG_ROOT=/app/logs/cli" \
    -e "FS_CLI_HOST_NO_COLOR=" \
    "$product_target_service" python -m core.entrypoints.cli.click_app "$@"
}

# `reports diagnostics` on the host: the runtime describes itself, and only the host can add
# Docker, the containers and the doctor. Their facts go in through --input-json, the report
# comes back as JSON and is saved to one file the operator attaches to a bug report.
run_diagnostics_report() {
  diagnostics_dir="$state_dir/diagnostics"
  mkdir -p "$diagnostics_dir"
  chmod 700 "$diagnostics_dir"
  diagnostics_file="$diagnostics_dir/flow-steward-diagnostics-$(date -u +%Y%m%dT%H%M%SZ).json"
  diagnostics_scratch=$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-diagnostics.XXXXXX") ||
    fail "Could not create a temporary directory for the diagnostic report." 2

  printf '%s\n' "Collecting host facts..." >&2
  collect_host_diagnostics_payload > "$diagnostics_scratch/host.json" 2>/dev/null || :
  compose_command ps --all --format json > "$diagnostics_scratch/containers.json" 2>/dev/null || :
  printf '%s\n' "Running doctor..." >&2
  "$script_dir/flow_steward_launcher.sh" doctor --json < /dev/null \
    > "$diagnostics_scratch/doctor.json" 2>/dev/null || :
  python3 - "$diagnostics_scratch" > "$diagnostics_scratch/input.json" <<'PY' ||
import json, sys
from pathlib import Path

scratch = Path(sys.argv[1])

def read(name):
    text = (scratch / name).read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        # `compose ps --format json` prints one object per line on newer Compose.
        rows = []
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                return {"unparsed": text[-4000:]}
        return rows

host = read("host.json") or {}
containers = read("containers.json")
if isinstance(containers, dict) and "unparsed" not in containers:
    containers = [containers]
if isinstance(containers, list):
    # Labels carry host paths and nothing a bug report needs.
    kept = ("Service", "Name", "Image", "State", "Health", "Status", "ExitCode", "CreatedAt", "RunningFor")
    containers = [{key: row.get(key) for key in kept if key in row} for row in containers if isinstance(row, dict)]
if containers is not None:
    host = {**host, "containers": containers} if isinstance(host, dict) else {"facts": host, "containers": containers}
doctor = read("doctor.json")
payload = {"host": host or None}
if doctor is not None:
    payload["doctor"] = doctor if isinstance(doctor, dict) else {"rows": doctor}
print(json.dumps(payload, separators=(",", ":")))
PY
    printf '%s' '{}' > "$diagnostics_scratch/input.json"

  printf '%s\n' "Building the report in the runtime..." >&2
  diagnostics_status=0
  (run_product_command reports diagnostics --json --input-json - < "$diagnostics_scratch/input.json") \
    > "$diagnostics_scratch/envelope.json" || diagnostics_status=$?
  python3 - "$diagnostics_scratch/envelope.json" "$diagnostics_file" <<'PY' || diagnostics_status=$?
import json, sys
from pathlib import Path

raw = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").strip()
try:
    envelope = json.loads(raw.splitlines()[-1] if raw else "")
except ValueError:
    print("The runtime did not return a report. " + raw[-2000:], file=sys.stderr)
    sys.exit(4)
report = (envelope.get("result") or {}).get("report") if envelope.get("ok") else None
if not isinstance(report, dict):
    error = envelope.get("error") or {}
    print(f"The runtime could not build the report: {error.get('message') or raw[-2000:]}", file=sys.stderr)
    sys.exit(4)
target = Path(sys.argv[2])
target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
target.chmod(0o600)
PY
  rm -rf -- "$diagnostics_scratch"
  if [ "$diagnostics_status" -ne 0 ]; then
    rm -f -- "$diagnostics_file"
    exit "$diagnostics_status"
  fi
  printf '%s\n' "Diagnostic report written to $diagnostics_file"
  printf '%s\n' "It holds no secrets, logs or job content. Attach this file to your bug report."
  exit 0
}

run_extension_mutation_command() {
  if [ -t 0 ] && [ -t 1 ]; then
    mutation_no_tty_flag=""
  else
    mutation_no_tty_flag="-T"
  fi

  exec_compose_command run --no-deps --rm $mutation_no_tty_flag \
    fs-extension-manager python -P -m core.entrypoints.cli.click_app "$@"
}

verbose=0
delegated_target_service=""
extension_mutation_command=0
product_command=0
top_level_command=""
previous_arg=""
doctor_command_count=0
doctor_arguments_valid=1
doctor_json_requested=0
doctor_plain_requested=0
compact_help_requested=0
[ "$#" -eq 0 ] && compact_help_requested=1
compact_retained_cli_command=0
compact_installer_owned_command=""
for arg in "$@"; do
  if [ "$arg" = "--verbose" ]; then
    verbose=1
  fi
  case "$arg" in
    -h|--help) compact_help_requested=1 ;;
    doctor) doctor_command_count=$((doctor_command_count + 1)) ;;
    --json) doctor_json_requested=1 ;;
    --plain) doctor_plain_requested=1 ;;
    --tui|--no-color|--verbose|--non-interactive) ;;
    *) doctor_arguments_valid=0 ;;
  esac
  case "$previous_arg:$arg" in
    quality:lint|quality:release-gate) delegated_target_service="fs-audit" ;;
    extensions:test) delegated_target_service="fs-test-runner" ;;
    extensions:install|extensions:uninstall) extension_mutation_command=1 ;;
    # AC17-R88: this one is a host-side installer operation wearing a product command's
    # clothes. Issuing a token writes the installer state file and .env.prod, then runs
    # `docker compose up -d --force-recreate` against the application service -- none of
    # which exists inside the Compact runtime, and the service it names is `fs-app`, which
    # this profile does not have at all.
    users:issue-bootstrap-token) compact_installer_owned_command="users issue-bootstrap-token" ;;
    testing:smoke-test|testing:nightfire-regression|testing:fuzz-api|testing:fuzz-mcp|testing:fuzz-ui|testing:cutover-preflight|testing:cutover-smoke)
      delegated_target_service="fs-test-runner"
      ;;
  esac
  if [ -z "$top_level_command" ]; then
    case "$arg" in
      users|extensions|projects|workflows|jobs|reports|database|localization)
        top_level_command=$arg
        product_command=1
        ;;
      status|doctor|stack|environment|maintenance|release|backup|clone|config|testing|quality)
        top_level_command=$arg
        ;;
    esac
  fi
  if [ "$top_level_command" = "database" ]; then
    case "$arg" in
      restore-parity-capture|restore-parity-verify) product_command=0 ;;
    esac
  fi
  # AC17-R51: `environment` and `release` are NOT added here, although they are in
  # lifecycle_click_app's LIFECYCLE_COMMAND_DOMAINS. The retained path overrides
  # FS_COMPOSE_FILES to docker-compose.prod.yaml and exec's a long-running `fs-cli`
  # container, and a Compact installation has neither: its own compose declares `fs-cli`
  # as a one-shot `platform_runtime` runner (profiles [cli], restart "no"), and the
  # flow-steward-cli image is absent by design. `config` and `stack` failed exactly this
  # way on a live Compact install ("The final fs-cli start attempt failed"), and are now
  # refused by name further down (AC17-R140); they are marked here only so that refusal
  # can find them. Adding commands would only widen the refusal. See the R51 note.
  if [ "$top_level_command" = "config" ] || [ "$top_level_command" = "stack" ]; then
    compact_retained_cli_command=1
  fi
  previous_arg=$arg
done

# Doctor has no value-taking options: help before the argument terminator is
# metadata, even alongside --fix. After -- it is a literal check id and must
# keep the normal repair/installation-lock path, not the read-only help path.
if [ "$top_level_command" = "doctor" ]; then
  doctor_help_requested=0
  for arg in "$@"; do
    [ "$arg" = "--" ] && break
    case "$arg" in -h|--help) doctor_help_requested=1 ;; esac
  done
  compact_help_requested=$doctor_help_requested
  if [ "$doctor_help_requested" -eq 1 ]; then
    render_doctor_help
    exit 0
  fi
fi

# AC17-R162 (L1): while ./install.sh runs, only what reads is accepted -- on either
# profile, whatever the command takes or does not take itself (maintenance takes no
# lifecycle lock at all, and a Compact command never reaches the fs-cli repair below).
launcher_subcommand=""
launcher_seen_top=0
for arg in "$@"; do
  case "$arg" in -*) continue ;; esac
  if [ "$launcher_seen_top" -eq 1 ]; then
    launcher_subcommand=$arg
    break
  fi
  [ "$arg" = "$top_level_command" ] && launcher_seen_top=1
done
launcher_reads_only=0
doctor_fix_requested=0
diagnostics_passthrough=0
for arg in "$@"; do
  [ "$arg" = "--fix" ] && doctor_fix_requested=1
  case "$arg" in --json|--plain|--input-json|--input-json=*) diagnostics_passthrough=1 ;; esac
done
case "$top_level_command:$launcher_subcommand" in
  status:* | doctor:* | :* | *:status | *:list | *:show | *:validate | *:logs | *:info | *:check | reports:diagnostics) launcher_reads_only=1 ;;
esac
# doctor --fix repairs; it owns the installation like ./install.sh does.
if [ "$top_level_command" = "doctor" ] && [ "$doctor_fix_requested" -eq 1 ]; then
  launcher_reads_only=0
fi
[ "$compact_help_requested" -eq 1 ] && launcher_reads_only=1
if [ "$launcher_reads_only" -eq 0 ]; then
  if [ "$top_level_command" = "doctor" ]; then
    # doctor --fix stops an installer container nobody runs (orphan_installer), which is
    # exactly what the container half of install_sh_running would refuse it for.
    if install_sh_host_running; then
      fail "./install.sh is running ($installer_running_as); rerun this once it has finished. Nothing was changed." 6
    fi
  elif install_sh_running; then
    fail "./install.sh is running ($installer_running_as); rerun this once it has finished. Nothing was changed." 6
  fi
fi

# The doctor task: ./flow-steward doctor runs in ./install.sh's management runtime, one
# implementation for both profiles. The configuration checks of the earlier doctor run
# first, here, as their own section of its report (FS_DOCTOR_SECTION=configuration marks
# that inner run). When the launcher could not run them at all, the installation checks
# cannot run either, and doctor answers as it always has (FS_DOCTOR_SECTION=precondition).
# doctor --fix repairs the installation, not its configuration: the section is not shown.
if [ "$top_level_command" = "doctor" ] && [ -z "${FS_DOCTOR_SECTION:-}" ]; then
  # Without a scratch directory of its own the section could only write into the
  # installation, which a plain doctor never does: answer as the launcher's own doctor.
  configuration_code=$doctor_blocked_code
  configuration_report=""
  if doctor_scratch=$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-doctor.XXXXXX" 2>/dev/null); then
    configuration_code=0
    configuration_report=$(FS_DOCTOR_SECTION=configuration FS_DOCTOR_SCRATCH_DIR=$doctor_scratch \
      "$script_dir/flow_steward_launcher.sh" doctor --json 2>/dev/null) || configuration_code=$?
    rm -rf -- "$doctor_scratch"
  fi
  if [ "$configuration_code" -eq "$doctor_blocked_code" ]; then
    set -- doctor
    [ "$doctor_json_requested" -eq 1 ] && set -- "$@" --json
    [ "$doctor_plain_requested" -eq 1 ] && set -- "$@" --plain
    FS_DOCTOR_SECTION=precondition exec "$script_dir/flow_steward_launcher.sh" "$@"
  fi
  [ "$doctor_fix_requested" -eq 1 ] && configuration_report=""
  # Which ./install.sh lives now, asked on the host where its process is: the doctor
  # runs in a container, which cannot see it (orphan_installer).
  FS_DOCTOR_LIVE_INSTALLER_PID=none
  if install_sh_host_running; then
    FS_DOCTOR_LIVE_INSTALLER_PID=$installer_host_pid
  fi
  FS_DOCTOR_CONFIGURATION_REPORT=$configuration_report
  FS_MANAGEMENT_MODULE=doctor
  export FS_DOCTOR_CONFIGURATION_REPORT FS_DOCTOR_LIVE_INSTALLER_PID FS_MANAGEMENT_MODULE
  # ./install.sh takes no subcommand; the doctor's own arguments go through.
  doctor_word_seen=0
  for arg in "$@"; do
    shift
    if [ "$doctor_word_seen" -eq 0 ] && [ "$arg" = "doctor" ]; then
      doctor_word_seen=1
      continue
    fi
    set -- "$@" "$arg"
  done
  exec "$root_dir/install.sh" "$@"
fi

doctor_requested=0
if \
  [ "$doctor_command_count" -eq 1 ] &&
  [ "$doctor_arguments_valid" -eq 1 ] &&
  ! { [ "$doctor_json_requested" -eq 1 ] && [ "$doctor_plain_requested" -eq 1 ]; }; then
  doctor_requested=1
fi

read_version
read_stack_state
read_selected_cli_image

# The installed product CLI owns command metadata even when execution belongs to
# an unavailable tooling/installer service. Its lazy composition shows help without
# invoking that owner. Only metadata-only arguments qualify: a literal --help used
# as a field value must never bypass an execution refusal.
compact_metadata_help=$compact_help_requested
compact_metadata_words=0
for arg in "$@"; do
  case "$arg" in
    -h|--help|--tui|--plain|--json|--no-color|--verbose|--non-interactive) ;;
    *)
      compact_metadata_words=$((compact_metadata_words + 1))
      if [ "$compact_metadata_words" -eq 1 ] && [ "$arg" = "$top_level_command" ]; then
        :
      elif [ "$compact_metadata_words" -eq 2 ] && [ "$arg" = "$launcher_subcommand" ]; then
        :
      else
        compact_metadata_help=0
      fi
      ;;
  esac
done
[ "$compact_metadata_words" -eq 2 ] || compact_metadata_help=0
if [ "$compact_mode" -eq 1 ] && [ "$compact_metadata_help" -eq 1 ] && {
  [ -n "$delegated_target_service" ] || [ "$extension_mutation_command" -eq 1 ] ||
    [ -n "$compact_installer_owned_command" ];
}; then
  product_command=1
  delegated_target_service=""
  extension_mutation_command=0
  compact_installer_owned_command=""
fi

# AC17-R51. `database` stays with the restricted dispatcher: it already serves four of its
# commands on Compact and routing it elsewhere would change a working path. The tooling
# services below do not exist in the Compact deployment at all, so those commands get a
# diagnostic rather than a Docker error about a missing service.
compact_product_dispatch=0
if \
  [ "$compact_mode" -eq 1 ] &&
  [ "$product_command" -eq 1 ] &&
  [ "$top_level_command" != "database" ] &&
  [ -z "$delegated_target_service" ] &&
  [ "$extension_mutation_command" -ne 1 ]; then
  compact_product_dispatch=1
fi

if [ "$compact_mode" -eq 1 ]; then
  if [ -n "$delegated_target_service" ]; then
    fail "The $top_level_command command runs in the $delegated_target_service tooling service, which is not part of a Compact deployment." 2
  fi
  if [ "$extension_mutation_command" -eq 1 ]; then
    fail "Extension install and uninstall are dispatched to the fs-extension-manager service, which a Compact deployment does not define. The Compact runtime does run an extension installer, so this is a missing route rather than a missing capability; the route is the gap, not the capability." 2
  fi
  if [ -n "$compact_installer_owned_command" ]; then
    fail "$compact_installer_owned_command is an installer operation on Compact, not a runtime one: it writes installation state on the host and recreates the runtime container, neither of which the runtime itself can do. To create the first administrator, run './flow-steward users create --email <address> --display-name <name> --role admin --password-stdin --confirm', which runs in the runtime and needs no token. To get a replacement one-time setup link instead, run ./install.sh, which resumes this installation and prints the link." 2
  fi
fi

# AC17-R84: `--help` anywhere on the line used to print the root help and exit, so
# `flow-steward users --help` answered with the same page that had just told the operator
# to "run `flow-steward COMMAND --help` for each". A product command carries its own help
# in the runtime, so let it through and let the product CLI answer; the root help stays
# for a bare `--help` and for the platform commands the launcher implements itself.
if \
  [ "$compact_mode" -eq 1 ] &&
  [ "$compact_help_requested" -eq 1 ] &&
  [ "$compact_product_dispatch" -ne 1 ]; then
  if [ "$top_level_command" = "release" ]; then
    render_compact_release_help "$launcher_subcommand"
    exit 0
  fi
  render_compact_help
  exit 0
fi

# AC17-R140: `config` and `stack` dispatch to a retained fs-cli container, and a Compact
# installation has none -- its own compose declares fs-cli as a one-shot runner. They
# used to be accepted, fail to start that container, and advise "rerun ./install.sh",
# which cannot create one: an operator of a working installation was told to reinstall
# it. This launcher already knew, and said so in its help, so it now refuses the way it
# refuses `environment`, before Docker is asked anything. Placed after the help above,
# so `config --help` still answers with the page that explains this.
if [ "$compact_mode" -eq 1 ] && [ "$compact_retained_cli_command" -eq 1 ]; then
  fail "Unsupported Compact platform command: $top_level_command runs in a retained CLI container, and a Compact installation does not have one, so reinstalling will not help. To update this installation, rerun ./install.sh from updated sources; for its state, run ./flow-steward status. To switch this installation to Scalable, run ./install.sh --deployment-profile scalable." 2
fi

if ! command -v docker >/dev/null 2>&1; then
  docker_install_help="Linux/server: https://docs.docker.com/engine/install/ ; Mac/Windows/Linux desktop: https://docs.docker.com/desktop/ (includes Compose)."
  if [ "$doctor_requested" -eq 1 ]; then
    render_doctor_precondition \
      "docker_cli" \
      "Docker CLI" \
      "Docker CLI was not found on PATH." \
      "Install Docker Desktop or Docker Engine, including the Docker CLI. $docker_install_help"
  fi
  fail "Docker CLI was not found. Install Docker Desktop or Docker Engine, then rerun ./flow-steward. $docker_install_help" 127
fi

if [ "$(id -u)" = "0" ]; then
  if [ "$doctor_requested" -eq 1 ]; then
    render_doctor_precondition \
      "deployment_owner" \
      "Deployment owner" \
      "Flow Steward management commands must not run as root." \
      "Rerun Doctor as the non-root account that owns this installation."
  fi
  fail "Flow Steward management commands must not be run as root; rerun as the deployment owner without sudo." 4
fi

mkdir -p "$state_dir" "$log_dir" "$secret_input_dir" "$runtime_dir"
chmod 700 "$state_dir" "$log_dir" "$secret_input_dir" "$runtime_dir"
trap 'cleanup_docker_preflight; exit 129' HUP
trap 'cleanup_docker_preflight; exit 130' INT
trap 'cleanup_docker_preflight; exit 143' TERM
collect_docker_preflight
trap - HUP INT TERM

check_local_docker_context "$context_host"
if [ "$info_status" -ne 0 ]; then
  if [ "$doctor_requested" -eq 1 ]; then
    render_doctor_precondition \
      "docker_daemon" \
      "Docker daemon" \
      "Docker daemon is not reachable." \
      "Start Docker Desktop or Docker Engine, then rerun Doctor."
  fi
  fail "Docker daemon is not reachable. Start Docker Desktop or the Docker Engine and rerun ./flow-steward." 2
fi
compose_major=${compose_version#v}
compose_major=${compose_major%%.*}
compose_v2_available=0
case "$compose_major" in
  ""|*[!0123456789]*) ;;
  *)
    if [ "$compose_major" -ge 2 ]; then
      compose_v2_available=1
    fi
    ;;
esac
if [ "$compose_status" -ne 0 ] || [ "$compose_v2_available" -ne 1 ]; then
  compose_install_help="Linux Compose plugin: https://docs.docker.com/compose/install/linux/ ; Docker Desktop (includes Compose): https://docs.docker.com/desktop/ ."
  if [ "$doctor_requested" -eq 1 ]; then
    render_doctor_precondition \
      "docker_compose" \
      "Docker Compose" \
      "Docker Compose v2 is not available." \
      "Install the Docker Compose v2 plugin, then rerun Doctor. $compose_install_help"
  fi
  fail "Docker Compose v2 is not available. Install the Docker Compose plugin and rerun ./flow-steward. $compose_install_help" 2
fi
check_architecture
check_rootless_boundary "$docker_security_options"

if [ "$compact_mode" -eq 1 ] && ! is_immutable_image_ref "$configured_compact_image"; then
  if [ "$doctor_requested" -eq 1 ]; then
    render_doctor_precondition \
      "doctor_runtime" \
      "Doctor runtime" \
      "The exact installed Compact image is missing or invalid." \
      "Run ./install.sh to restore the Compact runtime image, then rerun Doctor."
  fi
  fail "The exact installed Compact image is missing or invalid. Rerun ./install.sh." 4
fi

if [ "$doctor_requested" -eq 1 ]; then
  git_revision_value=$(git_revision)
  image_revision=$(docker image inspect "$FS_CLI_IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' 2>/dev/null || printf '')
  expected_image_id=$(docker image inspect "$FS_CLI_IMAGE" --format '{{.Id}}' 2>/dev/null || printf '')
  if \
    ! compact_pinned_runtime_image_resolved &&
    [ "$image_revision" != "$git_revision_value" ] &&
    ! complete_exact_prebuilt_image_set_configured &&
    ! source_cli_build_inputs_unchanged "$image_revision" "$git_revision_value"; then
    render_doctor_precondition \
      "doctor_runtime" \
      "Doctor runtime" \
      "The exact installed CLI image is missing or stale; Doctor will not build or replace it." \
      "Run ./install.sh to install or resume the CLI runtime, then rerun Doctor."
  fi
  expected_image_id=$(docker image inspect "$FS_CLI_IMAGE" --format '{{.Id}}' 2>/dev/null || printf '')
  if [ -z "$expected_image_id" ]; then
    render_doctor_precondition \
      "doctor_runtime" \
      "Doctor runtime" \
      "The exact installed CLI image ID could not be read safely." \
      "Run ./install.sh to repair the CLI runtime, then rerun Doctor."
  fi

  FS_HOST_UID=$(id -u)
  FS_HOST_GID=$(id -g)
  FS_DOCKER_GID=$(docker_socket_gid)
  export FS_HOST_UID FS_HOST_GID FS_DOCKER_GID

  if ! prepare_runtime_context; then
    render_doctor_precondition \
      "doctor_runtime" \
      "Doctor runtime" \
      "The installed CLI runtime paths are invalid." \
      "Restore installation state and rerun Doctor."
  fi
  if \
    [ "$compact_mode" -eq 1 ] &&
    [ "$compact_retained_cli_command" -ne 1 ] &&
    [ "$compact_product_dispatch" -ne 1 ]; then
    run_compact_platform_command "$@"
  fi
  calculate_runtime_identity
  if ! inspect_cli_runtime; then
    render_doctor_precondition \
      "doctor_runtime" \
      "Doctor runtime" \
      "$inspection_cause" \
      "Run ./install.sh to install or resume the CLI runtime, then rerun Doctor."
  fi
  if ! host_diagnostics_payload=$(collect_host_diagnostics_payload 2>/dev/null); then
    render_doctor_precondition \
      "host_diagnostics" \
      "Host diagnostics" \
      "Host diagnostics could not be collected truthfully on the installation host." \
      "Install Python 3 on the host and rerun Doctor."
  fi
  run_cli_command "$@"
  exit 0
fi

if [ "$delegated_target_service" = "fs-test-runner" ] && [ "${FS_STACK_ENVIRONMENT:-}" = "production" ]; then
  fail "Test commands require Development. Run flow-steward environment switch development, then retry." 2
fi

trap 'release_repair_lock; cleanup_runtime_capture' EXIT
trap 'release_repair_lock; cleanup_runtime_capture; exit 129' HUP
trap 'release_repair_lock; cleanup_runtime_capture; exit 130' INT
trap 'release_repair_lock; cleanup_runtime_capture; exit 143' TERM

git_revision_value=$(git_revision)
ensure_cli_image
resolve_expected_image_id

FS_HOST_UID=$(id -u)
FS_HOST_GID=$(id -g)
FS_DOCKER_GID=$(docker_socket_gid)
export FS_HOST_UID FS_HOST_GID FS_DOCKER_GID

prepare_runtime_context || \
  fail "Flow Steward CLI runtime paths are invalid. Restore installation state and rerun ./install.sh." 2
if \
  [ "$compact_mode" -eq 1 ] &&
  [ "$compact_retained_cli_command" -ne 1 ] &&
  [ "$compact_product_dispatch" -ne 1 ]; then
  run_compact_platform_command "$@"
fi
if [ -n "$delegated_target_service" ]; then
  run_delegated_target_command "$delegated_target_service" "$@"
fi
if [ "$extension_mutation_command" -eq 1 ]; then
  run_extension_mutation_command "$@"
fi
if [ "$compact_mode" -ne 1 ] &&
  [ "$top_level_command:$launcher_subcommand" = "users:issue-bootstrap-token" ] &&
  [ "$compact_metadata_help" -ne 1 ]; then
  calculate_runtime_identity
  ensure_cli_runtime
  run_bootstrap_recovery_command "$@"
fi
if [ "$product_command" -eq 1 ]; then
  # A bare `reports diagnostics` saves the report with the host's facts; asked for --json,
  # --plain or with its own --input-json, the command answers like any other.
  if [ "$top_level_command:$launcher_subcommand" = "reports:diagnostics" ] &&
    [ "$compact_help_requested" -ne 1 ] && [ "$diagnostics_passthrough" -eq 0 ]; then
    run_diagnostics_report
  fi
  run_product_command "$@"
fi
# The installer replaces retained fs-cli during an upgrade. Its caller must
# survive that replacement to commit the handoff and report the real result.
if [ "$top_level_command" = "release" ] && [ "$launcher_subcommand" = "upgrade" ]; then
  run_isolated_cli_command "$@"
fi
calculate_runtime_identity
ensure_cli_runtime
run_cli_command "$@"
