#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
# shellcheck source=core/entrypoints/cli/scripts/docker_compose_env.sh
source "${ROOT_DIR}/core/entrypoints/cli/scripts/docker_compose_env.sh"

MODE="full"
INSIDE=0
WITH_SONAR=0
REQUESTED_CHECKS=()
EVIDENCE_MODE="${FS_QUALITY_EVIDENCE_MODE:-reuse}"
TEST_PATHS=""
FINGERPRINT_SOURCE="worktree"
evidence_trust="local"
commit_sha=""
permission_preflight=0
quality_owner_tier=""
audit_image_checked=0
extension_root=""
extension_id=""
extension_source_dir=""
extension_from_stdin=0

TARGET_CANDIDATES=(
  "agents"
  "core"
  "control"
  "flows"
  "indexer"
  "integrations"
  "orchestrator"
  "registry"
  "services"
  "tools"
  "worker"
  "serve.py"
)

EXTENSION_STRICT_TARGETS=(
  core/application/extensions/command_*.py
  core/application/extensions/query_*.py
  core/application/extensions/uninstall_*.py
  core/application/extensions/binding_context.py
  core/application/extensions/lifecycle_ports.py
  core/application/extensions/service_support.py
)

SECURITY_TEST_CANDIDATES=(
  "tests/unit/test_security_policy.py"
  "tests/unit/test_security_models.py"
  "tests/unit/test_secret_scanner.py"
  "tests/unit/test_injection_guard.py"
  "tests/unit/test_pii_guard.py"
  "tests/unit/test_security_engine.py"
  "tests/unit/test_security_event_logger.py"
  "tests/unit/test_security_integration.py"
  "tests/unit/test_security_posture.py"
  "tests/unit/test_security_api.py"
  "tests/unit/test_prompt_safety.py"
  "tests/unit/test_prompt_builder_security.py"
  "tests/unit/test_prompt_trace_redaction.py"
  "tests/unit/test_registry_template_security.py"
)

append_requested_checks() {
  local raw="$1"
  local token=""
  local normalized=""
  local chunks=()

  IFS=',' read -r -a chunks <<< "${raw}"
  for token in "${chunks[@]}"; do
    normalized="$(
      printf '%s' "${token}" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//'
    )"
    if [[ -n "${normalized}" ]]; then
      REQUESTED_CHECKS+=("${normalized}")
    fi
  done
}

