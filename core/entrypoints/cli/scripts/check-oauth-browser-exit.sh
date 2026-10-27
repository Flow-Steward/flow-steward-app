#!/usr/bin/env bash
set -euo pipefail

wait_status="${1:-}"
browser_exit_output="${2:-}"

if [[ ! "${wait_status}" =~ ^[0-9]+$ || "${wait_status}" != "0" ]]; then
  exit 1
fi

if [[ "${browser_exit_output}" =~ ^[0-9]+$ ]]; then
  browser_exit_code="${browser_exit_output}"
else
  compose_wait_pattern='^container "[A-Za-z0-9][A-Za-z0-9_.-]*" exited with status code ([0-9]+)$'
  if [[ "${browser_exit_output}" =~ ${compose_wait_pattern} ]]; then
    browser_exit_code="${BASH_REMATCH[1]}"
  else
    exit 1
  fi
fi

if [[ "${browser_exit_code}" != "0" ]]; then
  exit 1
fi
