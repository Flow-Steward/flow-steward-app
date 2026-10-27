#!/bin/bash
# Shared frontend shell freshness checks for app startup and Compose bootstrap.

frontend_shell_build_required() {
  local frontend_dir="${1:?frontend directory is required}"
  local manifest="$frontend_dir/dist/.vite/manifest.json"
  shift

  if [[ ! -f "$manifest" ]]; then
    return 0
  fi

  if [[ -d "$frontend_dir/src" ]] && find "$frontend_dir/src" -type f \
    ! -path '*/test/*' \
    ! -name '*.test.ts' \
    ! -name '*.test.tsx' \
    ! -name '*.spec.ts' \
    ! -name '*.spec.tsx' \
    -newer "$manifest" \
    -print -quit | grep -q .
  then
    return 0
  fi

  local path
  for path in \
    "$frontend_dir/index."html \
    "$frontend_dir/package.json" \
    "$frontend_dir/package-lock.json" \
    "$frontend_dir/tsconfig.json" \
    "$frontend_dir/vite.config.ts"
  do
    if [[ -f "$path" && "$path" -nt "$manifest" ]]; then
      return 0
    fi
  done

  local extra_input
  for extra_input in "$@"; do
    if [[ -d "$extra_input" ]] && find "$extra_input" -type f \
      -newer "$manifest" \
      -print -quit | grep -q .
    then
      return 0
    fi
    if [[ -f "$extra_input" && "$extra_input" -nt "$manifest" ]]; then
      return 0
    fi
  done

  return 1
}