usage() {
  cat <<'EOF'
Usage:
  core/entrypoints/cli/scripts/lint.sh [--fast|--full|--audit] [--checks <list>] [--evidence-mode <mode>] [--fingerprint-source <source>] [--with-sonar] [--inside]

Modes:
  --fast   Tier 2 gate (dependency audits first, then security/static checks and lint).
  --full   Final committed gate: exact-SHA Tier 2 prerequisite, Tier 3 delta, cumulative evidence validation (default).
  --audit  Compatibility alias for the final committed gate (--full).
  --sonar  SonarQube scan only (starts optional local SonarQube service + runs sonar-scanner).
  --sonar-js  SonarQube JS/TS scan (fast frontend iteration profile).
  --sonar-py  SonarQube Python-only scan (fast backend iteration profile, includes tests/**).
  --sonar-full  SonarQube full profile scan (includes backend Python scope).
  --sonar-issues  Export SonarQube issues to machine-readable files (JSON/CSV).
  --sonar-backlog  Export SonarQube issues and generate developer backlog Markdown.
  --checks  Run only selected checks (comma-separated), e.g. `semgrep,pip-audit,bandit`.
            Supported: env-artifacts, ruff, ruff-format, mypy, bandit, pip-audit, semgrep, detect-secrets, ci-excludes, sonar-size, provider-neutral-core,
            tests, targeted-tests, frontend-targeted-tests, frontend-build, ui-i18n, frontend-lint, frontend-test-build, frontend-audit, extension-oauth-browser, vulture, deptry, pylint-dup, radon, xenon, frontend-sql, security-tests, zap,
            extension-static, extension-tests (both need --extension-root).
  --test-paths     pytest targets or frontend src/ targets for selected tests (space or comma separated).
                   For extension-tests they are relative to the extension root and run fail-fast.
  --extension-root  Absolute path of a standalone extension checkout for extension-static/extension-tests.
                   The checkout is streamed into fs-audit as a tar archive; the container never mounts it.
  --extension-id   Expected extension_id (default: read from the checkout's extension.yaml).
  --extension-from-stdin  Internal: the container reads the extension checkout as a tar archive on stdin.
  --evidence-mode  off, report, or reuse (default: reuse).
  --fingerprint-source  worktree, index, or commit (default: worktree).
  --trust  local or trusted_ci; trusted_ci is accepted only in exact-SHA GitHub Actions.
  --commit-sha  Commit identity required with trusted_ci commit fingerprints.

Notes:
  - When started on host machine, all modes run in on-demand `fs-audit`.
  - Fallback to `fs-app` is disabled by default; enable for non-full diagnostics with
    `FS_LINT_CONTAINER_FALLBACK=1` only if `fs-audit` cannot run. Final `--full` never falls back.
  - `--with-sonar` appends SonarQube scan after the selected gate mode.
  - `--inside` is for internal use to avoid recursive docker exec.
  - Optional OWASP ZAP baseline: set `FS_ZAP_TARGET_URL=https://...` and run `--checks zap`.
  - Semgrep tuning: `FS_SEMGREP_TIMEOUT` (seconds, default 30) and
    `FS_SEMGREP_TIMEOUT_THRESHOLD` (default 25).
  - SonarQube scan needs `SONAR_TOKEN` (create token in SonarQube UI).
  - Set `SONAR_LOCAL=0` to use external SonarQube instead of local compose profile.
  - SonarQube uses `infra/tooling/sonar-project.properties` stable profile by default.
  - `--sonar-js` uses project key `flow-steward-project-js` by default (override with `SONAR_PROJECT_KEY_JS` / `SONAR_PROJECT_NAME_JS`).
  - Local SonarQube scans ensure repository-required file-size rules are active: python:S104, javascript:S104, typescript:S104.
  - Every Sonar scan first runs the `sonar-size` precheck (about a second) and stops before starting SonarQube when a file newly crosses the S104 limit.
  - `--sonar-py` uses project key `flow-steward-project-py` by default (override with `SONAR_PROJECT_KEY_PY` / `SONAR_PROJECT_NAME_PY`).
  - `--sonar-py` includes Python tests by default; tune with `SONAR_SCANNER_JAVA_OPTS_PY` and `SONAR_SCANNER_MEM_LIMIT_PY`.
  - Use `--sonar-js` / `--sonar-py` for fast language-focused feedback, or `--sonar-full` / `SONAR_PROFILE=full` for full codebase Sonar profile.
  - Set `SONAR_MIN_FILE_LINES=1000` / `SONAR_MAX_FILE_LINES=3000` for temporary line-threshold Sonar batches; JS/TS batches also get a generated tsconfig limited to the same files. Leave unset for final full-scope scans.
  - Set `SONAR_EXPORT=1` to auto-export issues after Sonar scan.
  - Set `SONAR_BACKLOG=1` to auto-generate backlog Markdown after issue export.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fast)
      MODE="fast"
      ;;
    --full)
      MODE="full"
      ;;
    --audit)
      MODE="full"
      ;;
    --sonar)
      MODE="sonar"
      ;;
    --sonar-js)
      MODE="sonar-js"
      ;;
    --sonar-py)
      MODE="sonar-py"
      ;;
    --sonar-full)
      MODE="sonar-full"
      ;;
    --sonar-issues)
      MODE="sonar-issues"
      ;;
    --sonar-backlog)
      MODE="sonar-backlog"
      ;;
    --with-sonar)
      WITH_SONAR=1
      ;;
    --checks)
      shift
      if [[ $# -eq 0 ]]; then
        echo "[lint] --checks requires a value." >&2
        usage >&2
        exit 2
      fi
      append_requested_checks "$1"
      ;;
    --test-paths)
      shift
      if [[ $# -eq 0 ]]; then
        echo "[lint] --test-paths requires a value." >&2
        usage >&2
        exit 2
      fi
      TEST_PATHS="$1"
      ;;
    --test-paths=*)
      TEST_PATHS="${1#*=}"
      ;;
    --extension-root)
      shift
      extension_root="${1:-}"
      ;;
    --extension-root=*)
      extension_root="${1#*=}"
      ;;
    --extension-id)
      shift
      extension_id="${1:-}"
      ;;
    --extension-id=*)
      extension_id="${1#*=}"
      ;;
    --extension-from-stdin)
      extension_from_stdin=1
      ;;
    --checks=*)
      append_requested_checks "${1#*=}"
      ;;
    --evidence-mode)
      shift
      EVIDENCE_MODE="${1:-}"
      ;;
    --evidence-mode=*)
      EVIDENCE_MODE="${1#*=}"
      ;;
    --fingerprint-source)
      shift
      FINGERPRINT_SOURCE="${1:-}"
      ;;
    --fingerprint-source=*)
      FINGERPRINT_SOURCE="${1#*=}"
      ;;
    --trust)
      shift
      evidence_trust="${1:-}"
      ;;
    --trust=*)
      evidence_trust="${1#*=}"
      ;;
    --commit-sha)
      shift
      commit_sha="${1:-}"
      ;;
    --commit-sha=*)
      commit_sha="${1#*=}"
      ;;
    --permission-preflight)
      permission_preflight=1
      ;;
    --quality-owner-tier)
      shift
      if [[ $# -eq 0 ]]; then
        echo "[lint] --quality-owner-tier requires a value." >&2
        exit 2
      fi
      quality_owner_tier="$1"
      ;;
    --quality-owner-tier=*)
      quality_owner_tier="${1#*=}"
      ;;
    --inside)
      INSIDE=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[lint] Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

case "${EVIDENCE_MODE}" in off|report|reuse) ;; *) echo "[lint] Invalid evidence mode: ${EVIDENCE_MODE}" >&2; exit 2 ;; esac
# Checks run by verify.py are nested lint.sh calls; they inherit the mode so
# `--evidence-mode off` also turns off their per-file result caches.
export FS_QUALITY_EVIDENCE_MODE="${EVIDENCE_MODE}"
case "${FINGERPRINT_SOURCE}" in worktree|index|commit) ;; *) echo "[lint] Invalid fingerprint source: ${FINGERPRINT_SOURCE}" >&2; exit 2 ;; esac
case "${evidence_trust}" in local|trusted_ci) ;; *) echo "[lint] Invalid evidence trust: ${evidence_trust}" >&2; exit 2 ;; esac
case "${quality_owner_tier}" in ""|1|2|3) ;; *) echo "[lint] Invalid quality owner tier: ${quality_owner_tier}" >&2; exit 2 ;; esac
if [[ -n "${quality_owner_tier}" && ${#REQUESTED_CHECKS[@]} -eq 0 ]]; then
  echo "[lint] --quality-owner-tier requires --checks." >&2
  exit 2
fi

if [[ ${#REQUESTED_CHECKS[@]} -gt 0 ]]; then
  case "${MODE}" in
    fast|audit|sonar|sonar-js|sonar-py|sonar-full|sonar-issues|sonar-backlog)
      echo "[lint] --checks cannot be combined with --${MODE}. Use either a gate mode or --checks." >&2
      exit 2
      ;;
  esac
fi

# extension-static and extension-tests check a standalone extension repository,
# not this checkout, so they are never part of a tier run and always name the
# checkout they check. --test-paths cannot address two trees in one run.
extension_checks_requested=0
if [[ ${#REQUESTED_CHECKS[@]} -gt 0 ]]; then
  for requested_check in "${REQUESTED_CHECKS[@]}"; do
    case "${requested_check}" in
      extension-static|extension-tests) extension_checks_requested=1 ;;
    esac
  done
fi
if [[ "${extension_checks_requested}" -eq 1 && " ${REQUESTED_CHECKS[*]} " == *" targeted-tests "* ]]; then
  echo "[lint] extension-tests and targeted-tests cannot share --test-paths; run them separately." >&2
  exit 2
fi
if [[ "${extension_checks_requested}" -eq 0 && ( -n "${extension_root}" || -n "${extension_id}" || "${extension_from_stdin}" -eq 1 ) ]]; then
  echo "[lint] --extension-root and --extension-id apply only to extension-static and extension-tests." >&2
  exit 2
fi
if [[ "${extension_checks_requested}" -eq 1 && "${extension_from_stdin}" -eq 0 ]] \
  && [[ "${extension_root}" != /* || ! -f "${extension_root}/extension.yaml" ]]; then
  echo "[lint] extension checks need --extension-root <absolute path of a checkout with extension.yaml>." >&2
  exit 2
fi

cd "${ROOT_DIR}"

if [[ -x "${ROOT_DIR}/core/entrypoints/cli/scripts/check_no_enterprise_leak.sh" ]]; then
  "${ROOT_DIR}/core/entrypoints/cli/scripts/check_no_enterprise_leak.sh"
fi

DOTENV_FILE="${ROOT_DIR}/${FS_ENV_FILE:-.env}"

read_dotenv_value() {
  local key="$1"
  local line=""

  if [[ ! -f "${DOTENV_FILE}" ]]; then
    return 1
  fi

  line="$(grep -E "^[[:space:]]*${key}=" "${DOTENV_FILE}" | tail -n 1 || true)"
  if [[ -z "${line}" ]]; then
    return 1
  fi

  line="${line#*=}"
  line="$(printf '%s' "${line}" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"

  if [[ "${line}" == \"*\" && "${line}" == *\" ]]; then
    line="${line:1:${#line}-2}"
  elif [[ "${line}" == \'*\' && "${line}" == *\' ]]; then
    line="${line:1:${#line}-2}"
  fi

  printf '%s' "${line}"
}

load_sonar_env_defaults() {
  local key=""
  local value=""
  local keys=(
    "SONAR_TOKEN"
    "SONAR_PORT"
    "SONAR_WAIT_URL"
    "SONAR_HOST_URL"
    "SONAR_PROJECT_KEY"
    "SONAR_PROJECT_NAME"
    "SONAR_PROJECT_KEY_JS"
    "SONAR_PROJECT_NAME_JS"
    "SONAR_PROJECT_KEY_PY"
    "SONAR_PROJECT_NAME_PY"
    "SONAR_QUALITYGATE_WAIT"
    "SONAR_QUALITYGATE_TIMEOUT"
    "SONAR_SCANNER_JAVA_OPTS"
    "SONAR_SCANNER_JAVA_OPTS_PY"
    "SONAR_MEM_LIMIT"
    "SONAR_SCANNER_MEM_LIMIT"
    "SONAR_SCANNER_MEM_LIMIT_PY"
    "SONAR_WEB_JAVAOPTS"
    "SONAR_CE_JAVAOPTS"
    "SONAR_SEARCH_JAVAOPTS"
    "SONAR_MEM_LIMIT_FULL"
    "SONAR_SCANNER_MEM_LIMIT_FULL"
    "SONAR_SCANNER_JAVA_OPTS_FULL"
    "SONAR_WEB_JAVAOPTS_FULL"
    "SONAR_CE_JAVAOPTS_FULL"
    "SONAR_SEARCH_JAVAOPTS_FULL"
    "SONAR_EXPORT_HOST_URL"
    "SONAR_ISSUES_STATUSES"
    "SONAR_ISSUES_SEVERITIES"
    "SONAR_ISSUES_TYPES"
    "SONAR_ISSUES_PAGE_SIZE"
    "SONAR_LOCAL"
    "SONAR_PROFILE"
    "SONAR_EXPORT"
    "SONAR_EXPORT_DIR"
    "SONAR_BACKLOG"
    "SONAR_BACKLOG_PATH"
    "SONAR_BACKLOG_INPUT_CSV"
    "SONAR_BACKLOG_TOP_RULES"
    "SONAR_BACKLOG_TOP_FILES"
    "SONAR_BACKLOG_MAX_P0"
    "SONAR_BACKLOG_MAX_P1"
    "SONAR_WAIT_ATTEMPTS"
    "SONAR_WAIT_SECONDS"
    "SONAR_MIN_FILE_LINES"
    "SONAR_MAX_FILE_LINES"
  )

  for key in "${keys[@]}"; do
    if [[ -z "${!key:-}" ]]; then
      value="$(read_dotenv_value "${key}" || true)"
      if [[ -n "${value}" ]]; then
        export "${key}=${value}"
      fi
    fi
  done
}

resolve_python_bin() {
  local running_in_container=0
  if [[ -f "/.dockerenv" ]] || grep -qE '(docker|containerd|kubepods)' /proc/1/cgroup 2>/dev/null; then
    running_in_container=1
  fi

  if [[ -n "${FS_PYTHON_BIN:-}" ]]; then
    printf '%s\n' "${FS_PYTHON_BIN}"
  elif [[ "${running_in_container}" -eq 0 && -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    printf '%s\n' "${ROOT_DIR}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
  elif command -v python >/dev/null 2>&1; then
    printf '%s\n' "python"
  else
    echo "[lint] Python interpreter not found (python/python3)." >&2
    return 1
  fi
}

prepare_sonar_line_threshold_settings() {
  local min_lines="$1"
  local max_lines="$2"
  local sonar_profile="$3"
  local settings_file_host="$4"
  local settings_file_container="$5"
  local generated_dir_host=""
  local generated_file_host=""
  local generated_file_container=""
  local generated_tsconfig_host=""
  local generated_tsconfig_container=""
  local excluded_file_host=""
  local included_ts_file_host=""
  local included_ts_tmp=""
  local tmp_file=""
  local threshold_label=""
  local threshold_summary=""
  local sources_value=""
  local source_entry=""
  local source_path=""
  local line_count=""
  local relative_path=""
  local base_exclusions=""
  local threshold_exclusions=""
  local excluded_count=""
  local included_ts_count=""
  local tsconfig_paths_value=""
  local tsconfig_entry=""
  local tsconfig_path=""
  local escaped_tsconfig_path=""
  local tsconfig_file_index=0
  local old_ifs=""

  if [[ -z "${min_lines}" ]]; then
    min_lines="0"
  fi
  if [[ -z "${max_lines}" ]]; then
    max_lines="0"
  fi

  if [[ "${min_lines}" == "0" && "${max_lines}" == "0" ]]; then
    printf '%s\n' "${settings_file_host}"
    printf '%s\n' "${settings_file_container}"
    return 0
  fi

  if ! [[ "${min_lines}" =~ ^[0-9]+$ ]]; then
    echo "[lint] SONAR_MIN_FILE_LINES must be a non-negative integer." >&2
    return 1
  fi
  if ! [[ "${max_lines}" =~ ^[0-9]+$ ]]; then
    echo "[lint] SONAR_MAX_FILE_LINES must be a non-negative integer." >&2
    return 1
  fi
  if (( min_lines > 0 && max_lines > 0 && min_lines > max_lines )); then
    echo "[lint] SONAR_MIN_FILE_LINES cannot be greater than SONAR_MAX_FILE_LINES." >&2
    return 1
  fi
  if (( min_lines > 0 )); then
    threshold_label="min-${min_lines}"
    threshold_summary="SONAR_MIN_FILE_LINES=${min_lines}"
  fi
  if (( max_lines > 0 )); then
    if [[ -n "${threshold_label}" ]]; then
      threshold_label="${threshold_label}-max-${max_lines}"
      threshold_summary="${threshold_summary} SONAR_MAX_FILE_LINES=${max_lines}"
    else
      threshold_label="max-${max_lines}"
      threshold_summary="SONAR_MAX_FILE_LINES=${max_lines}"
    fi
  fi

  sources_value="$(
    sed -n 's/^sonar\.sources=//p' "${settings_file_host}" \
      | head -n 1
  )"
  if [[ -z "${sources_value}" ]]; then
    echo "[lint] Cannot apply Sonar line thresholds: ${settings_file_host} has no sonar.sources." >&2
    return 1
  fi

  generated_dir_host="${ROOT_DIR}/logs/quality/sonar/generated"
  mkdir -p "${generated_dir_host}"
  generated_file_host="${generated_dir_host}/${sonar_profile}-${threshold_label}-lines.properties"
  generated_file_container="/usr/src/logs/quality/sonar/generated/${sonar_profile}-${threshold_label}-lines.properties"
  excluded_file_host="${generated_dir_host}/${sonar_profile}-excluded-outside-${threshold_label}-lines.txt"
  tmp_file="${excluded_file_host}.tmp"
  : > "${tmp_file}"

  old_ifs="${IFS}"
  IFS=","
  for source_entry in ${sources_value}; do
    IFS="${old_ifs}"
    source_entry="$(
      printf '%s' "${source_entry}" \
        | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//'
    )"
    source_path="${ROOT_DIR}/${source_entry}"

    if [[ -f "${source_path}" ]]; then
      line_count="$(wc -l < "${source_path}" | tr -d '[:space:]')"
      if (( (min_lines > 0 && line_count < min_lines) || (max_lines > 0 && line_count > max_lines) )); then
        printf '%s\n' "${source_entry}" >> "${tmp_file}"
      fi
    elif [[ -d "${source_path}" ]]; then
      find "${source_path}" \
        \( -path '*/node_modules/*' -o \
           -path '*/dist/*' -o \
           -path '*/test-results/*' -o \
           -path '*/vendor/*' -o \
           -path '*/src/test/*' -o \
           -path '*/__pycache__/*' \) -prune -o \
        -type f \( \
          -name '*.py' -o -name '*.js' -o -name '*.ts' -o -name '*.tsx' -o \
          -name '*.css' -o -name '*.html' -o -name '*.xml' -o -name '*.yaml' -o \
          -name '*.yml' -o -name '*.json' -o -name '*.sql' \
        \) \
        ! -name '*.test.ts' \
        ! -name '*.test.tsx' \
        ! -name '*.spec.ts' \
        ! -name '*.spec.tsx' \
        ! -name 'package-lock.json' \
        ! -name 'yarn.lock' \
        ! -name 'pnpm-lock.yaml' \
        -print \
        | while IFS= read -r source_path; do
            line_count="$(wc -l < "${source_path}" | tr -d '[:space:]')"
            if (( (min_lines > 0 && line_count < min_lines) || (max_lines > 0 && line_count > max_lines) )); then
              relative_path="${source_path#"${ROOT_DIR}/"}"
              printf '%s\n' "${relative_path}" >> "${tmp_file}"
            fi
          done
    fi
    IFS=","
  done
  IFS="${old_ifs}"

  sort -u "${tmp_file}" > "${excluded_file_host}"
  rm -f "${tmp_file}"
  excluded_count="$(wc -l < "${excluded_file_host}" | tr -d '[:space:]')"

  if (( excluded_count == 0 )); then
    echo "[lint] ${threshold_summary}: no source files fall outside the line threshold." >&2
    printf '%s\n' "${settings_file_host}"
    printf '%s\n' "${settings_file_container}"
    return 0
  fi

  base_exclusions="$(
    sed -n 's/^sonar\.exclusions=//p' "${settings_file_host}" \
      | head -n 1
  )"
  threshold_exclusions="$(paste -sd, "${excluded_file_host}")"
  if [[ -n "${base_exclusions}" ]]; then
      threshold_exclusions="${base_exclusions},${threshold_exclusions}"
  fi

  tsconfig_paths_value="$(
    sed -n 's/^sonar\.typescript\.tsconfigPaths=//p' "${settings_file_host}" \
      | head -n 1
  )"
  if [[ -n "${tsconfig_paths_value}" ]]; then
    included_ts_file_host="${generated_dir_host}/${sonar_profile}-included-ts-${threshold_label}-lines.txt"
    included_ts_tmp="${included_ts_file_host}.tmp"
    : > "${included_ts_tmp}"

    old_ifs="${IFS}"
    IFS=","
    for source_entry in ${sources_value}; do
      IFS="${old_ifs}"
      source_entry="$(
        printf '%s' "${source_entry}" \
          | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//'
      )"
      source_path="${ROOT_DIR}/${source_entry}"

      if [[ -f "${source_path}" ]]; then
        case "${source_path}" in
          *.ts|*.tsx)
            relative_path="${source_path#"${ROOT_DIR}/"}"
            if ! grep -Fxq "${relative_path}" "${excluded_file_host}"; then
              printf '%s\n' "${relative_path}" >> "${included_ts_tmp}"
            fi
            ;;
        esac
      elif [[ -d "${source_path}" ]]; then
        find "${source_path}" \
          \( -path '*/node_modules/*' -o \
             -path '*/dist/*' -o \
             -path '*/test-results/*' -o \
             -path '*/vendor/*' -o \
             -path '*/src/test/*' -o \
             -path '*/__pycache__/*' \) -prune -o \
          -type f \( -name '*.ts' -o -name '*.tsx' \) \
          ! -name '*.test.ts' \
          ! -name '*.test.tsx' \
          ! -name '*.spec.ts' \
          ! -name '*.spec.tsx' \
          -print \
          | while IFS= read -r source_path; do
              relative_path="${source_path#"${ROOT_DIR}/"}"
              if ! grep -Fxq "${relative_path}" "${excluded_file_host}"; then
                printf '%s\n' "${relative_path}" >> "${included_ts_tmp}"
              fi
            done
      fi
      IFS=","
    done
    IFS="${old_ifs}"

    sort -u "${included_ts_tmp}" > "${included_ts_file_host}"
    rm -f "${included_ts_tmp}"
    included_ts_count="$(wc -l < "${included_ts_file_host}" | tr -d '[:space:]')"
    generated_tsconfig_host="${generated_dir_host}/${sonar_profile}-${threshold_label}-lines.tsconfig.json"
    generated_tsconfig_container="/usr/src/logs/quality/sonar/generated/${sonar_profile}-${threshold_label}-lines.tsconfig.json"

    {
      printf '{\n'
      printf '  "extends": "../../../../assets/frontend/tsconfig.json",\n'
      printf '  "compilerOptions": {\n'
      printf '    "allowJs": false,\n'
      printf '    "checkJs": false,\n'
      printf '    "noEmit": true\n'
      printf '  },\n'
      printf '  "files": [\n'
      tsconfig_file_index=0
      while IFS= read -r tsconfig_entry; do
        [[ -z "${tsconfig_entry}" ]] && continue
        tsconfig_path="../../../../${tsconfig_entry}"
        escaped_tsconfig_path="$(
          printf '%s' "${tsconfig_path}" \
            | sed 's/\\/\\\\/g; s/"/\\"/g'
        )"
        if (( tsconfig_file_index > 0 )); then
          printf ',\n'
        fi
        printf '    "%s"' "${escaped_tsconfig_path}"
        tsconfig_file_index=$((tsconfig_file_index + 1))
      done < "${included_ts_file_host}"
      printf '\n'
      printf '  ]\n'
      printf '}\n'
    } > "${generated_tsconfig_host}"
  fi

  awk \
    -v exclusions="${threshold_exclusions}" \
    -v tsconfig="${generated_tsconfig_container}" '
    BEGIN { wrote_exclusions = 0; wrote_tsconfig = 0 }
    /^sonar\.exclusions=/ {
      if (!wrote_exclusions) {
        print "sonar.exclusions=" exclusions
        wrote_exclusions = 1
      }
      next
    }
    /^sonar\.typescript\.tsconfigPaths=/ {
      if (tsconfig != "" && !wrote_tsconfig) {
        print "sonar.typescript.tsconfigPaths=" tsconfig
        wrote_tsconfig = 1
      }
      if (tsconfig == "") {
        print
      }
      next
    }
    { print }
    END {
      if (!wrote_exclusions) {
        print "sonar.exclusions=" exclusions
      }
      if (tsconfig != "" && !wrote_tsconfig) {
        print "sonar.typescript.tsconfigPaths=" tsconfig
      }
    }
  ' "${settings_file_host}" > "${generated_file_host}"

  echo "[lint] ${threshold_summary}: temporarily excluding ${excluded_count} source file(s)." >&2
  echo "[lint] Sonar line-threshold exclusions: ${excluded_file_host#"${ROOT_DIR}/"}" >&2
  if [[ -n "${generated_tsconfig_host}" ]]; then
    echo "[lint] ${threshold_summary}: generated TypeScript batch config with ${included_ts_count} file(s)." >&2
    echo "[lint] Generated Sonar TypeScript config: ${generated_tsconfig_host#"${ROOT_DIR}/"}" >&2
  fi
  echo "[lint] Generated Sonar settings: ${generated_file_host#"${ROOT_DIR}/"}" >&2

  printf '%s\n' "${generated_file_host}"
  printf '%s\n' "${generated_file_container}"
}

# The files that decide which tools the fs-audit image carries: the Dockerfile
# (base image digests, apt and global npm pins, every stage the `audit` target
# inherits) and the requirement sets those stages install -- runtime-dependencies
# installs requirements-runtime.lock, audit-tooling the other three. Source is
# deliberately absent except for the public SDK: the gate bind-mounts the
# checkout over /app, but pip installs the SDK into site-packages during the
# audit build. Its package inputs must invalidate the installed wheel too.
audit_toolchain_digest() {
  local root="$1"
  local input=""
  local blob=""
  local inputs=(
    infra/docker/Dockerfile
    requirements-runtime.lock
    requirements-audit.txt
    requirements-semgrep.txt
    requirements-cli.lock
  )
  {
    for input in "${inputs[@]}"; do
      blob="$(git hash-object --no-filters -- "${root}/${input}" 2>/dev/null)" || blob="missing"
      printf '%s %s\n' "${blob}" "${input}"
    done
    if [[ -d "${root}/core/infrastructure/extension_sdk/public" ]]; then
      git -C "${root}" ls-files -z --cached --others --exclude-standard -- \
        core/infrastructure/extension_sdk/public | LC_ALL=C sort -zu | while IFS= read -r -d '' input; do
        blob="$(git hash-object --no-filters -- "${root}/${input}" 2>/dev/null)" || blob="missing"
        printf '%s %s\n' "${blob}" "${input}"
      done
    fi
  } | git hash-object --no-filters --stdin
}

# Compose builds a service image only when it is missing, so a ruff, mypy or
# semgrep upgrade used to keep running the previous tools until someone rebuilt
# by hand, while the evidence fingerprint (which hashes the same files) already
# treated the upgrade as a new toolchain. The image records the digest it was
# built from; a different, missing or absent label means rebuild before any run.
ensure_audit_image_current() {
  local image=""
  local wanted=""
  local built=""

  [[ "${audit_image_checked}" -eq 0 ]] || return 0
  wanted="$(audit_toolchain_digest "${ROOT_DIR}")" || return $?
  image="$(fs_compose_service_image fs-audit)"
  if ! built="$(
    docker image inspect \
      --format '{{ index .Config.Labels "org.flowsteward.audit-toolchain" }}' \
      "${image}" 2>/dev/null
  )"; then
    echo "[lint] ${image} is missing; building it for toolchain ${wanted:0:12}."
  elif [[ "${built}" == "${wanted}" ]]; then
    audit_image_checked=1
    return 0
  elif [[ -z "${built}" || "${built}" == "<no value>" || "${built}" == "unknown" ]]; then
    echo "[lint] ${image} carries no toolchain digest; rebuilding it for toolchain ${wanted:0:12}."
  else
    echo "[lint] ${image} was built from toolchain ${built:0:12}, the inputs are now ${wanted:0:12}; rebuilding."
  fi
  if ! fs_compose build --build-arg "AUDIT_TOOLCHAIN_DIGEST=${wanted}" fs-audit; then
    echo "[lint] Building ${image} failed; the gate does not run on a stale toolchain." >&2
    return 2
  fi
  audit_image_checked=1
}

# Nested test helpers must use this run's validated image even when an inner
# dispatcher changes Compose projects. An inherited selector is never trusted.
resolve_audit_test_image() {
  local image=""
  local wanted=""
  local metadata=""
  local resolved_id=""
  local resolved_toolchain=""

  unset FS_TEST_AUDIT_IMAGE_ID
  image="$(fs_compose_service_image fs-audit)"
  wanted="$(audit_toolchain_digest "${ROOT_DIR}")" || return $?
  if ! metadata="$(
    docker image inspect \
      --format '{{.Id}} {{ index .Config.Labels "org.flowsteward.audit-toolchain" }}' \
      "${image}" 2>/dev/null
  )"; then
    echo "[lint] Cannot resolve the validated audit image identity for ${image}." >&2
    return 2
  fi
  if [[ "${metadata}" =~ ^(sha256:[0-9a-f]{64})\ ([0-9a-f]{40})$ ]]; then
    resolved_id="${BASH_REMATCH[1]}"
    resolved_toolchain="${BASH_REMATCH[2]}"
  else
    echo "[lint] ${image} returned a missing or malformed immutable audit image identity." >&2
    return 2
  fi
  if [[ "${resolved_toolchain}" != "${wanted}" ]]; then
    echo "[lint] ${image} does not carry the current audit toolchain; refusing test consumers." >&2
    return 2
  fi
  export FS_TEST_AUDIT_IMAGE_ID="${resolved_id}"
}

run_sonar_quality_command() {
  local token_required="$1"
  local command_name="$2"
  shift 2

  ensure_audit_image_current || return $?

  if [[ "${token_required}" == "1" ]]; then
    if [[ -z "${SONAR_TOKEN:-}" ]]; then
      echo "[lint] SONAR_TOKEN is required for quality.${command_name}." >&2
      return 1
    fi
    printf '%s' "${SONAR_TOKEN}" | fs_compose run --no-deps --rm -T fs-audit \
      python -m core.entrypoints.cli quality "${command_name}" \
      --confirm \
      --sonar-token-stdin \
      "$@"
    return
  fi

  fs_compose run --no-deps --rm -T fs-audit \
    python -m core.entrypoints.cli quality "${command_name}" \
    --confirm \
    "$@"
}

sonar_container_path() {
  local host_path="$1"

  if [[ "${host_path}" == "${ROOT_DIR}" ]]; then
    printf '%s\n' "/app"
  elif [[ "${host_path}" == "${ROOT_DIR}/"* ]]; then
    printf '/app/%s\n' "${host_path#"${ROOT_DIR}/"}"
  else
    printf '%s\n' "${host_path}"
  fi
}

run_sonar_required_rule_sync() {
  local sonar_host_url="$1"

  echo
  echo "==> SonarQube required Quality Profile rules"
  run_sonar_quality_command \
    1 \
    sonar-ensure-rules \
    --host-url "${sonar_host_url}"
}

run_sonar_issue_export() {
  local sonar_host_url=""
  local sonar_project_key=""
  local sonar_local=""
  local export_host_url=""
  local export_dir=""
  local statuses=""
  local severities=""
  local types=""
  local page_size=""

  load_sonar_env_defaults

  if [[ -z "${SONAR_TOKEN:-}" ]]; then
    echo "[lint] SONAR_TOKEN is not set (cannot export Sonar issues)." >&2
    return 1
  fi

  sonar_host_url="${SONAR_HOST_URL:-http://sonarqube:9000}"
  sonar_project_key="${SONAR_PROJECT_KEY:-flow-steward-project}"
  sonar_local="${SONAR_LOCAL:-1}"

  if [[ -n "${SONAR_EXPORT_HOST_URL:-}" ]]; then
    export_host_url="${SONAR_EXPORT_HOST_URL}"
  elif [[ "${sonar_local}" == "1" ]]; then
    export_host_url="${sonar_host_url}"
  else
    export_host_url="${sonar_host_url}"
  fi

  export_dir="${SONAR_EXPORT_DIR:-${ROOT_DIR}/logs/quality/sonar}"
  export_dir="$(sonar_container_path "${export_dir}")"
  statuses="${SONAR_ISSUES_STATUSES:-OPEN,CONFIRMED,REOPENED}"
  severities="${SONAR_ISSUES_SEVERITIES:-}"
  types="${SONAR_ISSUES_TYPES:-}"
  page_size="${SONAR_ISSUES_PAGE_SIZE:-500}"

  local command_args=(
    --host-url "${export_host_url}"
    --project-key "${sonar_project_key}"
    --output-dir "${export_dir}"
    --page-size "${page_size}"
  )
  if [[ -n "${statuses}" ]]; then
    command_args+=(--statuses "${statuses}")
  fi
  if [[ -n "${severities}" ]]; then
    command_args+=(--severities "${severities}")
  fi
  if [[ -n "${types}" ]]; then
    command_args+=(--types "${types}")
  fi

  echo
  echo "==> SonarQube issues export"
  run_sonar_quality_command \
    1 \
    sonar-export-issues \
    "${command_args[@]}"
}

run_sonar_backlog_generate() {
  local output_path=""
  local input_csv=""
  local input_dir=""

  load_sonar_env_defaults

  output_path="${SONAR_BACKLOG_PATH:-${ROOT_DIR}/logs/quality/sonar/sonar-dev-backlog.md}"
  input_csv="${SONAR_BACKLOG_INPUT_CSV:-}"
  input_dir="${SONAR_EXPORT_DIR:-${ROOT_DIR}/logs/quality/sonar}"
  output_path="$(sonar_container_path "${output_path}")"
  input_dir="$(sonar_container_path "${input_dir}")"
  if [[ -n "${input_csv}" ]]; then
    input_csv="$(sonar_container_path "${input_csv}")"
  fi

  local command_args=(
    --output "${output_path}"
    --input-dir "${input_dir}"
  )
  if [[ -n "${input_csv}" ]]; then
    command_args+=(--input-csv "${input_csv}")
  fi
  if [[ -n "${SONAR_BACKLOG_TOP_RULES:-}" ]]; then
    command_args+=(--top-rules "${SONAR_BACKLOG_TOP_RULES}")
  fi
  if [[ -n "${SONAR_BACKLOG_TOP_FILES:-}" ]]; then
    command_args+=(--top-files "${SONAR_BACKLOG_TOP_FILES}")
  fi
  if [[ -n "${SONAR_BACKLOG_MAX_P0:-}" ]]; then
    command_args+=(--max-p0 "${SONAR_BACKLOG_MAX_P0}")
  fi
  if [[ -n "${SONAR_BACKLOG_MAX_P1:-}" ]]; then
    command_args+=(--max-p1 "${SONAR_BACKLOG_MAX_P1}")
  fi

  echo
  echo "==> SonarQube developer backlog generation"
  run_sonar_quality_command \
    0 \
    sonar-generate-backlog \
    "${command_args[@]}"
}

normalize_sonar_python_coverage_report() {
  local source_report="$1"
  local target_report="$2"
  local source_report_container=""
  local target_report_container=""

  if [[ "${source_report}" != "${ROOT_DIR}/"* || "${target_report}" != "${ROOT_DIR}/"* ]]; then
    echo "[lint] Sonar coverage reports must be inside the repository." >&2
    return 2
  fi

  source_report_container="/app/${source_report#"${ROOT_DIR}/"}"
  target_report_container="/app/${target_report#"${ROOT_DIR}/"}"
  ensure_audit_image_current || return $?
  fs_compose run --no-deps --rm -T fs-audit \
    python -m core.application.tooling.quality.sonar_coverage \
      "${source_report_container}" \
      "${target_report_container}" \
      /app
}

run_host_sonar_scan() {
  local sonar_profile="${1:-}"
  local sonar_wait_url=""
  local sonar_host_url=""
  local sonar_project_key=""
  local sonar_project_name=""
  local sonar_settings_file_host=""
  local sonar_settings_file_container=""
  local quality_gate_wait=""
  local quality_gate_timeout=""
  local scanner_java_opts=""
  local sonar_local=""
  local max_attempts=""
  local sleep_seconds=""
  local sonar_min_file_lines=""
  local sonar_max_file_lines=""
  local prepared_settings_paths=""
  local coverage_report_host=""
  local coverage_report_container=""
  local normalized_coverage_report_host=""
  local normalized_coverage_report_container=""
  local js_coverage_report_host=""
  local js_coverage_report_container=""
  local sonar_args=()
  local scanner_run_args=(--rm -T)
  local attempt=0
  local status_line=""
  local status_payload=""
  local status_value=""

  if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "[lint] Docker Compose is required for SonarQube scan mode." >&2
    return 1
  fi

  load_sonar_env_defaults
  if [[ -z "${sonar_profile}" ]]; then
    sonar_profile="${SONAR_PROFILE:-stable}"
  fi

  if [[ -z "${SONAR_TOKEN:-}" ]]; then
    echo "[lint] SONAR_TOKEN is not set." >&2
    if [[ -f "${DOTENV_FILE}" ]]; then
      echo "[lint] Add SONAR_TOKEN=... to ${DOTENV_FILE} or pass it in the command." >&2
    fi
    echo "[lint] Create a token in SonarQube (My Account -> Security) and rerun." >&2
    return 1
  fi

  sonar_wait_url="${SONAR_WAIT_URL:-http://localhost:${SONAR_PORT:-9000}}"
  sonar_host_url="${SONAR_HOST_URL:-http://sonarqube:9000}"
  sonar_project_key="${SONAR_PROJECT_KEY:-flow-steward-project}"
  sonar_project_name="${SONAR_PROJECT_NAME:-Flow Steward Project}"
  quality_gate_wait="${SONAR_QUALITYGATE_WAIT:-true}"
  quality_gate_timeout="${SONAR_QUALITYGATE_TIMEOUT:-300}"
  scanner_java_opts="${SONAR_SCANNER_JAVA_OPTS:-}"
  sonar_local="${SONAR_LOCAL:-1}"
  max_attempts="${SONAR_WAIT_ATTEMPTS:-120}"
  sleep_seconds="${SONAR_WAIT_SECONDS:-5}"

  case "${sonar_profile}" in
    stable)
      sonar_settings_file_host="${FS_TOOLING_DIR}/sonar-project.properties"
      sonar_settings_file_container="/usr/src/infra/tooling/sonar-project.properties"
      ;;
    js)
      sonar_settings_file_host="${FS_TOOLING_DIR}/sonar-project.js.properties"
      sonar_settings_file_container="/usr/src/infra/tooling/sonar-project.js.properties"
      sonar_project_key="${SONAR_PROJECT_KEY_JS:-flow-steward-project-js}"
      sonar_project_name="${SONAR_PROJECT_NAME_JS:-Flow Steward Project (JS)}"
      ;;
    py)
      sonar_settings_file_host="${FS_TOOLING_DIR}/sonar-project.py.properties"
      sonar_settings_file_container="/usr/src/infra/tooling/sonar-project.py.properties"
      sonar_project_key="${SONAR_PROJECT_KEY_PY:-flow-steward-project-py}"
      sonar_project_name="${SONAR_PROJECT_NAME_PY:-Flow Steward Project (Python)}"
      ;;
    full)
      sonar_settings_file_host="${FS_TOOLING_DIR}/sonar-project.full.properties"
      sonar_settings_file_container="/usr/src/infra/tooling/sonar-project.full.properties"
      ;;
    *)
      echo "[lint] Unsupported Sonar profile: ${sonar_profile}. Use stable|js|py|full." >&2
      return 2
      ;;
  esac

  # The scan takes 40+ minutes and fails its gate on a new S104 issue; the
  # precheck predicts that from the same file selection in about a second.
  ensure_audit_image_current || return $?
  echo "==> Sonar file-size precheck (${sonar_profile})"
  if ! fs_compose run --no-deps --rm -T fs-audit \
    python -m core.application.tooling.quality.sonar_size_precheck \
      --root /app \
      --profile "${sonar_profile}"; then
    echo "[lint] Sonar file-size precheck failed; fix it before the Sonar scan." >&2
    return 1
  fi

  if [[ "${sonar_profile}" == "full" ]]; then
    # Full profile defaults are tuned for local Docker Desktop environments
    # where total memory is often ~8GB. Keep conservative defaults to avoid
    # scanner OOM kills (exit code 137) during Python/JS sensors.
    export SONAR_MEM_LIMIT="${SONAR_MEM_LIMIT_FULL:-${SONAR_MEM_LIMIT:-6g}}"
    export SONAR_SCANNER_MEM_LIMIT="${SONAR_SCANNER_MEM_LIMIT_FULL:-${SONAR_SCANNER_MEM_LIMIT:-8g}}"
    export SONAR_SCANNER_JAVA_OPTS="${SONAR_SCANNER_JAVA_OPTS_FULL:-${SONAR_SCANNER_JAVA_OPTS:--Xmx1g}}"
    export SONAR_WEB_JAVAOPTS="${SONAR_WEB_JAVAOPTS_FULL:-${SONAR_WEB_JAVAOPTS:--Xms512m -Xmx1024m}}"
    export SONAR_CE_JAVAOPTS="${SONAR_CE_JAVAOPTS_FULL:-${SONAR_CE_JAVAOPTS:--Xms512m -Xmx2048m}}"
    export SONAR_SEARCH_JAVAOPTS="${SONAR_SEARCH_JAVAOPTS_FULL:-${SONAR_SEARCH_JAVAOPTS:--Xms512m -Xmx1024m}}"
    scanner_java_opts="${SONAR_SCANNER_JAVA_OPTS}"
  elif [[ "${sonar_profile}" == "py" ]]; then
    # Python profile includes backend tests; bump scanner heap/limit to avoid
    # OOM on semantic analysis of large test modules.
    export SONAR_SCANNER_MEM_LIMIT="${SONAR_SCANNER_MEM_LIMIT:-${SONAR_SCANNER_MEM_LIMIT_PY:-6g}}"
    export SONAR_SCANNER_JAVA_OPTS="${SONAR_SCANNER_JAVA_OPTS:-${SONAR_SCANNER_JAVA_OPTS_PY:--Xmx2g}}"
    scanner_java_opts="${SONAR_SCANNER_JAVA_OPTS}"
  fi

  if [[ "${sonar_profile}" == "full" || "${sonar_profile}" == "py" ]]; then
    # pytest coverage runs inside fs-audit at /app, while sonar-scanner runs at
    # /usr/src. Normalize Cobertura source path so Sonar can resolve files.
    #
    # Coverage lookup priority:
    #   1. logs/coverage/critical-matrix/coverage.xml — broadest, produced by
    #      core/entrypoints/cli/scripts/coverage_rebaseline.sh --critical-matrix
    #   2. logs/coverage/unit/coverage.xml — unit-only, produced by lint --fast/--checks
    #
    # NOTE: unit-only coverage intentionally under-reports project-wide coverage.
    # If only unit coverage is available, Sonar will show low numbers — that is correct.
    # Run core/entrypoints/cli/scripts/coverage_rebaseline.sh --critical-matrix before --sonar for better signal.
    coverage_report_host=""
    for candidate in \
      "${ROOT_DIR}/logs/coverage/critical-matrix/coverage.xml" \
      "${ROOT_DIR}/logs/coverage/unit/coverage.xml"; do
      if [[ -f "${candidate}" ]]; then
        coverage_report_host="${candidate}"
        echo "[lint] Sonar Python coverage: using ${candidate#"${ROOT_DIR}/"}"
        break
      fi
    done
    normalized_coverage_report_host="${ROOT_DIR}/logs/quality/sonar/coverage.sonar.xml"
    normalized_coverage_report_container="/usr/src/logs/quality/sonar/coverage.sonar.xml"
    if [[ -n "${coverage_report_host}" ]]; then
      normalize_sonar_python_coverage_report "${coverage_report_host}" "${normalized_coverage_report_host}"
      coverage_report_container="${normalized_coverage_report_container}"
      sonar_args+=("-Dsonar.python.coverage.reportPaths=${coverage_report_container}")
    else
      echo "[lint] Sonar Python coverage: no coverage.xml found — skipping coverage report."
      echo "[lint] Run: core/entrypoints/cli/scripts/coverage_rebaseline.sh --critical-matrix  to generate one."
    fi
  fi

  if [[ "${sonar_profile}" == "stable" || "${sonar_profile}" == "js" || "${sonar_profile}" == "full" ]]; then
    js_coverage_report_host="${ROOT_DIR}/logs/coverage/lcov.info"
    js_coverage_report_container="/usr/src/logs/coverage/lcov.info"
    if [[ -f "${js_coverage_report_host}" ]]; then
      sonar_args+=("-Dsonar.javascript.lcov.reportPaths=${js_coverage_report_container}")
    else
      echo "[lint] Sonar JavaScript coverage: no logs/coverage/lcov.info found — skipping LCOV report."
    fi
  fi

  if [[ ! -f "${sonar_settings_file_host}" ]]; then
    echo "[lint] Sonar settings file not found: ${sonar_settings_file_host}" >&2
    return 1
  fi

  sonar_min_file_lines="${SONAR_MIN_FILE_LINES:-${SONAR_MIN_LINES:-}}"
  sonar_max_file_lines="${SONAR_MAX_FILE_LINES:-${SONAR_MAX_LINES:-}}"
  prepared_settings_paths="$(
    prepare_sonar_line_threshold_settings \
      "${sonar_min_file_lines}" \
      "${sonar_max_file_lines}" \
      "${sonar_profile}" \
      "${sonar_settings_file_host}" \
      "${sonar_settings_file_container}"
  )"
  sonar_settings_file_host="$(printf '%s\n' "${prepared_settings_paths}" | tail -n 2 | head -n 1)"
  sonar_settings_file_container="$(printf '%s\n' "${prepared_settings_paths}" | tail -n 1)"

  if [[ "${sonar_local}" == "1" ]]; then
    echo "[lint] Starting SonarQube service (profile=quality)..."
    if [[ "${sonar_profile}" == "full" ]]; then
      fs_compose --profile quality up -d --force-recreate sonarqube
    else
      fs_compose --profile quality up -d sonarqube
    fi

    echo "[lint] Waiting for SonarQube at ${sonar_wait_url} ..."
    for ((attempt = 1; attempt <= max_attempts; attempt++)); do
      status_payload="$(curl -fsS "${sonar_wait_url%/}/api/system/status" 2>/dev/null || true)"
      status_line="$(
        printf '%s' "${status_payload}" \
          | tr -d '\n' \
          | sed -n 's/.*"status"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
      )"

      if [[ "${status_line}" == "UP" ]]; then
        echo "[lint] SonarQube is UP."
        break
      fi

      if [[ -z "${status_line}" ]]; then
        status_value="unreachable"
      else
        status_value="${status_line}"
      fi

      if (( attempt == max_attempts )); then
        echo "[lint] SonarQube is not ready yet (status=${status_value})." >&2
        return 1
      fi

      if (( attempt % 6 == 0 )); then
        echo "[lint] SonarQube status=${status_value} (attempt ${attempt}/${max_attempts})..."
      fi
      sleep "${sleep_seconds}"
    done
  else
    echo "[lint] SONAR_LOCAL=0 -> using external SonarQube at ${sonar_host_url}."
    scanner_run_args+=(--no-deps)
  fi

  if [[ "${sonar_local}" == "1" ]]; then
    run_sonar_required_rule_sync "${sonar_host_url}"
  else
    echo "[lint] External SonarQube selected; ensure python:S104, javascript:S104, and typescript:S104 are active in the server Quality Profiles."
  fi

  echo
  echo "==> SonarQube scan (${sonar_profile})"
  export SONAR_HOST_URL="${sonar_host_url}"
  export SONAR_TOKEN
  if [[ -n "${scanner_java_opts}" ]]; then
    export SONAR_SCANNER_JAVA_OPTS="${scanner_java_opts}"
  fi
  if (( ${#sonar_args[@]} > 0 )); then
    fs_compose --profile quality run "${scanner_run_args[@]}" \
      sonar-scanner sonar-scanner \
        -Dproject.settings="${sonar_settings_file_container}" \
        -Dsonar.projectKey="${sonar_project_key}" \
        -Dsonar.projectName="${sonar_project_name}" \
        -Dsonar.qualitygate.wait="${quality_gate_wait}" \
        -Dsonar.qualitygate.timeout="${quality_gate_timeout}" \
        "${sonar_args[@]}"
  else
    fs_compose --profile quality run "${scanner_run_args[@]}" \
      sonar-scanner sonar-scanner \
        -Dproject.settings="${sonar_settings_file_container}" \
        -Dsonar.projectKey="${sonar_project_key}" \
        -Dsonar.projectName="${sonar_project_name}" \
        -Dsonar.qualitygate.wait="${quality_gate_wait}" \
        -Dsonar.qualitygate.timeout="${quality_gate_timeout}"
  fi

  if [[ "${SONAR_EXPORT:-0}" == "1" ]]; then
    run_sonar_issue_export
    if [[ "${SONAR_BACKLOG:-0}" == "1" ]]; then
      run_sonar_backlog_generate
    fi
  fi
}

if [[ "${MODE}" == "sonar" ]]; then
  run_host_sonar_scan "stable"
  echo
  echo "[lint] Quality gate passed (sonar)."
  exit 0
fi

if [[ "${MODE}" == "sonar-js" ]]; then
  run_host_sonar_scan "js"
  echo
  echo "[lint] Quality gate passed (sonar-js)."
  exit 0
fi

if [[ "${MODE}" == "sonar-py" ]]; then
  run_host_sonar_scan "py"
  echo
  echo "[lint] Quality gate passed (sonar-py)."
  exit 0
fi

if [[ "${MODE}" == "sonar-full" ]]; then
  run_host_sonar_scan "full"
  echo
  echo "[lint] Quality gate passed (sonar-full)."
  exit 0
fi

if [[ "${MODE}" == "sonar-issues" ]]; then
  run_sonar_issue_export
  if [[ "${SONAR_BACKLOG:-0}" == "1" ]]; then
    run_sonar_backlog_generate
  fi
  echo
  echo "[lint] Sonar issues export completed."
  exit 0
fi

if [[ "${MODE}" == "sonar-backlog" ]]; then
  run_sonar_issue_export
  run_sonar_backlog_generate
  echo
  echo "[lint] Sonar issues export and backlog generation completed."
  exit 0
fi

# Docker Desktop serves stale or truncated files through a bind mount of a path
# under the user's home once a mounted file changes, so an extension checkout is
# never mounted: the host reads it and streams it into fs-audit as a tar archive.
# The container works on its own copy and cannot write back to the checkout.
extension_source_archive() {
  COPYFILE_DISABLE=1 tar -C "${extension_root}" --no-xattrs \
    --exclude=.git --exclude=.venv --exclude=.fs-python --exclude=__pycache__ \
    --exclude=.mypy_cache --exclude=.pytest_cache --exclude=.ruff_cache --exclude=node_modules \
    -cf - .
}

# Auto-detect running inside a Docker container (/.dockerenv marker or cgroup evidence).
_in_container=0
if [[ -f "/.dockerenv" ]] || grep -qE '(docker|containerd|kubepods)' /proc/1/cgroup 2>/dev/null; then
  _in_container=1
fi

if [[ "${permission_preflight}" -eq 1 ]]; then
  if [[ "${INSIDE}" -ne 1 || "${_in_container}" -ne 1 ]]; then
    echo "[lint] --permission-preflight is an internal container-only option." >&2
    exit 2
  fi
  "${SCRIPT_DIR}/quality_permissions.sh" repair --repo-root "${ROOT_DIR}"
  "${SCRIPT_DIR}/quality_permissions.sh" check --repo-root "${ROOT_DIR}"
fi

if [[ "${INSIDE}" -eq 0 && "${_in_container}" -eq 0 ]]; then
  "${SCRIPT_DIR}/quality_permissions.sh" repair --repo-root "${ROOT_DIR}"
  "${SCRIPT_DIR}/quality_permissions.sh" check --repo-root "${ROOT_DIR}"

  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    candidate_commit_sha="${commit_sha}"
    if [[ ${#REQUESTED_CHECKS[@]} -eq 0 && "${MODE}" == "full" && -z "${candidate_commit_sha}" ]]; then
      candidate_commit_sha="$(git -C "${ROOT_DIR}" rev-parse HEAD)"
    fi
    local_inside_args=(--inside --permission-preflight)
    if [[ "${extension_checks_requested}" -eq 1 ]]; then
      # The evidence names what was checked: the checkout's commit and whether the
      # copied tree carried uncommitted changes on top of it.
      extension_head="$(git -C "${extension_root}" rev-parse HEAD 2>/dev/null || echo "not a git checkout")"
      extension_dirty="$(git -C "${extension_root}" status --porcelain 2>/dev/null | grep -c . || true)"
      echo "[lint] Extension checkout: ${extension_root} at ${extension_head} (${extension_dirty} uncommitted paths)"
    fi
    if [[ ${#REQUESTED_CHECKS[@]} -gt 0 ]]; then
      echo "[lint] Running selected checks inside on-demand fs-audit container..."
      local_inside_args+=(--checks "$(IFS=,; echo "${REQUESTED_CHECKS[*]}")")
      # AC17-R89: --quality-owner-tier is the flag that suppresses `-x`, and it is only
      # legal together with --checks. It used to be parsed and validated on the host and
      # then dropped here, so the canonical backend-tests command from
      # infra/tooling/quality/check-registry.json always ran fail-fast, against the MUST
      # in docs/standards/testing-standard.md that the backend unit suite keep its
      # complete failure list. A flag that is accepted and silently not delivered is
      # worse than one that does not exist: it manufactures confidence.
      if [[ -n "${quality_owner_tier}" ]]; then
        local_inside_args+=(--quality-owner-tier "${quality_owner_tier}")
      fi
      # AC17-R97 obeys AC17-R89: a new flag that is accepted on the host and not
      # delivered to the container is worse than no flag, so it is forwarded here
      # and the delivery guard in tests/unit/test_lint_script.py covers it.
      if [[ -n "${TEST_PATHS}" ]]; then
        local_inside_args+=(--test-paths "${TEST_PATHS}")
      fi
      if [[ -n "${extension_id}" ]]; then
        local_inside_args+=(--extension-id "${extension_id}")
      fi
      if [[ "${extension_checks_requested}" -eq 1 ]]; then
        local_inside_args+=(--extension-from-stdin)
      fi
    else
      echo "[lint] Running ${MODE} gate inside on-demand fs-audit container..."
      local_inside_args+=("--${MODE}")
    fi
    # AC17-R89: these decide evidence reuse and identity and were forwarded only for a
    # whole-mode run. For a --checks run the container fell back to
    # EVIDENCE_MODE=reuse, so `--checks X --evidence-mode off` could not be expressed
    # from the host at all -- a false-green-by-cache risk, not just an inconvenience.
    local_inside_args+=(--evidence-mode "${EVIDENCE_MODE}")
    local_inside_args+=(--fingerprint-source "${FINGERPRINT_SOURCE}")
    local_inside_args+=(--trust "${evidence_trust}")
    if [[ -n "${candidate_commit_sha}" ]]; then
      local_inside_args+=(--commit-sha "${candidate_commit_sha}")
    fi

    lint_container_env_args=()
    # AC17-R97: a check that needs a host resource declares it in the registry as
    # `runtime_inputs`. The gate forwards every declared input that is actually set,
    # so "this check needs a real Traefik binary" is a prerequisite the container
    # receives rather than a reason to run the check outside it. Keyed on the union
    # rather than the requested id on purpose: a registry entry may wrap its work in
    # another check (compact-p1-routing dispatches targeted-tests), and the resource
    # belongs to the entry, not to the wrapper. Unset inputs are not forwarded, so a
    # check's own guard still decides whether to skip or fail.
    declared_runtime_inputs="$(
      "${LINT_HOST_PYTHON:-python3}" -c 'import json,sys
try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(0)
rows = payload if isinstance(payload, list) else payload.get("checks", [])
seen = []
for row in rows:
    for name in row.get("runtime_inputs") or ():
        if name not in seen:
            seen.append(name)
print("\n".join(seen))' "${ROOT_DIR}/infra/tooling/quality/check-registry.json" 2>/dev/null || true
    )"
    while IFS= read -r runtime_input; do
      [[ -n "${runtime_input}" && -n "${!runtime_input:-}" ]] || continue
      lint_container_env_args+=(-e "${runtime_input}=${!runtime_input}")
    done <<< "${declared_runtime_inputs}"
    if [[ -n "${FS_ZAP_TARGET_URL:-}" ]]; then
      lint_container_env_args+=(-e "FS_ZAP_TARGET_URL=${FS_ZAP_TARGET_URL}")
    fi
    if [[ -n "${FS_ZAP_MAX_MINUTES:-}" ]]; then
      lint_container_env_args+=(-e "FS_ZAP_MAX_MINUTES=${FS_ZAP_MAX_MINUTES}")
    fi
    if [[ -n "${FS_ZAP_CONFIG_FILE:-}" ]]; then
      lint_container_env_args+=(-e "FS_ZAP_CONFIG_FILE=${FS_ZAP_CONFIG_FILE}")
    fi
    if [[ "${evidence_trust}" == "trusted_ci" ]]; then
      lint_container_env_args+=(-e "GITHUB_ACTIONS=${GITHUB_ACTIONS:-}")
      lint_container_env_args+=(-e "GITHUB_SHA=${GITHUB_SHA:-}")
    fi
    lint_container_env_args+=(-e "FS_HOST_ROOT_DIR=${ROOT_DIR}")
    lint_container_volume_args=()
    git_common_dir="$(git -C "${ROOT_DIR}" rev-parse --path-format=absolute --git-common-dir)"
    if [[ -d "${git_common_dir}" ]]; then
      lint_container_volume_args+=(
        -v "${git_common_dir}:${git_common_dir}:ro"
      )
    fi

    ensure_audit_image_current || exit $?
    resolve_audit_test_image || exit $?
    lint_container_env_args+=(-e "FS_TEST_AUDIT_IMAGE_ID=${FS_TEST_AUDIT_IMAGE_ID}")

    # The shared quality cache volume is created root-owned unless the image that
    # first mounts it already owns the mount point, and the gate image is not
    # rebuilt on every run. Hand it to the container user once per invocation;
    # if this fails the gate still runs, just without the cache.
    fs_compose run --rm --no-deps -T --user 0:0 --entrypoint chown fs-audit \
      fs:fs /home/fs/.cache/flow-steward-quality >/dev/null 2>&1 || true

    boundary_manifest_host="${ROOT_DIR}/logs/quality/tracked-files.txt"
    boundary_manifest_container="/app/logs/quality/tracked-files.txt"
    mkdir -p "$(dirname -- "${boundary_manifest_host}")"
    git -C "${ROOT_DIR}" ls-files > "${boundary_manifest_host}"
    lint_container_env_args+=(
      -e "FS_BOUNDARY_TRACKED_FILES_MANIFEST=${boundary_manifest_container}"
    )

    needs_test_infra=0
    if [[ ${#REQUESTED_CHECKS[@]} -eq 0 && "${MODE}" == "full" ]]; then
      needs_test_infra=1
    fi
    if [[ ${#REQUESTED_CHECKS[@]} -gt 0 ]]; then
      for requested_check in "${REQUESTED_CHECKS[@]}"; do
        case "${requested_check}" in
          tests|pytest|targeted-tests|runtime-heartbeat-cross-profile|security-tests|security-regressions)
            needs_test_infra=1
            ;;
        esac
      done
    fi

    audit_infra_started=0
    cleanup_audit_test_infra() {
      if [[ "${audit_infra_started}" -eq 1 ]]; then
        fs_compose rm -s -f pg-audit rabbitmq-audit >/dev/null 2>&1 || true
        audit_infra_started=0
      fi
    }
    assert_audit_test_infra_removed() {
      local remaining=""
      remaining="$(fs_compose ps -q pg-audit rabbitmq-audit 2>/dev/null || true)"
      if [[ -n "${remaining}" ]]; then
        echo "[lint] Audit infrastructure cleanup failed; disposable services remain." >&2
        return 2
      fi
    }
    trap cleanup_audit_test_infra EXIT INT TERM

    # A final full gate is cumulative even though Tier 3 owns only its delta.
    # Refresh/reuse commit-bound Tier 2 before starting the long-lived test
    # infrastructure, so a vulnerable dependency or stale security result fails
    # before the frontend/backend suites consume tens of minutes.
    audit_exit_code=0
    if [[ ${#REQUESTED_CHECKS[@]} -eq 0 && "${MODE}" == "full" ]]; then
      final_tier_two_args=(--inside --permission-preflight --fast)
      final_tier_two_args+=(--evidence-mode "${EVIDENCE_MODE}")
      final_tier_two_args+=(--fingerprint-source commit)
      final_tier_two_args+=(--trust "${evidence_trust}")
      final_tier_two_args+=(--commit-sha "${candidate_commit_sha}")
      fs_compose run --no-deps --rm -T "${lint_container_env_args[@]}" "${lint_container_volume_args[@]}" fs-audit \
        bash /app/core/entrypoints/cli/scripts/lint.sh "${final_tier_two_args[@]}" \
        || audit_exit_code=$?
    fi

    if [[ "${audit_exit_code}" -eq 0 && "${needs_test_infra}" -eq 1 ]]; then
      audit_infra_started=1
      fs_compose up -d --wait pg-audit rabbitmq-audit
      fs_compose run --no-deps --rm -T fs-audit \
        python -m core.application.tooling.db.init_db
    fi

    # Capture exit code via || to avoid the `if cmd; then fi` bash trap where
    # `$?` after `fi` without an else-clause is always 0 even if cmd failed.
    if [[ "${audit_exit_code}" -eq 0 && "${extension_checks_requested}" -eq 1 ]]; then
      extension_source_archive \
        | fs_compose run --no-deps --rm -T "${lint_container_env_args[@]}" "${lint_container_volume_args[@]}" fs-audit bash /app/core/entrypoints/cli/scripts/lint.sh "${local_inside_args[@]}" \
        || audit_exit_code=$?
    elif [[ "${audit_exit_code}" -eq 0 ]]; then
      fs_compose run --no-deps --rm -T "${lint_container_env_args[@]}" "${lint_container_volume_args[@]}" fs-audit bash /app/core/entrypoints/cli/scripts/lint.sh "${local_inside_args[@]}" \
        || audit_exit_code=$?
    fi
    cleanup_audit_test_infra
    if [[ "${needs_test_infra}" -eq 1 ]] && ! assert_audit_test_infra_removed; then
      audit_exit_code=2
    fi
    if [[ "${audit_exit_code}" -eq 0 && ${#REQUESTED_CHECKS[@]} -eq 0 && "${MODE}" == "full" ]]; then
      external_verify_args=(
        --evidence-mode "${EVIDENCE_MODE}"
        --fingerprint-source commit
        --trust "${evidence_trust}"
        --commit-sha "${candidate_commit_sha}"
      )
      "${SCRIPT_DIR}/run-external-tier-three.sh" "${external_verify_args[@]}" \
        || audit_exit_code=$?
    fi
    if [[ "${audit_exit_code}" -eq 0 && ${#REQUESTED_CHECKS[@]} -eq 0 && "${MODE}" == "full" ]]; then
      fs_compose run --no-deps --rm -T "${lint_container_volume_args[@]}" fs-audit \
        python -m core.application.tooling.quality.release_gate \
        || audit_exit_code=$?
    fi

    if [[ "${audit_exit_code}" -eq 0 ]]; then
      if [[ "${WITH_SONAR}" -eq 1 || "${SONAR_ENABLED:-0}" == "1" ]]; then
        run_host_sonar_scan "${SONAR_PROFILE:-stable}"
      fi
      exit 0
    fi

    # Container fallback is opt-in only. By default, preserve the original fs-audit
    # failure so local runs match CI and don't get masked by dependency drift in fs-app.
    if [[ "${FS_LINT_CONTAINER_FALLBACK:-0}" == "1" && "${extension_checks_requested}" -eq 0 && ( ${#REQUESTED_CHECKS[@]} -gt 0 || "${MODE}" != "full" ) ]]; then
      echo "[lint] fs-audit exited ${audit_exit_code}; retrying in fs-app because FS_LINT_CONTAINER_FALLBACK=1."
      fallback_exit_code=0
      fs_compose exec -T fs-app bash /app/core/entrypoints/cli/scripts/lint.sh "${local_inside_args[@]}" \
        || fallback_exit_code=$?
      if [[ "${fallback_exit_code}" -eq 0 ]]; then
        if [[ "${WITH_SONAR}" -eq 1 || "${SONAR_ENABLED:-0}" == "1" ]]; then
          run_host_sonar_scan "${SONAR_PROFILE:-stable}"
        fi
        exit 0
      fi
    fi

    exit "${audit_exit_code}"
  fi
fi

run_step() {
  local title="$1"
  shift
  echo
  echo "==> ${title}"
  "$@"
}

python_module_available() {
  "${PYTHON_BIN}" -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)" "$1"
}

coverage_source_for_target() {
  local target="$1"
  if [[ "${target}" == *.py ]]; then
    return 0
  fi
  printf '%s\n' "${target}"
}

PYTHON_BIN="$(resolve_python_bin)" || exit 1

SEMGREP_CMD=("${PYTHON_BIN}" "-m" "semgrep")
if command -v semgrep >/dev/null 2>&1; then
  SEMGREP_CMD=("semgrep")
fi
SEMGREP_TIMEOUT="${FS_SEMGREP_TIMEOUT:-30}"
SEMGREP_TIMEOUT_THRESHOLD="${FS_SEMGREP_TIMEOUT_THRESHOLD:-25}"
SEMGREP_JOBS="${FS_SEMGREP_JOBS:-4}"

TARGETS=()
for candidate in "${TARGET_CANDIDATES[@]}"; do
  if [[ -e "${candidate}" ]]; then
    TARGETS+=("${candidate}")
  fi
done
if [[ ${#TARGETS[@]} -eq 0 ]]; then
  TARGETS=(".")
fi

COMPLEXITY_TARGETS=()
for complexity_candidate in \
  "core/application/orchestration/agents/prompt_agent.py" \
  "core/application/jobs/task_enqueue_runtime.py" \
  "core/application/workflows/workflow_entrypoint_execution_service.py" \
  "core/entrypoints/api/jobs_control.py" \
  "core/entrypoints/api/workflow_entrypoints_routes_execute.py" \
  "tests/api_runtime_test_surface.py" \
  "core/application/management/command_services.py" \
  "core/application/management/query_services.py" \
  "core/application/registry/db_store.py" \
  "core/infrastructure/runtime/worker/worker.py"; do
  if [[ -e "${complexity_candidate}" ]]; then
    COMPLEXITY_TARGETS+=("${complexity_candidate}")
  fi
done
if [[ ${#COMPLEXITY_TARGETS[@]} -eq 0 ]]; then
  COMPLEXITY_TARGETS=("${TARGETS[@]}")
fi

run_detect_secrets() {
  local baseline_file=".secrets.baseline"
  local tracked_manifest="${FS_BOUNDARY_TRACKED_FILES_MANIFEST:-}"
  local tracked_files=""
  local file_path=""
  local scan_rc=0
  local secret_scan_files=()
  local temporary_baseline=""
  local temporary_output=""
  local scan_list=""
  local pending_file=""
  local pending_list=""
  local pending_files=()
  local secret_cache_args=()

  if [[ ! -s "${baseline_file}" ]]; then
    echo "[lint] Missing non-empty .secrets.baseline for repository-wide secret gate." >&2
    return 2
  fi
  if ! tracked_files="$(git ls-files 2>/dev/null)"; then
    if [[ -z "${tracked_manifest}" || ! -s "${tracked_manifest}" ]]; then
      echo "[lint] Cannot enumerate tracked files for secret gate." >&2
      return 2
    fi
    tracked_files="$(cat -- "${tracked_manifest}")"
  fi

  while IFS= read -r file_path; do
    case "${file_path}" in
      extensions/*|*/node_modules/*|*/dist/*|*/vendor/*|*/.ruff_cache/*|*/.pytest_cache/*|logs/*|.secrets.baseline)
        continue
        ;;
    esac
    [[ -f "${file_path}" ]] && secret_scan_files+=("${file_path}")
  done <<< "${tracked_files}"

  if [[ ${#secret_scan_files[@]} -eq 0 ]]; then
    echo "[lint] No tracked canonical files were found for secret gate." >&2
    return 2
  fi

  # detect-secrets judges each file against that file's baseline entries only, so
  # a file whose content already passed under the same tool version, baseline and
  # exclusions (this script) passes again. Only the rest is scanned.
  scan_list="$(mktemp "${TMPDIR:-/tmp}/flow-steward-secrets-files.XXXXXX")"
  printf '%s\n' "${secret_scan_files[@]}" > "${scan_list}"
  secret_cache_args=(
    --tool detect-secrets
    --key "$(detect-secrets --version 2>/dev/null || echo unknown)"
    --key-file "${baseline_file}"
    --key-file "${SCRIPT_DIR}/lint.sh"
  )
  pending_list="$(mktemp "${TMPDIR:-/tmp}/flow-steward-secrets-pending.XXXXXX")"
  if [[ "${EVIDENCE_MODE}" == off ]]; then
    # Diagnostic mode reuses nothing, including earlier per-file passes.
    pending_files=("${secret_scan_files[@]}")
  elif "${PYTHON_BIN}" -m core.application.tooling.quality.file_scan_cache pending \
    "${secret_cache_args[@]}" --files-from "${scan_list}" > "${pending_list}"; then
    while IFS= read -r pending_file; do
      [[ -n "${pending_file}" ]] && pending_files+=("${pending_file}")
    done < "${pending_list}"
  else
    # A cache that cannot answer must never shrink the scan.
    echo "[lint] detect-secrets: file cache unavailable; scanning every file." >&2
    pending_files=("${secret_scan_files[@]}")
  fi
  rm -f -- "${pending_list}"
  if [[ ${#pending_files[@]} -eq 0 ]]; then
    rm -f -- "${scan_list}"
    echo "[lint] detect-secrets: all ${#secret_scan_files[@]} files already passed with this content; nothing to rescan."
    return 0
  fi
  echo "[lint] detect-secrets: scanning ${#pending_files[@]} of ${#secret_scan_files[@]} files; the rest already passed with this content."
  secret_scan_files=("${pending_files[@]}")
  printf '%s\n' "${secret_scan_files[@]}" > "${scan_list}"

  temporary_baseline="$(mktemp "${TMPDIR:-/tmp}/flow-steward-secrets-baseline.XXXXXX")"
  temporary_output="$(mktemp "${TMPDIR:-/tmp}/flow-steward-secrets-output.XXXXXX")"
  cp -- "${baseline_file}" "${temporary_baseline}"

  run_detect_secrets_hook() {
    detect-secrets-hook \
      --json \
      --baseline "${temporary_baseline}" \
      --exclude-files '(^|/)(node_modules|dist|vendor|\.ruff_cache|\.pytest_cache|logs)(/|$)|^\.secrets\.baseline$' \
      "${secret_scan_files[@]}"
  }

  is_detect_secrets_metadata_only() {
    local non_empty_lines=""
    [[ "${scan_rc}" -eq 3 ]] || return 1
    non_empty_lines="$(grep -cve '^[[:space:]]*$' "${temporary_output}")"
    [[ "${non_empty_lines}" -eq 3 ]] || return 1
    grep -Fxq 'The baseline file was updated.' "${temporary_output}" || return 1
    grep -Fxq 'Probably to keep line numbers of secrets up-to-date.' \
      "${temporary_output}" || return 1
    grep -Eq '^Please `git add .+`, thank you\.$' "${temporary_output}" || return 1
    python - "${baseline_file}" "${temporary_baseline}" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path


def normalized(path: str) -> tuple[object, Counter[tuple[object, ...]]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    signatures: Counter[tuple[object, ...]] = Counter()
    if isinstance(payload, dict):
        payload.pop("generated_at", None)
        filters = payload.get("filters_used")
        if isinstance(filters, list):
            payload["filters_used"] = [
                item
                for item in filters
                if not (
                    isinstance(item, dict)
                    and item.get("path") == "detect_secrets.filters.common.is_baseline_file"
                )
            ]
        results = payload.pop("results", None)
        if isinstance(results, dict):
            for findings in results.values():
                if isinstance(findings, list):
                    for finding in findings:
                        if isinstance(finding, dict):
                            signatures[
                                (
                                    finding.get("type"),
                                    finding.get("hashed_secret"),
                                    finding.get("is_verified"),
                                    finding.get("is_secret"),
                                )
                            ] += 1
    return payload, signatures


before_config, before_findings = normalized(sys.argv[1])
after_config, after_findings = normalized(sys.argv[2])
is_safe_refresh = before_config == after_config and all(
    count <= before_findings[signature] for signature, count in after_findings.items()
)
raise SystemExit(0 if is_safe_refresh else 1)
PY
  }

  if run_detect_secrets_hook >"${temporary_output}" 2>&1; then
    scan_rc=0
  else
    scan_rc=$?
  fi
  cat -- "${temporary_output}"
  if is_detect_secrets_metadata_only; then
    # Exit 3 plus the exact metadata-only contract proves the completed scan
    # found no new secret. Avoid repeating the same repository-wide scan.
    echo "[lint] detect-secrets: temporary baseline metadata refreshed; no rescan required."
    scan_rc=0
  fi
  rm -f -- "${temporary_baseline}" "${temporary_output}"
  if [[ "${scan_rc}" -ne 0 ]]; then
    rm -f -- "${scan_list}"
    return "${scan_rc}"
  fi
  if [[ "${EVIDENCE_MODE}" != off ]]; then
    "${PYTHON_BIN}" -m core.application.tooling.quality.file_scan_cache record \
      "${secret_cache_args[@]}" --files-from "${scan_list}" || true
  fi
  rm -f -- "${scan_list}"
  echo "[lint] detect-secrets: no new potential secrets outside the reviewed baseline."
}

run_pip_audit() {
  local acceptance_file=""
  local requirements_runtime=""
  local requirements_audit=""
  local requirements_cli="requirements-cli.lock"
  local requirements_semgrep=""
  local runtime_audit_report=""
  local runtime_audit_error=""
  local cli_report=""
  local cli_error=""
  local semgrep_report=""
  local semgrep_error=""
  local audit_status=0
  local missing_files=()
  # The product images install exactly what these locks pin (infra/docker/Dockerfile);
  # the audits below resolve the unpinned requirements afresh and never see them.
  local product_locks=(requirements-runtime.lock requirements-compact.lock requirements-mcp.lock requirements-control.lock)
  local lock_file=""
  local lock_label=""
  local lock_reports_dir=""
  local lock_specs=()

  acceptance_file="${FS_PIP_AUDIT_ACCEPTANCE_FILE:-infra/tooling/quality/pip-audit-risk-acceptance.md}"
  requirements_runtime="${FS_PIP_AUDIT_REQUIREMENTS_RUNTIME:-requirements-runtime.txt}"
  requirements_audit="${FS_PIP_AUDIT_REQUIREMENTS_AUDIT:-requirements-audit.txt}"
  requirements_semgrep="${FS_PIP_AUDIT_REQUIREMENTS_SEMGREP:-requirements-semgrep.txt}"

  if [[ -f "${requirements_runtime}" ]]; then
    true
  else
    missing_files+=("${requirements_runtime}")
  fi

  if [[ -f "${requirements_audit}" ]]; then
    true
  else
    missing_files+=("${requirements_audit}")
  fi

  if [[ ! -f "${requirements_cli}" ]]; then
    missing_files+=("${requirements_cli}")
  fi

  if [[ ! -f "${requirements_semgrep}" ]]; then
    missing_files+=("${requirements_semgrep}")
  fi

  for lock_file in "${product_locks[@]}"; do
    if [[ ! -f "${lock_file}" ]]; then
      missing_files+=("${lock_file}")
    fi
  done

  if [[ ${#missing_files[@]} -gt 0 ]]; then
    echo "[lint] pip-audit: missing requirements file(s): ${missing_files[*]}" >&2
    return 1
  fi

  runtime_audit_report="$(mktemp)"
  runtime_audit_error="$(mktemp)"
  cli_report="$(mktemp)"
  cli_error="$(mktemp)"
  semgrep_report="$(mktemp)"
  semgrep_error="$(mktemp)"

  # Runtime and general audit tools share one compatible dependency graph.
  # Semgrep is installed in an isolated venv and must be resolved independently.
  if ! "${PYTHON_BIN}" -m pip_audit -f json --timeout 60 \
    -r "${requirements_runtime}" \
    -r "${requirements_audit}" \
    > "${runtime_audit_report}" 2> "${runtime_audit_error}"; then
    true
  fi
  if ! "${PYTHON_BIN}" -m pip_audit -f json --timeout 60 \
    -r "${requirements_cli}" \
    > "${cli_report}" 2> "${cli_error}"; then
    true
  fi
  if ! "${PYTHON_BIN}" -m pip_audit -f json --timeout 60 \
    -r "${requirements_semgrep}" \
    > "${semgrep_report}" 2> "${semgrep_error}"; then
    true
  fi

  if [[ ! -s "${runtime_audit_report}" ]]; then
    echo "[lint] pip-audit (runtime+audit): no JSON report produced." >&2
    sed -n '1,120p' "${runtime_audit_error}" >&2
    rm -f "${runtime_audit_report}" "${runtime_audit_error}" "${cli_report}" "${cli_error}" "${semgrep_report}" "${semgrep_error}"
    return 1
  fi
  if [[ ! -s "${cli_report}" ]]; then
    echo "[lint] pip-audit (cli): no JSON report produced." >&2
    sed -n '1,120p' "${cli_error}" >&2
    rm -f "${runtime_audit_report}" "${runtime_audit_error}" "${cli_report}" "${cli_error}" "${semgrep_report}" "${semgrep_error}"
    return 1
  fi
  if [[ ! -s "${semgrep_report}" ]]; then
    echo "[lint] pip-audit (semgrep): no JSON report produced." >&2
    sed -n '1,120p' "${semgrep_error}" >&2
    rm -f "${runtime_audit_report}" "${runtime_audit_error}" "${cli_report}" "${cli_error}" "${semgrep_report}" "${semgrep_error}"
    return 1
  fi

  # Audit each lock as pinned and hashed, without resolving or installing anything.
  lock_reports_dir="$(mktemp -d)"
  for lock_file in "${product_locks[@]}"; do
    lock_label="$(basename "${lock_file}" .lock)"
    lock_label="${lock_label#requirements-}-lock"
    if ! "${PYTHON_BIN}" -m pip_audit -f json --timeout 60 --require-hashes --disable-pip \
      -r "${lock_file}" \
      > "${lock_reports_dir}/${lock_label}.json" 2> "${lock_reports_dir}/${lock_label}.err"; then
      true
    fi
    if [[ ! -s "${lock_reports_dir}/${lock_label}.json" ]]; then
      echo "[lint] pip-audit (${lock_label}): no JSON report produced." >&2
      sed -n '1,120p' "${lock_reports_dir}/${lock_label}.err" >&2
      rm -rf "${lock_reports_dir}"
      rm -f "${runtime_audit_report}" "${runtime_audit_error}" "${cli_report}" "${cli_error}" "${semgrep_report}" "${semgrep_error}"
      return 1
    fi
    lock_specs+=("${lock_label}:${lock_reports_dir}/${lock_label}.json")
  done

  if "${PYTHON_BIN}" -m core.application.tooling.quality.pip_audit_acceptance \
    "${acceptance_file}" \
    "runtime+audit:${runtime_audit_report}" \
    "cli:${cli_report}" \
    "semgrep:${semgrep_report}" \
    "${lock_specs[@]}"
  then
    audit_status=0
  else
    audit_status=$?
  fi
  rm -rf "${lock_reports_dir}"
  rm -f "${runtime_audit_report}" "${runtime_audit_error}" "${cli_report}" "${cli_error}" "${semgrep_report}" "${semgrep_error}"
  return "${audit_status}"
}

run_security_regression_suite() {
  assert_audit_test_isolation
  local tests=()
  local test_file=""
  for test_file in "${SECURITY_TEST_CANDIDATES[@]}"; do
    if [[ -f "${test_file}" ]]; then
      tests+=("${test_file}")
    fi
  done

  if [[ ${#tests[@]} -eq 0 ]]; then
    echo
    echo "[lint] Security regression tests not found, skipping."
    return 0
  fi

  run_step \
    "Security regression tests" \
    "${PYTHON_BIN}" -m pytest -q -p no:randomly "${tests[@]}"
}

run_optional_zap_baseline() {
  local target_url="${FS_ZAP_TARGET_URL:-}"
  local zap_config_file="${FS_ZAP_CONFIG_FILE:-infra/tooling/quality/zap-baseline.conf}"
  local zap_workdir="${FS_HOST_ROOT_DIR:-${ROOT_DIR}}"
  local zap_config_args=()
  if [[ -z "${target_url}" ]]; then
    echo
    echo "[lint] FS_ZAP_TARGET_URL not set, skipping OWASP ZAP baseline."
    return 0
  fi

  if ! command -v docker >/dev/null 2>&1; then
    echo "[lint] Docker is required for OWASP ZAP baseline scan." >&2
    return 1
  fi

  mkdir -p logs/quality/security
  if [[ -f "${zap_config_file}" ]]; then
    zap_config_args=(-c "${zap_config_file}")
  fi
  run_step \
    "OWASP ZAP baseline scan (${target_url})" \
    docker run --rm -t \
      -v "${zap_workdir}:/zap/wrk:rw" \
      ghcr.io/zaproxy/zaproxy:stable \
      zap-baseline.py \
      -t "${target_url}" \
      "${zap_config_args[@]}" \
      -m "${FS_ZAP_MAX_MINUTES:-5}" \
      -r "logs/quality/security/zap_baseline_report."html \
      -J "logs/quality/security/zap_baseline_report.json"
}

run_check_ruff_lint() {
  if ! python_module_available "ruff"; then
    echo
    echo "[lint] Ruff is not installed for ${PYTHON_BIN}; skipping Ruff lint."
    return 0
  fi
  run_step "Ruff lint" "${PYTHON_BIN}" -m ruff check "${TARGETS[@]}"
}

run_check_ruff_format() {
  if ! python_module_available "ruff"; then
    echo
    echo "[lint] Ruff is not installed for ${PYTHON_BIN}; skipping Ruff format check."
    return 0
  fi
  run_step "Ruff format check" "${PYTHON_BIN}" -m ruff format --check "${TARGETS[@]}"
}

run_check_mypy() {
  # In the audit container mypy keeps its incremental cache in the shared volume:
  # a fresh worktree otherwise rechecks the whole tree cold. mypy revalidates every
  # entry by source hash when the mtime differs and replaces cache files
  # atomically, so checkouts can share it. Each invocation has its own options
  # and therefore its own directory.
  local main_cache_args=()
  local strict_cache_args=()
  if [[ -d "${quality_cache_dir}" && -w "${quality_cache_dir}" ]]; then
    main_cache_args=(--cache-dir "${quality_cache_dir}/mypy/main")
    strict_cache_args=(--cache-dir "${quality_cache_dir}/mypy/extension-strict")
  fi
  run_step "Mypy type check" "${PYTHON_BIN}" -m mypy "${main_cache_args[@]}" "${TARGETS[@]}"
  run_step \
    "Mypy strict extension graph" \
    "${PYTHON_BIN}" -m mypy \
    "${strict_cache_args[@]}" \
    --disallow-untyped-defs \
    --follow-imports=skip \
    "${EXTENSION_STRICT_TARGETS[@]}"
}

run_check_bandit() {
  local baseline_args=()
  local baseline_file="${FS_BANDIT_BASELINE_FILE:-infra/tooling/quality/bandit-baseline-medium-high.json}"
  if [[ -f "${baseline_file}" ]]; then
    baseline_args=(-b "${baseline_file}")
  fi
  run_step "Bandit security lint (MEDIUM/HIGH)" "${PYTHON_BIN}" -m bandit -c pyproject.toml -r "${TARGETS[@]}" -ll "${baseline_args[@]}"
}

run_check_pip_audit() {
  run_step "Dependency vulnerability audit (pip-audit)" run_pip_audit
}

run_check_semgrep() {
  local semgrep_pythonwarnings="${FS_SEMGREP_PYTHONWARNINGS:-ignore::SyntaxWarning:glom.matching}"
  run_step \
    "Semgrep security scan (OWASP + Python)" \
    "${SCRIPT_DIR}/semgrep_retry.sh" \
    env "PYTHONWARNINGS=${semgrep_pythonwarnings}" "SEMGREP_SEND_METRICS=off" \
    "${SEMGREP_CMD[@]}" \
    --metrics=off \
    --disable-version-check \
    --config p/python \
    --config p/owasp-top-ten \
    --error \
    --jobs "${SEMGREP_JOBS}" \
    --include='*.py' \
    --timeout "${SEMGREP_TIMEOUT}" \
    --timeout-threshold "${SEMGREP_TIMEOUT_THRESHOLD}" \
    "${TARGETS[@]}"
}

run_check_detect_secrets() {
  run_step "Secret scan (detect-secrets)" run_detect_secrets
}

run_check_ci_excludes() {
  # R-12: tests/CI_EXCLUDES.txt must stay well-formed (OWNER + TICKET +
  # REMOVE_BY per ROOT_CAUSE group, no expired entries).
  run_step \
    "CI excludes lint (R-12 contract)" \
    "${PYTHON_BIN}" -m core.application.tooling.quality.check_ci_excludes
}

run_check_ui_i18n() {
  run_step \
    "Used UI localization keys (supported locales and POT)" \
    "${PYTHON_BIN}" -m core.application.tooling.i18n.ui_i18n_catalog validate-keys
}

run_check_sonar_size() {
  # Predicts Sonar's S104 file-size findings in about a second, so an over-long
  # file is reported here rather than after a 40-minute scan.
  run_step \
    "Sonar file-size precheck (S104)" \
    "${PYTHON_BIN}" -m core.application.tooling.quality.sonar_size_precheck --root "${ROOT_DIR}"
}

run_check_provider_neutral_core() {
  run_step \
    "Provider-neutral OAuth architecture check" \
    "${PYTHON_BIN}" "${SCRIPT_DIR}/check-provider-neutral-core.py" --root "${ROOT_DIR}"
}

run_check_extension_oauth_browser() {
  run_step \
    "Extension OAuth real-stack browser acceptance" \
    "${SCRIPT_DIR}/run-extension-oauth-browser-acceptance.sh"
}

run_check_env_artifacts() {
  run_step \
    "Environment artifact check" \
    "${PYTHON_BIN}" -m core.application.tooling.ops.sync_env_artifacts check \
    --repo-root "${ROOT_DIR}"
}

run_check_repository_boundary() {
  # AC17-R97: the boundary guard already runs unconditionally at the top of every
  # lint.sh invocation. The registry named it as a check of its own, which was not
  # dispatchable -- so the canonical command for it could not be executed. Giving it
  # a case makes the registry entry real without changing when the guard runs.
  run_step \
    "Repository boundary" \
    bash ./core/entrypoints/cli/scripts/check_no_enterprise_leak.sh
}

run_check_release_contracts() {
  run_step \
    "Release contracts" \
    "${PYTHON_BIN}" -m core.entrypoints.cli quality release-gate --confirm
}

run_check_candidate_smoke() {
  run_step \
    "Candidate smoke" \
    bash ./core/entrypoints/cli/scripts/smoke/release-smoke.sh --profile release
}

# The extension checkout arrives on stdin (see extension_source_archive) and is
# unpacked once per run; extension_gate stages it under its canonical folder name.
stage_extension_source() {
  if [[ -n "${extension_source_dir}" ]]; then
    return 0
  fi
  extension_source_dir="$(mktemp -d "${TMPDIR:-/tmp}/fs-extension-source.XXXXXX")"
  if [[ "${extension_from_stdin}" -eq 1 ]]; then
    tar -x -f - -C "${extension_source_dir}" --no-same-owner --warning=no-unknown-keyword
  elif [[ -n "${extension_root}" && -f "${extension_root}/extension.yaml" ]]; then
    cp -R "${extension_root}/." "${extension_source_dir}/"
  else
    echo "[lint] extension checks received no extension checkout; pass --extension-root." >&2
    return 2
  fi
  if [[ ! -f "${extension_source_dir}/extension.yaml" ]]; then
    echo "[lint] the extension checkout has no extension.yaml." >&2
    return 2
  fi
}

extension_gate_args() {
  extension_gate_cli_args=(--source "${extension_source_dir}")
  if [[ -n "${extension_id}" ]]; then
    extension_gate_cli_args+=(--extension-id "${extension_id}")
  fi
}

run_check_extension_static() {
  stage_extension_source || return $?
  extension_gate_args
  run_step \
    "Extension static checks (validator, compileall, ruff, mypy, bandit; no pip-audit)" \
    "${PYTHON_BIN}" -m core.application.tooling.extensions.extension_gate static "${extension_gate_cli_args[@]}"
}

run_check_extension_tests() {
  # Tier 1 shape: selected targets run fail-fast (-x); without a selection the
  # whole tests/test_*.py suite runs to the end.
  stage_extension_source || return $?
  extension_gate_args
  local requested=()
  local token=""
  IFS=' ,' read -r -a requested <<< "${TEST_PATHS:-}"
  for token in "${requested[@]}"; do
    if [[ -n "${token}" ]]; then
      extension_gate_cli_args+=(--test-path "${token}")
    fi
  done
  run_step \
    "Extension tests (validate + offline venv + pytest)" \
    env FS_EXTENSION_TEST_RUNTIME=1 \
    "${PYTHON_BIN}" -m core.application.tooling.extensions.extension_gate tests "${extension_gate_cli_args[@]}"
}

run_check_targeted_tests() {
  # AC17-R97: Tier 1 is a focused, fail-fast cycle, and its registry entries invoked
  # `python -m pytest` directly -- on whatever interpreter the caller happened to have.
  # That is why Tier 1 could not run on this host at all (python3 is 3.14, not the
  # pinned 3.12 runtime) and why a host run of the same files could
  # pass while the gate failed: the gate substitutes the tree via
  # run-core-only-tests.sh. Being focused does not require being outside the container.
  local paths_raw="${TEST_PATHS:-${FS_TEST_PATHS:-}}"
  if [[ -z "${paths_raw}" ]]; then
    echo "[lint] targeted-tests requires --test-paths with one or more pytest targets." >&2
    return 2
  fi
  local paths=()
  local requested=()
  local token=""
  local selected=""
  # shellcheck disable=SC2206
  IFS=' ,' read -r -a requested <<< "${paths_raw}"
  # `auto` (or `auto:<base-ref>`, default main) expands to the unit tests the
  # change reaches: changed tests, tests importing a changed module or one of its
  # direct importers, and tests that name a changed non-Python file.
  for token in "${requested[@]}"; do
    if [[ "${token}" == auto || "${token}" == auto:* ]]; then
      local base_ref="main"
      [[ "${token}" == auto:* ]] && base_ref="${token#auto:}"
      if ! selected="$("${PYTHON_BIN}" -m core.application.tooling.quality.impacted_tests --base "${base_ref}")"; then
        echo "[lint] targeted-tests could not derive impacted tests from ${base_ref}." >&2
        return 2
      fi
      if [[ -n "${selected}" ]]; then
        while IFS= read -r token; do paths+=("${token}"); done <<< "${selected}"
      fi
      echo "[lint] targeted-tests auto (${base_ref}): $(printf '%s\n' "${selected}" | grep -c . || true) impacted unit test files."
    else
      paths+=("${token}")
    fi
  done
  if [[ ${#paths[@]} -eq 0 ]]; then
    if [[ " ${requested[*]} " == *" auto"* ]]; then
      echo "[lint] targeted-tests: the change reaches no unit tests."
      return 0
    fi
    echo "[lint] targeted-tests received no usable pytest targets." >&2
    return 2
  fi
  assert_audit_test_isolation || return $?
  ensure_frontend_shell || return $?
  run_step \
    "Targeted tests" \
    ./core/entrypoints/cli/scripts/run-core-only-tests.sh \
    "${PYTHON_BIN}" -m pytest -q -x --no-cov "${paths[@]}"
}

run_check_unit_tests_with_coverage() {
  if [[ -d tests/unit ]]; then
    assert_audit_test_isolation
    ensure_frontend_shell || return $?
    # Write unit-only coverage to logs/coverage/unit/.
    # Mixing unit-only and project-wide artifacts into one file makes coverage
    # claims untrustworthy. See TESTING_GAP_ANALYSIS.md §Measurement Scope.
    local coverage_args=()
    local coverage_args_shell=""
    local coverage_source=""
    local pytest_fail_fast_arg=""
    # The whole unit suite runs on a fixed number of pytest-xdist workers: a fixed
    # count keeps runs comparable, and four keeps the peak memory of the workers
    # inside the shared Docker VM. Each worker clones the audit database for
    # itself (tests/_xdist_isolation.py); loadgroup keeps tests marked
    # xdist_group on one worker. Tier 1 targeted runs stay single-process.
    local unit_test_workers=4
    local pytest_parallel_args="-n ${unit_test_workers} --dist loadgroup "
    # Every worker must collect the same tests in the same order. A test that
    # parametrizes over a set orders by string hash, which is random per process,
    # so the workers share one seed: drawn per run, printed for a reproduction.
    local unit_test_hash_seed=""
    unit_test_hash_seed="$(od -An -N4 -tu4 /dev/urandom | tr -d ' ')"
    local target=""
    if [[ ${#REQUESTED_CHECKS[@]} -gt 0 && "${quality_owner_tier}" != "3" ]]; then
      pytest_fail_fast_arg="-x "
    fi
    for target in "${TARGETS[@]}"; do
      coverage_source="$(coverage_source_for_target "${target}")"
      if [[ -n "${coverage_source}" ]]; then
        coverage_args+=("--cov=${coverage_source}")
      fi
    done
    if [[ ${#coverage_args[@]} -eq 0 ]]; then
      coverage_args=("--cov=.")
    fi
    printf -v coverage_args_shell "%q " "${coverage_args[@]}"
    if [[ ! -d logs/coverage/unit ]]; then
      mkdir -p logs/coverage/unit
    fi
    echo "[lint] Unit test workers share hash seed ${unit_test_hash_seed}."
    # -u streams progress through tee: a run the kernel kills for memory still
    # shows how far it got instead of an empty block-buffered log.
    run_step \
      "Unit tests with coverage" \
      bash -lc "set -o pipefail; \
        PYTHONHASHSEED=${unit_test_hash_seed} ./core/entrypoints/cli/scripts/run-core-only-tests.sh \
          --coverage-export-dir "${ROOT_DIR}/logs/coverage/unit" \
          \"${PYTHON_BIN}\" -u -m pytest -q -ra ${pytest_parallel_args}${pytest_fail_fast_arg}tests/unit \
          ${coverage_args_shell}\
          --durations=30 --durations-min=1.0 \
          --cov-report=term-missing \
          --cov-report=xml:logs/coverage/unit/coverage.xml \
          --cov-fail-under=\"${FS_COV_FAIL_UNDER:-0}\" \
          | tee logs/coverage/unit/coverage.txt; \
        pytest_status=\${PIPESTATUS[0]}; \
        if [[ \"\${pytest_status}\" -ne 0 ]]; then exit \"\${pytest_status}\"; fi; \
        if grep -Eq '(^SKIPPED \\[|[0-9]+ skipped)' logs/coverage/unit/coverage.txt; then \
          if [[ "${FS_ALLOW_UNIT_TEST_SKIPS:-0}" == "1" ]]; then \
            echo '[lint] Unit test skips explicitly accepted for this environment.'; \
          else \
            echo '[lint] Unit tests reported skipped tests; skipped tests must be fixed or explicitly removed.' >&2; \
            exit 1; \
          fi; \
        fi"
  else
    echo
    echo "[lint] tests/unit not found, skipping pytest coverage step."
  fi
}

assert_audit_test_isolation() {
  if [[ "${FS_AUDIT_ISOLATED:-0}" != "1" ]]; then
    echo "[lint] Refusing tests: FS_AUDIT_ISOLATED=1 is required." >&2
    return 2
  fi
  case "${DATABASE_URL:-}" in
    *"@pg-audit:5432/flow_steward_audit"*) ;;
    *)
      echo "[lint] Refusing tests: DATABASE_URL is not the disposable pg-audit database." >&2
      return 2
      ;;
  esac
  case "${FS_DB_DSN:-}" in
    *"dbname=flow_steward_audit"*"host=pg-audit"*) ;;
    *)
      echo "[lint] Refusing tests: FS_DB_DSN is not the disposable pg-audit database." >&2
      return 2
      ;;
  esac
  case "${RABBITMQ_URL:-}" in
    *"@rabbitmq-audit:5672/flow_steward_audit") ;;
    *)
      echo "[lint] Refusing tests: RABBITMQ_URL is not the disposable audit vhost." >&2
      return 2
      ;;
  esac
  if [[ "${ARTIFACT_STORAGE_BACKEND:-}" != "local_fs" \
    || "${ARTIFACT_KEY_PREFIX:-}" != "audit/" \
    || "${ARTIFACT_LOCAL_ROOT_DIR:-}" != "/tmp/flow-steward-audit-artifacts" ]]; then
    echo "[lint] Refusing tests: artifact writes are not isolated to the audit tmpfs prefix." >&2
    return 2
  fi
}

run_check_vulture() {
  local vulture_paths=("${TARGETS[@]}")
  local vulture_whitelist="infra/tooling/quality/vulture-whitelist.py"

  if [[ -f "${vulture_whitelist}" ]]; then
    vulture_paths+=("${vulture_whitelist}")
  fi

  run_step \
    "Vulture dead code scan" \
    "${PYTHON_BIN}" -m vulture \
    "${vulture_paths[@]}" \
    --exclude ".git,.venv,venv,node_modules,docs,ops,migrations,tests" \
    --min-confidence "${FS_VULTURE_MIN_CONFIDENCE:-90}"
}

run_check_deptry() {
  run_step \
    "Deptry runtime dependency hygiene scan" \
    "${PYTHON_BIN}" -m deptry . \
    --ignore-notebooks \
    --requirements-files "requirements-runtime.txt" \
    --requirements-files-dev "requirements-audit.txt" \
    --ignore "DEP002,DEP004" \
    --per-rule-ignores "DEP001=platform_remote_sdk|psycopg_runtime" \
    --known-first-party "extensions" \
    --exclude "(\\.git|\\.venv|venv|node_modules|docs|ops|migrations|tests|extensions|core/entrypoints/cli|\\.private|\\.agents|\\.flow-steward|logs|tmp)(/|$)"
  run_step \
    "Deptry CLI dependency hygiene scan" \
    "${PYTHON_BIN}" -m deptry core/entrypoints/cli \
    --ignore-notebooks \
    --requirements-files "requirements-cli.txt,requirements-runtime.txt" \
    --requirements-files-dev "requirements-audit.txt" \
    --ignore "DEP002,DEP004" \
    --per-rule-ignores "DEP001=platform_remote_sdk|psycopg_runtime" \
    --known-first-party "core"
}

run_check_pylint_duplicate() {
  run_step \
    "Pylint duplicate code scan" \
    "${PYTHON_BIN}" -m pylint \
    "${COMPLEXITY_TARGETS[@]}" \
    --disable=all \
    --enable=duplicate-code \
    --min-similarity-lines "${FS_PYLINT_DUPLICATE_MIN_LINES:-12}" \
    --jobs=0 \
    --score=n
}

run_check_radon() {
  run_step "Radon maintainability index report" "${PYTHON_BIN}" -m radon mi -s "${TARGETS[@]}"
}

run_check_xenon() {
  run_step \
    "Xenon complexity thresholds" \
    "${PYTHON_BIN}" -m xenon \
    --max-absolute "${FS_XENON_MAX_ABSOLUTE:-F}" \
    --max-modules "${FS_XENON_MAX_MODULES:-C}" \
    --max-average "${FS_XENON_MAX_AVERAGE:-B}" \
    "${COMPLEXITY_TARGETS[@]}"
}

run_base_checks() {
  run_check_env_artifacts
  run_check_ruff_lint
  run_check_ruff_format
  run_check_mypy
  run_check_bandit
  run_check_ci_excludes
  run_check_provider_neutral_core
  run_check_frontend_lint
}

run_selected_checks() {
  local check_name=""
  for check_name in "${REQUESTED_CHECKS[@]}"; do
    case "${check_name}" in
      env-artifacts|environment-artifacts)
        run_check_env_artifacts
        ;;
      ruff)
        run_check_ruff_lint
        ;;
      ruff-format|format)
        run_check_ruff_format
        ;;
      mypy)
        run_check_mypy
        ;;
      bandit)
        run_check_bandit
        ;;
      pip-audit|pipaudit)
        run_check_pip_audit
        ;;
      semgrep)
        run_check_semgrep
        ;;
      detect-secrets|secrets|secret-scan)
        run_check_detect_secrets
        ;;
      ci-excludes)
        run_check_ci_excludes
        ;;
      sonar-size|sonar-size-precheck)
        run_check_sonar_size
        ;;
      provider-neutral-core|provider-neutral-oauth)
        run_check_provider_neutral_core
        ;;
      extension-oauth-browser|oauth-browser)
        run_check_extension_oauth_browser
        ;;
      tests|pytest)
        run_check_unit_tests_with_coverage
        ;;
      targeted-tests)
        run_check_targeted_tests
        ;;
      extension-static)
        run_check_extension_static
        ;;
      extension-tests)
        run_check_extension_tests
        ;;
      runtime-heartbeat-cross-profile)
        TEST_PATHS="tests/integration/test_runtime_heartbeat_cross_profile.py" run_check_targeted_tests
        ;;
      frontend-targeted-tests)
        run_check_frontend_targeted_tests
        ;;
      frontend-build)
        run_check_frontend_build
        ;;
      release-contracts)
        run_check_release_contracts
        ;;
      candidate-smoke)
        run_check_candidate_smoke
        ;;
      repository-boundary)
        run_check_repository_boundary
        ;;
      ui-i18n)
        run_check_ui_i18n
        ;;
      frontend-lint|frontend-eslint|react-lint)
        run_check_frontend_lint
        ;;
      frontend-test-build|frontend-workspace|frontend-react|frontend-shell)
        run_check_frontend_test_build
        ;;
      frontend-audit|frontend-dependency-audit)
        run_check_frontend_dependency_audit
        ;;
      vulture)
        run_check_vulture
        ;;
      deptry)
        run_check_deptry
        ;;
      pylint-dup|pylint|duplicate-code)
        run_check_pylint_duplicate
        ;;
      radon)
        run_check_radon
        ;;
      xenon)
        run_check_xenon
        ;;
      frontend-sql|frontend)
        run_frontend_sql_audit_checks
        ;;
      security-tests|security-regressions)
        run_security_regression_suite
        ;;
      zap|owasp-zap)
        run_optional_zap_baseline
        ;;
      *)
        echo "[lint] Unsupported check in --checks: ${check_name}" >&2
        echo "[lint] Supported checks: env-artifacts, ruff, ruff-format, mypy, bandit, pip-audit, semgrep, detect-secrets, ci-excludes, sonar-size, provider-neutral-core, tests, targeted-tests, frontend-targeted-tests, frontend-build, ui-i18n, frontend-lint, frontend-test-build, frontend-audit, extension-oauth-browser, vulture, deptry, pylint-dup, radon, xenon, frontend-sql, security-tests, zap, extension-static, extension-tests" >&2
        exit 2
        ;;
    esac
  done
}

# The fs-audit service mounts one cache volume for every checkout. npm's cache is
# content-addressed by integrity hash, so sharing it is safe; without it every
# frontend check downloads the whole dependency tree into a throwaway container.
# A fixed path in the container user's home, not a configurable input.
quality_cache_dir=~/.cache/flow-steward-quality
if [[ -d "${quality_cache_dir}" && -w "${quality_cache_dir}" ]]; then
  export npm_config_cache="${quality_cache_dir}/npm"
  export npm_config_prefer_offline=true
fi

# Backend tests serve the frontend shell from assets/frontend/dist. It used to
# appear only as a side effect of frontend-tests passing, so a fresh worktree, a
# Tier 1 run or any vitest flake left the backend suite answering 503. Every
# backend test run now makes sure the shell matches the current sources itself:
# keyed by the content of assets/ (tests excluded), restored from the shared
# cache when that key was built before, otherwise built with vite.
ensure_frontend_shell() {
  local frontend_dir="${ROOT_DIR}/assets/frontend"
  local dist_dir="${frontend_dir}/dist"
  local stamp="${dist_dir}/.flow-steward-shell-key"
  local key=""
  local cached=""
  local workspace_dir=""

  local sources=""

  [[ -f "${frontend_dir}/package.json" ]] || return 0
  # Without a source listing there is no trustworthy key: build, cache nothing.
  if sources="$(git -C "${ROOT_DIR}" ls-files --cached --others --exclude-standard -- assets)" \
    && [[ -n "${sources}" ]]; then
    key="$(
      {
        node --version 2>/dev/null || echo no-node
        grep -vE '(\.test\.|\.spec\.|/tests?/|/dist/)' <<< "${sources}" \
          | (cd "${ROOT_DIR}" && xargs -d '\n' -r sha256sum 2>/dev/null)
      } | sha256sum | cut -d' ' -f1
    )"
  fi
  if [[ -n "${key}" && -f "${stamp}" && "$(cat -- "${stamp}")" == "${key}" ]]; then
    return 0
  fi
  cached="${quality_cache_dir}/frontend-shell/${key}.tar"
  if [[ -n "${key}" && -f "${cached}" ]]; then
    # GNU tar restores the archive's root-directory metadata after its children.
    # Docker Desktop bind mounts can report a different directory identity then;
    # extract on the container filesystem and publish like a fresh build instead.
    workspace_dir="$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-frontend-shell.XXXXXX")"
    if tar -C "${workspace_dir}" -xf "${cached}" \
      && rm -rf -- "${dist_dir}" \
      && cp -R "${workspace_dir}" "${dist_dir}"; then
      rm -rf -- "${workspace_dir}"
      printf '%s\n' "${key}" > "${stamp}"
      echo "[lint] Frontend shell for these sources restored from the quality cache (${key:0:12})."
      return 0
    fi
    rm -rf -- "${workspace_dir}"
  fi

  echo "[lint] Building the frontend shell for backend tests (${key:0:12})."
  workspace_dir="$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-frontend-shell.XXXXXX")"
  prepare_frontend_workspace "${ROOT_DIR}/assets" "${workspace_dir}/assets"
  if ! (
    cd "${workspace_dir}/assets/frontend" \
      && npm ci --include=optional --no-audit --no-fund \
      && env -u VITE_FRONTEND_EXTENSION_MANIFEST npx --no-install vite build
  ); then
    rm -rf -- "${workspace_dir}"
    echo "[lint] The frontend shell did not build; backend tests cannot serve it." >&2
    return 1
  fi
  rm -rf -- "${dist_dir}"
  cp -R "${workspace_dir}/assets/frontend/dist" "${dist_dir}"
  rm -rf -- "${workspace_dir}"
  [[ -n "${key}" ]] || return 0
  printf '%s\n' "${key}" > "${stamp}"
  if [[ -d "${quality_cache_dir}" && -w "${quality_cache_dir}" ]]; then
    mkdir -p -- "${quality_cache_dir}/frontend-shell"
    if tar -C "${dist_dir}" -cf "${cached}.$$" .; then
      mv -f -- "${cached}.$$" "${cached}"
    else
      rm -f -- "${cached}.$$"
    fi
  fi
}

prepare_frontend_workspace() {
  local source_assets_dir="$1"
  local workspace_assets_dir="$2"

  # Keep npm's mutable dependency tree off the Docker Desktop bind mount.
  mkdir -p "${workspace_assets_dir}"
  tar --exclude='./frontend/node_modules' --exclude='./frontend/dist' \
    -C "${source_assets_dir}" -cf - . \
    | tar -C "${workspace_assets_dir}" -xf -
}

run_check_frontend_lint() {
(
  local source_dir="${ROOT_DIR}/assets/frontend"
  local workspace_dir=""
  local frontend_dir=""

  if [[ ! -f "${source_dir}/package.json" ]]; then
    echo
    echo "[lint] No React frontend workspace found at assets/frontend; skipping frontend lint check."
    return
  fi

  workspace_dir="$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-frontend.XXXXXX")"
  frontend_dir="${workspace_dir}/assets/frontend"
  trap 'rm -rf "${workspace_dir}"' EXIT
  prepare_frontend_workspace "${ROOT_DIR}/assets" "${workspace_dir}/assets"

  run_step "Frontend workspace install (fast)" bash -lc "cd '${frontend_dir}' && npm ci --include=optional"
  run_step "Frontend workspace lint" bash -lc "cd '${frontend_dir}' && npm run lint"
)
}

run_check_frontend_targeted_tests() {
(
  local paths_raw="${TEST_PATHS:-${FS_TEST_PATHS:-}}"
  local paths=()
  local target=""
  local source_dir="${ROOT_DIR}/assets/frontend"
  local workspace_dir=""
  local frontend_dir=""
  local vitest_mode="run"
  local base_ref="main"
  local merge_base=""
  local changed_path=""
  if [[ -z "${paths_raw}" ]]; then
    echo "[lint] frontend-targeted-tests requires explicit src/ targets." >&2
    return 2
  fi
  if [[ "${paths_raw}" == auto || "${paths_raw}" == auto:* ]]; then
    # `auto` hands vitest the changed frontend sources; `vitest related` follows
    # the module graph to every test that imports them, directly or not.
    [[ "${paths_raw}" == auto:* ]] && base_ref="${paths_raw#auto:}"
    if ! merge_base="$(git -C "${ROOT_DIR}" merge-base HEAD "${base_ref}")"; then
      echo "[lint] frontend-targeted-tests could not find the merge base with ${base_ref}." >&2
      return 2
    fi
    paths_raw=""
    while IFS= read -r changed_path; do
      if [[ -f "${ROOT_DIR}/${changed_path}" ]]; then
        paths_raw+=" ${changed_path#assets/frontend/}"
      fi
    done < <(
      {
        git -C "${ROOT_DIR}" diff --name-only "${merge_base}" -- assets/frontend/src
        git -C "${ROOT_DIR}" ls-files --others --exclude-standard -- assets/frontend/src
      } | sort -u
    )
    if [[ -z "${paths_raw// /}" ]]; then
      echo "[lint] frontend-targeted-tests auto (${base_ref}): no changed frontend sources."
      return 0
    fi
    vitest_mode="related"
  fi
  IFS=' ,' read -r -a paths <<< "${paths_raw}"
  for target in "${paths[@]}"; do
    if [[ "${target}" != src/* || "${target}" == src/ || "${target}" == *..* || ! -e "${source_dir}/${target}" ]]; then
      echo "[lint] invalid focused frontend target: ${target}" >&2
      return 2
    fi
  done
  if [[ ${#paths[@]} -eq 0 ]]; then
    echo "[lint] frontend-targeted-tests requires non-empty src/ targets." >&2
    return 2
  fi
  workspace_dir="$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-frontend.XXXXXX")"
  frontend_dir="${workspace_dir}/assets/frontend"
  trap 'rm -rf "${workspace_dir}"' EXIT
  prepare_frontend_workspace "${ROOT_DIR}/assets" "${workspace_dir}/assets"
  run_step "Frontend targeted workspace install" bash -lc 'cd "$1" && npm ci --include=optional' _ "${frontend_dir}"
  run_step "Frontend targeted setup" bash -lc 'cd "$1" && npm run pretest' _ "${frontend_dir}"
  run_step "Frontend targeted tests" bash -lc 'cd "$1" && mode="$2" && shift 2 && npx --no-install vitest "${mode}" --run --bail=1 --coverage.enabled=false "$@"' _ "${frontend_dir}" "${vitest_mode}" "${paths[@]}"
)
}

run_check_frontend_build() {
(
  local workspace_dir=""
  local frontend_dir=""
  workspace_dir="$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-frontend.XXXXXX")"
  frontend_dir="${workspace_dir}/assets/frontend"
  trap 'rm -rf "${workspace_dir}"' EXIT
  prepare_frontend_workspace "${ROOT_DIR}/assets" "${workspace_dir}/assets"
  run_step "Frontend build workspace install" bash -lc 'cd "$1" && npm ci --include=optional' _ "${frontend_dir}"
  run_step "Frontend workspace build" bash -lc 'cd "$1" && npm run build' _ "${frontend_dir}"
)
}

run_check_frontend_test_build() {
(
  local source_dir="${ROOT_DIR}/assets/frontend"
  local workspace_dir=""
  local frontend_dir=""

  if [[ ! -f "${source_dir}/package.json" ]]; then
    echo
    echo "[lint] No React frontend workspace found at assets/frontend; skipping frontend workspace checks."
    return
  fi

  workspace_dir="$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-frontend.XXXXXX")"
  frontend_dir="${workspace_dir}/assets/frontend"
  trap 'rm -rf "${workspace_dir}"' EXIT
  prepare_frontend_workspace "${ROOT_DIR}/assets" "${workspace_dir}/assets"

  run_step "Frontend workspace install" bash -lc "cd '${frontend_dir}' && npm ci --include=optional"
  run_step "Frontend workspace tests" bash -lc "cd '${frontend_dir}' && npm run test"
  run_step "Frontend workspace build" bash -lc "cd '${frontend_dir}' && npm run build"
  run_step \
    "Publish frontend shell for backend tests" \
    bash -lc "rm -rf '${source_dir}/dist' && cp -R '${frontend_dir}/dist' '${source_dir}/dist'"
)
}

run_check_frontend_dependency_audit() {
(
  local source_dir="${ROOT_DIR}/assets/frontend"
  local workspace_dir=""
  local frontend_dir=""

  if [[ ! -f "${source_dir}/package.json" ]]; then
    echo "[lint] No React frontend workspace found; skipping frontend dependency audit."
    return
  fi
  workspace_dir="$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-frontend-audit.XXXXXX")"
  frontend_dir="${workspace_dir}/assets/frontend"
  trap 'rm -rf "${workspace_dir}"' EXIT
  prepare_frontend_workspace "${ROOT_DIR}/assets" "${workspace_dir}/assets"
  run_step "Frontend dependency audit (fresh container)" bash -lc \
    "cd '${frontend_dir}' && '${SCRIPT_DIR}/npm_audit_retry.sh' --audit-level=high"
)
}

run_frontend_sql_audit_checks() {
  local js_files=()
  local css_files=()
  local html_files=()
  local sql_files=()
  local html_ext="html"

  while IFS= read -r file; do
    js_files+=("${file}")
  done < <(
    find assets \
      \( -path 'assets/frontend/*' -o -path '*/node_modules/*' -o -path '*/dist/*' -o -path '*/vendor/*' \) -prune \
      -o -type f -name '*.js' ! -name '*.min.js' -print 2>/dev/null | sort
  )

  while IFS= read -r file; do
    css_files+=("${file}")
  done < <(
    find assets \
      \( -path '*/node_modules/*' -o -path '*/dist/*' -o -path '*/vendor/*' \) -prune \
      -o -type f -name '*.css' -print 2>/dev/null | sort
  )

  while IFS= read -r file; do
    html_files+=("${file}")
  done < <(
    find assets \
      \( -path '*/node_modules/*' -o -path '*/dist/*' -o -path '*/vendor/*' \) -prune \
      -o -type f -name "*.${html_ext}" -print 2>/dev/null | sort
  )

  while IFS= read -r file; do
    sql_files+=("${file}")
  done < <(find db/schema -type f -name '*.sql' 2>/dev/null | sort)

  if [[ ${#js_files[@]} -gt 0 ]]; then
    run_step "ESLint (JS quality)" eslint "${js_files[@]}"
  else
    echo
    echo "[lint] No JS files found for ESLint scan."
  fi

  if [[ ${#css_files[@]} -gt 0 ]]; then
    run_step \
      "Stylelint (CSS quality)" \
      stylelint --config "${FS_TOOLING_DIR}/.stylelintrc.json" "${css_files[@]}"
  else
    echo
    echo "[lint] No CSS files found for Stylelint scan."
  fi

  if [[ ${#html_files[@]} -gt 0 ]]; then
    run_step \
      "HTMLHint (HTML quality)" \
      htmlhint --config "${FS_TOOLING_DIR}/.htmlhintrc" "${html_files[@]}"
  else
    echo
    echo "[lint] No asset/wireframe HTML files found for HTMLHint scan."
  fi

  if [[ ${#sql_files[@]} -gt 0 ]]; then
    run_step \
      "SQLFluff (Postgres SQL quality)" \
      "${PYTHON_BIN}" -m sqlfluff lint --config "${FS_TOOLING_DIR}/.sqlfluff" "${sql_files[@]}"
  else
    echo
    echo "[lint] No schema SQL files found for SQLFluff scan."
  fi
}

if [[ ${#REQUESTED_CHECKS[@]} -gt 0 ]]; then
  echo "[lint] Mode: selected checks"
  echo "[lint] Requested checks: ${REQUESTED_CHECKS[*]}"
else
  echo "[lint] Mode: ${MODE}"
fi
echo "[lint] Targets: ${TARGETS[*]}"

if [[ ${#REQUESTED_CHECKS[@]} -gt 0 ]]; then
  run_selected_checks
else
  case "${MODE}" in fast) tier=2 ;; full) tier=3 ;; *) echo "[lint] Unsupported mode: ${MODE}" >&2; exit 2 ;; esac
  quality_verify_args=(
    --tier "${tier}"
    --evidence-mode "${EVIDENCE_MODE}"
    --fingerprint-source "${FINGERPRINT_SOURCE}"
    --trust "${evidence_trust}"
    --repository-root "${ROOT_DIR}"
  )
  if [[ -n "${commit_sha}" ]]; then
    quality_verify_args+=(--commit-sha "${commit_sha}")
  fi
  "${PYTHON_BIN}" -m core.application.tooling.quality.verify "${quality_verify_args[@]}"
fi

if [[ "${WITH_SONAR}" -eq 1 || "${SONAR_ENABLED:-0}" == "1" ]]; then
  run_host_sonar_scan "${SONAR_PROFILE:-stable}"
fi

echo
if [[ ${#REQUESTED_CHECKS[@]} -gt 0 ]]; then
  echo "[lint] Quality gate passed (selected checks)."
else
  echo "[lint] Quality gate passed (${MODE})."
fi
