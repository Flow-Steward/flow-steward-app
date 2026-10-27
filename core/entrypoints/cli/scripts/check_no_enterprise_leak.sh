#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v git >/dev/null 2>&1; then
  echo "[guard] git is required to verify tracked files." >&2
  exit 2
fi

if ! tracked_files="$(git ls-files 2>/dev/null)"; then
  manifest_path="${FS_BOUNDARY_TRACKED_FILES_MANIFEST:-}"
  if [[ -z "${manifest_path}" || ! -f "${manifest_path}" ]]; then
    echo "[guard] Could not enumerate tracked files; refusing to pass boundary check." >&2
    exit 2
  fi
  tracked_files="$(cat -- "${manifest_path}")"
  if [[ -z "${tracked_files}" ]]; then
    echo "[guard] Tracked-files manifest is empty; refusing to pass boundary check." >&2
    exit 2
  fi
fi

leaked_paths="$(printf '%s\n' "${tracked_files}" | grep -E '(^|/)enterprise(/|$)' || true)"

scan_files="$(
  printf '%s\n' "${tracked_files}" \
    | grep -E '^(README\.md|\.env\.example($|\.)|assets/frontend/src/|core/|db/(schema|seeds)/|docs/|infra/|install\.sh$|requirements[^/]*\.txt$|tests/)' \
    | while IFS= read -r file_path; do
        [[ -f "${file_path}" ]] && printf '%s\n' "${file_path}"
      done \
    | grep -Ev '(^|/)(check_no_enterprise_leak\.sh|deployment_doctor(_common|_static)?\.py|test_deployment_doctor\.py|test_pre_010_cleanup_guardrails\.py|dataset_selection_fixture\.json)$' \
    || true
)"

leaked_content=""
if [[ -n "${scan_files}" ]]; then
  private_identity_word="$(printf '\123\123\117')"
  private_auth_terms=(
    "$(printf '\155\146\141')"
    "$(printf '\164\157\164\160')"
    "${private_identity_word}"
    "$(printf '\157\151\144\143')"
    "$(printf '\163\141\155\154')"
    "$(printf '\154\144\141\160')"
  )
  join_regex_terms() {
    local joined=""
    local term
    for term in "$@"; do
      if [[ -n "${joined}" ]]; then
        joined="${joined}|${term}"
      else
        joined="${term}"
      fi
    done
    printf '%s' "${joined}"
  }
  private_auth_pattern="$(join_regex_terms "${private_auth_terms[@]}")"
  private_auth_proxy_pattern="$(printf '\141\165\164\150[ _-]?\160\162\157\170\171')"
  private_auth_proxy_env="$(printf '\101\125\124\110_\120\122\117\130\131')"
  private_auth_header="$(printf '\170-\141\165\164\150-\162\145\161\165\145\163\164')"
  private_word_leaks="$(
    printf '%s\n' "${scan_files}" \
      | xargs grep -InE "(^|[^[:alnum:]_])[Ee]nterprise([^[:alnum:]_]|$)|${private_identity_word} feature flag" \
      || true
  )"
  docs_identity_leaks="$(
    printf '%s\n' "${scan_files}" \
      | grep '^docs/' \
      | xargs grep -InE "(^|[^[:alnum:]_])${private_identity_word}([^[:alnum:]_]|$)" \
      || true
  )"
  legacy_private_auth_leaks="$(
    printf '%s\n' "${scan_files}" \
      | xargs grep -InEi "(^|[^[:alnum:]_])(${private_auth_pattern})([^[:alnum:]_]|$)|${private_auth_proxy_pattern}|${private_auth_proxy_env}|${private_auth_header}" \
      || true
  )"
  leaked_content="$(
    printf '%s\n%s\n%s\n' \
      "${private_word_leaks}" \
      "${docs_identity_leaks}" \
      "${legacy_private_auth_leaks}" \
      | sed '/^$/d'
  )"
fi

if [[ -n "${leaked_paths}" ]]; then
  cat >&2 <<'EOF'
[guard] Enterprise package paths detected in this OSS repository.
[guard] Move these files to a separate private repo/package before committing.
EOF
  echo "${leaked_paths}" >&2
  exit 1
fi

if [[ -n "${leaked_content}" ]]; then
  cat >&2 <<'EOF'
[guard] Private boundary wording detected in OSS-visible files.
[guard] Use neutral wording such as "private" or "external sign-in", or add a narrow guard allowlist for intentional guard tests.
EOF
  echo "${leaked_content}" >&2
  exit 1
fi

echo "[guard] OK: no tracked private package paths or boundary wording in OSS repository."
