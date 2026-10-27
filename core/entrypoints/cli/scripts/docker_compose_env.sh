#!/usr/bin/env bash

FS_SCRIPTS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
FS_ROOT_DIR="$(cd -- "${FS_SCRIPTS_DIR}/../../../.." && pwd)"
FS_DOCKER_DIR="${FS_ROOT_DIR}/infra/docker"
FS_TOOLING_DIR="${FS_ROOT_DIR}/infra/tooling"
FS_COMPOSE_FILE="${FS_DOCKER_DIR}/docker-compose.yaml"
FS_COMPOSE_DEV_FILE="${FS_DOCKER_DIR}/docker-compose.dev.yaml"
FS_COMPOSE_PROD_FILE="${FS_DOCKER_DIR}/docker-compose.prod.yaml"
FS_STACK_ENV_FILE="${FS_STACK_ENV_FILE:-${FS_ROOT_DIR}/.flow-steward/compose.env}"
FS_PYTHON_CACHE_DIR="${FS_ROOT_DIR}/logs/tool-cache/python/pycache"

# Absolute, so the build-cache paths in docker-compose.yaml stay inside this
# checkout. BuildKit resolves a relative `cache_to` path against the caller's
# working directory, not the Compose file, and the old `../../.flow-steward`
# default landed two directories above the repository (the user's home). Passed
# to each Compose command rather than exported, so the tests this file's callers
# run keep the environment they had.
fs_installer_state_dir() {
  printf '%s' "${FS_INSTALLER_STATE_DIR:-${FS_ROOT_DIR}/.flow-steward}"
}

mkdir -p "${FS_PYTHON_CACHE_DIR}" 2>/dev/null || true
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-${FS_PYTHON_CACHE_DIR}}"

