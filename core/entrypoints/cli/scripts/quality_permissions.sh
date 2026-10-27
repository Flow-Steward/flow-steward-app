#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
default_root="$(cd -- "${script_dir}/../../../.." && pwd)"
expected_mode="0777"
quality_output_paths=(
  "logs/coverage"
  "logs/coverage/unit"
  "logs/quality"
  "logs/quality/evidence"
)

usage() {
  cat <<'EOF'
Usage: quality_permissions.sh <check|repair> [--repo-root PATH]

Checks or repairs the closed allowlist of non-secret quality-output directories.
EOF
}

fail() {
  printf '[quality-permissions] %s\n' "$*" >&2
  exit 1
}

path_mode() {
  local path="$1"
  local mode=""

  if mode="$(stat -f '%Lp' -- "${path}" 2>/dev/null)"; then
    printf '%s\n' "${mode}"
    return 0
  fi
  stat -c '%a' -- "${path}"
}

assert_no_symlink_components() {
  local relative_path="$1"
  local current="${repo_root}"
  local component=""
  local components=()

  IFS='/' read -r -a components <<< "${relative_path}"
  for component in "${components[@]}"; do
    current="${current}/${component}"
    if [[ -L "${current}" ]]; then
      fail "Refusing symbolic link in allowlisted path: ${current}"
    fi
  done
}

check_path() {
  local path="$1"
  local mode=""

  if [[ ! -d "${path}" ]]; then
    fail "Expected directory is missing or not a directory: ${path}"
  fi
  mode="$(path_mode "${path}")"
  mode="${mode#0}"
  if [[ "${mode}" != "${expected_mode#0}" ]]; then
    fail "Expected mode ${expected_mode} for ${path}; found 0${mode}"
  fi
  if [[ ! -w "${path}" ]]; then
    fail "Expected writable directory with mode ${expected_mode}: ${path}"
  fi
}

mode="${1:-}"
if [[ "${mode}" != "check" && "${mode}" != "repair" ]]; then
  usage >&2
  exit 2
fi
shift

repo_root="${default_root}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      if [[ $# -lt 2 ]]; then
        fail "--repo-root requires a path"
      fi
      repo_root="$2"
      shift 2
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -d "${repo_root}" ]]; then
  fail "Repository root is not a directory: ${repo_root}"
fi
repo_root="$(cd -- "${repo_root}" && pwd -P)"
if [[ "${repo_root}" == "/" ]]; then
  fail "Refusing filesystem root as repository root"
fi

for relative_path in "${quality_output_paths[@]}"; do
  path="${repo_root}/${relative_path}"
  assert_no_symlink_components "${relative_path}"
  if [[ "${mode}" == "repair" ]]; then
    if [[ -e "${path}" && ! -d "${path}" ]]; then
      fail "Allowlisted path exists but is not a directory: ${path}"
    fi
    if [[ ! -d "${path}" ]]; then
      mkdir -p -- "${path}"
    fi
    current_mode="$(path_mode "${path}")"
    current_mode="${current_mode#0}"
    if [[ "${current_mode}" != "${expected_mode#0}" ]]; then
      chmod 0777 "${path}"
    fi
    assert_no_symlink_components "${relative_path}"
  fi
  check_path "${path}"
done

printf '[quality-permissions] %s complete for %s\n' "${mode}" "${repo_root}"