fs_compose_env_file_path() {
  local env_file="${FS_ENV_FILE:-.env}"
  if [[ "${env_file}" == /* ]]; then
    printf '%s\n' "${env_file}"
    return 0
  fi
  printf '%s\n' "${FS_ROOT_DIR}/${env_file}"
}

fs_trim_env_value() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s\n' "${value}"
}

fs_read_env_file_value() {
  local key="$1"
  local env_file="$2"
  local raw_line
  local value=""

  if [[ ! -f "${env_file}" ]]; then
    return 1
  fi

  while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
    if [[ "${raw_line}" =~ ^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*= ]]; then
      value="${raw_line#*=}"
      value="$(fs_trim_env_value "${value}")"
      if [[ "${#value}" -ge 2 ]]; then
        local first="${value:0:1}"
        local last="${value: -1}"
        if [[ ("${first}" == '"' && "${last}" == '"') || ("${first}" == "'" && "${last}" == "'") ]]; then
          value="${value:1:${#value}-2}"
        fi
      fi
    fi
  done < "${env_file}"

  if [[ -z "${value}" ]]; then
    return 1
  fi
  printf '%s\n' "${value}"
}

fs_load_stack_env() {
  local raw_line
  local line
  local key
  local value
  local first
  local last

  if [[ ! -f "${FS_STACK_ENV_FILE}" ]]; then
    return 0
  fi

  while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
    line="$(fs_trim_env_value "${raw_line}")"
    if [[ -z "${line}" || "${line}" == \#* ]]; then
      continue
    fi
    if [[ "${line}" =~ ^export[[:space:]]+ ]]; then
      line="$(fs_trim_env_value "${line#export}")"
    fi
    if [[ "${line}" != *=* ]]; then
      continue
    fi

    key="$(fs_trim_env_value "${line%%=*}")"
    value="$(fs_trim_env_value "${line#*=}")"
    if [[ "${#value}" -ge 2 ]]; then
      first="${value:0:1}"
      last="${value: -1}"
      if [[ ("${first}" == '"' && "${last}" == '"') || ("${first}" == "'" && "${last}" == "'") ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi

    case "${key}" in
      FS_COMPOSE_FILES | FS_COMPOSE_PROFILES | FS_COMPOSE_PROJECT_NAME | FS_ENV_FILE | FS_STACK_ENVIRONMENT)
        export "${key}=${value}"
        ;;
    esac
  done < "${FS_STACK_ENV_FILE}"
}

fs_load_stack_env
FS_COMPOSE_PROJECT_NAME="${FS_COMPOSE_PROJECT_NAME:-flow-steward}"

fs_export_fs_secret_key_from_env_file() {
  if [[ -n "${FS_SECRET_KEY:-}" ]]; then
    export FS_SECRET_KEY
    return 0
  fi

  local env_file
  local value
  env_file="$(fs_compose_env_file_path)"
  if value="$(fs_read_env_file_value "FS_SECRET_KEY" "${env_file}")"; then
    export FS_SECRET_KEY="${value}"
  fi
}

fs_compose_args() {
  local env_file
  local compose_file
  local profile
  local files="${FS_COMPOSE_FILES:-infra/docker/docker-compose.yaml}"
  for compose_file in ${files}; do
    if [[ "${compose_file}" == /* ]]; then
      printf '%s\0%s\0' "-f" "${compose_file}"
    else
      printf '%s\0%s\0' "-f" "${FS_ROOT_DIR}/${compose_file}"
    fi
  done
  env_file="$(fs_compose_env_file_path)"
  if [[ -f "${env_file}" ]]; then
    printf '%s\0%s\0' "--env-file" "${env_file}"
  fi
  for profile in ${FS_COMPOSE_PROFILES:-}; do
    printf '%s\0%s\0' "--profile" "${profile}"
  done
}

# The local BuildKit cache exporter never deletes the layers an export replaced,
# so every image build grows <state>/build-cache. Prune what no cache index
# references; best effort, it never changes the Compose command's result.
fs_prune_build_cache() {
  local cache_root
  cache_root="$(fs_installer_state_dir)/build-cache"
  [[ -d "${cache_root}" ]] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  PYTHONPATH="${FS_ROOT_DIR}" python3 -m core.application.tooling.ops.build_cache_gc \
    --quiet "${cache_root}" >&2 || true
}

fs_compose_may_build() {
  local arg
  for arg in "$@"; do
    case "${arg}" in
      build | run | up | create) return 0 ;;
    esac
  done
  return 1
}

fs_compose() {
  local compose_args=()
  local status=0
  while IFS= read -r -d '' arg; do
    compose_args+=("${arg}")
  done < <(fs_compose_args)
  FS_INSTALLER_STATE_DIR="$(fs_installer_state_dir)" \
    docker compose -p "${FS_COMPOSE_PROJECT_NAME}" "${compose_args[@]}" "$@" || status=$?
  if fs_compose_may_build "$@"; then
    fs_prune_build_cache
  fi
  return "${status}"
}

# The tag Compose gives the image it builds for a service that declares no
# `image:` key: `<project>-<service>:latest`.
fs_compose_service_image() {
  printf '%s-%s:latest\n' "${FS_COMPOSE_PROJECT_NAME}" "$1"
}

fs_compose_dev() {
  local status=0
  FS_INSTALLER_STATE_DIR="$(fs_installer_state_dir)" docker compose \
    -p "${FS_COMPOSE_PROJECT_NAME}" \
    -f "${FS_COMPOSE_FILE}" \
    -f "${FS_COMPOSE_DEV_FILE}" \
    "$@" || status=$?
  if fs_compose_may_build "$@"; then
    fs_prune_build_cache
  fi
  return "${status}"
}

fs_compose_prod() {
  docker compose \
    -p "${FS_COMPOSE_PROJECT_NAME}" \
    -f "${FS_COMPOSE_PROD_FILE}" \
    --env-file "${FS_ROOT_DIR}/.env.prod" \
    "$@"
}
