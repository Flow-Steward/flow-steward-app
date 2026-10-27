#!/usr/bin/env bash
# Flow Steward permanent release smoke gate.
# Usage: release-smoke.sh [A B C D E]
# Exit status is the failure count capped at 125.
set -uo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
MANIFEST="${FS_SMOKE_MANIFEST:-$SCRIPT_DIR/checks.json}"
GROUP_ROOT="${FS_SMOKE_GROUP_ROOT:-$SCRIPT_DIR}"
PROFILE_LIST="${FS_SMOKE_PROFILES:-oss}"
SMOKE_BASE="${FS_SMOKE_BASE:-https://localhost:8099}"
LOG_ALLOWLIST="${FS_SMOKE_LOG_ALLOWLIST:-$SCRIPT_DIR/candidate-log-allowlist.json}"
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-smoke.XXXXXX")"
CLEANUP_RESULT=pending

cleanup() {
  local rc=$?
  if rm -rf -- "$RUN_DIR"; then
    CLEANUP_RESULT=ok
  else
    CLEANUP_RESULT=failed
  fi
  printf 'cleanup_result=%s\n' "$CLEANUP_RESULT"
  return "$rc"
}
trap cleanup EXIT INT TERM

die() {
  printf 'release-smoke: %s\n' "$1"
  exit "${2:-1}"
}

assert_candidate_container() {
  local role="$1" container="$2" require_digest="${3:-1}" metadata image_id project
  [ -n "$container" ] || die "release profile requires explicit $role container"
  metadata="$(docker inspect "$container" \
    --format '{{.Image}}|{{index .Config.Labels "com.docker.compose.project"}}' \
    2>/dev/null)" || die "cannot inspect $role container '$container'"
  image_id="${metadata%%|*}"
  project="${metadata#*|}"
  [ "$project" = "$FS_SMOKE_COMPOSE_PROJECT" ] || die \
    "$role container project mismatch: expected $FS_SMOKE_COMPOSE_PROJECT, got ${project:-missing}"
  if [ "$require_digest" = 1 ]; then
    case ",$FS_SMOKE_IMAGE_DIGESTS," in
      *"$image_id"*) ;;
      *) die "$role container image $image_id is absent from FS_SMOKE_IMAGE_DIGESTS" ;;
    esac
  fi
}

[ -r "$MANIFEST" ] || die "manifest is not readable: $MANIFEST"
python3 - "$MANIFEST" <<'PY' || die "manifest validation failed"
import json, re, sys

path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
if data.get("schema_version") != "flow-steward-release-smoke-v1":
    raise SystemExit("unsupported schema_version")
checks = data.get("checks")
if not isinstance(checks, list) or not checks:
    raise SystemExit("checks must be a non-empty list")
seen = set()
for check in checks:
    check_id = check.get("id", "")
    if not re.fullmatch(r"[A-E][0-9]{2}", check_id) or check_id in seen:
        raise SystemExit(f"invalid or duplicate check id: {check_id!r}")
    seen.add(check_id)
    if check.get("group") != check_id[0]:
        raise SystemExit(f"group mismatch for {check_id}")
    if check.get("profile") not in {"oss", "tenant", "deploy"}:
        raise SystemExit(f"invalid profile for {check_id}")
    if not isinstance(check.get("required"), bool):
        raise SystemExit(f"required must be boolean for {check_id}")
    if not str(check.get("description") or "").strip():
        raise SystemExit(f"description missing for {check_id}")
PY
MANIFEST_TOTAL="$(python3 - "$MANIFEST" <<'PY'
import json, sys
print(len(json.load(open(sys.argv[1], encoding="utf-8"))["checks"]))
PY
)"

REQUESTED_PROFILE=""
POSITIONAL_GROUPS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile)
      [ "$#" -ge 2 ] || die "--profile requires a value"
      REQUESTED_PROFILE="$2"
      shift 2
      ;;
    --profile=*)
      REQUESTED_PROFILE="${1#--profile=}"
      shift
      ;;
    *)
      POSITIONAL_GROUPS[${#POSITIONAL_GROUPS[@]}]="$1"
      shift
      ;;
  esac
done

case "$REQUESTED_PROFILE" in
  "") ;;
  release)
    PROFILE_LIST="oss,tenant,deploy"
    FS_SMOKE_DEPLOY=1
    export FS_SMOKE_DEPLOY
    ;;
  oss|tenant|deploy) PROFILE_LIST="$REQUESTED_PROFILE" ;;
  *) die "unknown profile '$REQUESTED_PROFILE' (expected release, oss, tenant, or deploy)" ;;
esac

VERIFY_CLEAN_EXIT=0
if [ "$REQUESTED_PROFILE" = release ] || [ "${FS_SMOKE_VERIFY_CLEAN_EXIT:-0}" = 1 ]; then
  VERIFY_CLEAN_EXIT=1
fi

if [ "${#POSITIONAL_GROUPS[@]}" -eq 0 ]; then
  REQUESTED_GROUPS=(A B C D E)
else
  REQUESTED_GROUPS=("${POSITIONAL_GROUPS[@]}")
fi
for group in "${REQUESTED_GROUPS[@]}"; do
  case "$group" in A|B|C|D|E) ;; *) die "unknown group '$group' (expected A-E)" ;; esac
done

candidate_requirement_preflight() {
  local requirements_file="$RUN_DIR/selected-requirements.tsv"
  python3 - "$MANIFEST" "$GROUP_ROOT" "$PROFILE_LIST" "${REQUESTED_GROUPS[@]}" \
    >"$requirements_file" <<'PY' || die "selected smoke requirement declarations are invalid"
import json
import pathlib
import sys

manifest_path = pathlib.Path(sys.argv[1])
group_root = pathlib.Path(sys.argv[2])
profiles = set(sys.argv[3].split(","))
requested_groups = set(sys.argv[4:])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
selected = [
    row for row in manifest["checks"]
    if row["group"] in requested_groups and row["profile"] in profiles
]
required_env = set()
services = set()
declarations = []
shared = group_root / "requirements.json"
if shared.is_file():
    declarations.append(json.loads(shared.read_text(encoding="utf-8")))
for group in sorted({row["group"] for row in selected}):
    path = group_root / f"group{group}" / "requirements.json"
    if path.is_file():
        declarations.append(json.loads(path.read_text(encoding="utf-8")))
    elif group_root.name == "smoke":
        raise SystemExit(f"missing consumer requirement declaration: {path}")
for declaration in declarations:
    common = declaration.get("common") or {}
    required_env.update(common.get("required_env") or [])
    services.update(common.get("services") or [])
    checks = declaration.get("checks") or {}
    for row in selected:
        requirements = checks.get(row["id"])
        if not isinstance(requirements, dict):
            continue
        required_env.update(requirements.get("required_env") or [])
        services.update(requirements.get("services") or [])
for name in sorted(required_env):
    print(f"env\t{name}")
for name in sorted(services):
    print(f"service\t{name}")
PY

  local kind name value container_var
  while IFS="$(printf '\t')" read -r kind name; do
    [ -n "$name" ] || continue
    case "$kind" in
      env)
        value="${!name:-}"
        [ -n "$value" ] || die "required candidate input is missing: $name"
        case "$name" in
          *_SCRIPT)
            [ -x "$value" ] || die "required operator script is not executable: $name=$value"
            ;;
        esac
        ;;
      service)
        case "$name" in
          fs-app) container_var=FS_SMOKE_APP_CONTAINER ;;
          fs-cli) container_var=FS_SMOKE_CLI_CONTAINER ;;
          fs-worker) container_var=FS_SMOKE_WORKER_CONTAINER ;;
          pg-fs) container_var=FS_SMOKE_PG_CONTAINER ;;
          mailpit) container_var=FS_SMOKE_MAILPIT_CONTAINER ;;
          rabbitmq) container_var=FS_SMOKE_RABBITMQ_CONTAINER ;;
          *) die "unknown required candidate service: $name" ;;
        esac
        value="${!container_var:-}"
        [ -n "$value" ] || die "required candidate container is missing: $container_var ($name)"
        ;;
    esac
  done <"$requirements_file"
  printf 'candidate_requirement_preflight=ok\n'
}

candidate_requirement_preflight

worker_count() {
  if [ -n "${FS_SMOKE_WORKER_COUNT:-}" ]; then
    printf '%s\n' "$FS_SMOKE_WORKER_COUNT"
    return
  fi
  command -v docker >/dev/null 2>&1 || { printf '0\n'; return; }
  local project_filter=()
  if [ -n "${FS_SMOKE_COMPOSE_PROJECT:-}" ]; then
    project_filter=(--filter "label=com.docker.compose.project=$FS_SMOKE_COMPOSE_PROJECT")
  fi
  docker ps "${project_filter[@]}" \
    --filter 'label=com.docker.compose.service=fs-worker' \
    --filter 'status=running' \
    --format '{{.ID}}' 2>/dev/null | awk 'NF { count += 1 } END { print count + 0 }'
}

WORKER_COUNT="$(worker_count)"
CANDIDATE_COMMIT="${FS_SMOKE_CANDIDATE_COMMIT:-$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || printf unknown)}"
if [ -n "${FS_SMOKE_IMAGE_DIGESTS:-}" ]; then
  IMAGE_DIGESTS="$FS_SMOKE_IMAGE_DIGESTS"
elif command -v docker >/dev/null 2>&1; then
  if [ -n "${FS_SMOKE_COMPOSE_PROJECT:-}" ]; then
    IMAGE_DIGESTS="$(docker ps --filter "label=com.docker.compose.project=$FS_SMOKE_COMPOSE_PROJECT" --format '{{.Image}}={{.ID}}' 2>/dev/null | sort -u | paste -sd, -)"
  else
    IMAGE_DIGESTS=unavailable
  fi
else
  IMAGE_DIGESTS=unavailable
fi

printf 'candidate_commit=%s\n' "$CANDIDATE_COMMIT"
printf 'image_digests=%s\n' "${IMAGE_DIGESTS:-unavailable}"
printf 'environment_profiles=%s\n' "$PROFILE_LIST"
printf 'worker_count=%s\n' "$WORKER_COUNT"
printf 'shell=%s\n' "${BASH_VERSION:-unknown}"

if [ "$REQUESTED_PROFILE" = release ]; then
  [ -n "${FS_SMOKE_CANDIDATE_COMMIT:-}" ] || die \
    "release profile requires explicit FS_SMOKE_CANDIDATE_COMMIT"
  [ -n "${FS_SMOKE_IMAGE_DIGESTS:-}" ] || die \
    "release profile requires explicit FS_SMOKE_IMAGE_DIGESTS"
  [ -n "${FS_SMOKE_COMPOSE_PROJECT:-}" ] || die \
    "release profile requires explicit FS_SMOKE_COMPOSE_PROJECT"
  [ -n "${FS_SMOKE_APP_CONTAINER:-}" ] || die \
    "release profile requires explicit FS_SMOKE_APP_CONTAINER"
  command -v docker >/dev/null 2>&1 || die "release profile requires Docker metadata access"
  candidate_metadata="$(docker inspect "$FS_SMOKE_APP_CONTAINER" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}|{{.Image}}|{{index .Config.Labels "com.docker.compose.project"}}' \
    2>/dev/null)" || die "cannot inspect candidate app container '$FS_SMOKE_APP_CONTAINER'"
  candidate_label_commit="${candidate_metadata%%|*}"
  candidate_metadata_tail="${candidate_metadata#*|}"
  candidate_image_id="${candidate_metadata_tail%%|*}"
  candidate_project="${candidate_metadata_tail#*|}"
  [ "$candidate_label_commit" = "$CANDIDATE_COMMIT" ] || die \
    "candidate image revision mismatch: expected $CANDIDATE_COMMIT, got ${candidate_label_commit:-missing}"
  [ "$candidate_project" = "$FS_SMOKE_COMPOSE_PROJECT" ] || die \
    "candidate compose project mismatch: expected $FS_SMOKE_COMPOSE_PROJECT, got ${candidate_project:-missing}"
  case ",$FS_SMOKE_IMAGE_DIGESTS," in
    *"$candidate_image_id"*) ;;
    *) die "candidate app image $candidate_image_id is absent from FS_SMOKE_IMAGE_DIGESTS" ;;
  esac
  [ -n "${FS_SMOKE_DB_HOST:-}" ] || die "release profile requires explicit FS_SMOKE_DB_HOST"
  [ -n "${FS_SMOKE_INTERNAL_BASE:-}" ] || die \
    "release profile requires explicit FS_SMOKE_INTERNAL_BASE"
  [ -n "${FS_SMOKE_MAILPIT_BASE:-}" ] || die \
    "release profile requires explicit FS_SMOKE_MAILPIT_BASE"
  [ -n "${FS_SMOKE_MCP_CA_FILE:-}" ] && [ -r "$FS_SMOKE_MCP_CA_FILE" ] || die \
    "release profile requires readable FS_SMOKE_MCP_CA_FILE for public MCP TLS verification"
  [ -n "${FS_SMOKE_BEARER_TOKEN:-}" ] || die \
    "release profile requires protected FS_SMOKE_BEARER_TOKEN for CLI and MCP authentication"
  [ -n "${FS_SMOKE_MODEL_PROVIDER:-}" ] || die \
    "release profile requires explicit FS_SMOKE_MODEL_PROVIDER"
  [ -n "${FS_SMOKE_MODEL:-}" ] || die \
    "release profile requires explicit FS_SMOKE_MODEL"
  case "$FS_SMOKE_MODEL_PROVIDER" in
    custom:*) ;;
    *) die "release profile B01 requires an account-scoped custom:<provider-id> route" ;;
  esac
  assert_candidate_container cli "${FS_SMOKE_CLI_CONTAINER:-}"
  assert_candidate_container postgres "${FS_SMOKE_PG_CONTAINER:-}" 0
  assert_candidate_container worker "${FS_SMOKE_WORKER_CONTAINER:-}"
  assert_candidate_container mailpit "${FS_SMOKE_MAILPIT_CONTAINER:-}" 0
  assert_candidate_container rabbitmq "${FS_SMOKE_RABBITMQ_CONTAINER:-}" 0
  candidate_public_base="$(docker exec "$FS_SMOKE_APP_CONTAINER" sh -c 'printf %s "$FS_PUBLIC_BASE_URL"' 2>/dev/null)"
  [ "$candidate_public_base" = "$SMOKE_BASE" ] || die \
    "candidate FS_PUBLIC_BASE_URL mismatch: expected $SMOKE_BASE, got ${candidate_public_base:-missing}"
  export FS_PUBLIC_BASE_URL="$candidate_public_base"
fi

if [ "$VERIFY_CLEAN_EXIT" = 1 ]; then
  [ -n "${FS_SMOKE_COMPOSE_PROJECT:-}" ] || die \
    "clean-exit verification requires explicit FS_SMOKE_COMPOSE_PROJECT"
  [ -x "$SCRIPT_DIR/candidate_residue_check.py" ] || die \
    "required operator script is not executable: $SCRIPT_DIR/candidate_residue_check.py"
  [ -x "$SCRIPT_DIR/candidate_log_window_check.py" ] || die \
    "required operator script is not executable: $SCRIPT_DIR/candidate_log_window_check.py"
  [ -r "$LOG_ALLOWLIST" ] || die \
    "candidate log allowlist is not readable: $LOG_ALLOWLIST"
fi

health_response="$(curl -skS -w '\n%{http_code}' "$SMOKE_BASE/health" 2>/dev/null)" || die \
  "candidate is unreachable at $SMOKE_BASE"
health_code="${health_response##*$'\n'}"
health_body="${health_response%$'\n'*}"
[ "$health_code" = 200 ] || die \
  "candidate health returned HTTP ${health_code:-000} at $SMOKE_BASE"
health_commit="$(HEALTH_BODY="$health_body" python3 - <<'PY'
import json
import os

try:
    payload = json.loads(os.environ["HEALTH_BODY"])
except Exception:
    print("")
else:
    print(str(payload.get("build_sha") or payload.get("commit") or "").strip())
PY
)"
[ -n "$health_commit" ] || die "candidate health did not report build_sha"
[ "$health_commit" = "$CANDIDATE_COMMIT" ] || die \
  "candidate health commit mismatch: expected $CANDIDATE_COMMIT, got $health_commit"
printf 'candidate_health=verified base=%s build_sha=%s compose_project=%s\n' \
  "$SMOKE_BASE" "$health_commit" "${FS_SMOKE_COMPOSE_PROJECT:-unspecified}"

case "$WORKER_COUNT" in
  1) ;;
  *) die "exactly one queue consumer is required before mutation (worker_count=$WORKER_COUNT)" ;;
esac

# Preflight for the mutation groups. Without this the gate ran to completion with
# no credentials and reported 20 product-looking failures — "duplicate name ->
# HTTP , expected 409" — when the real cause was an unset FS_SMOKE_EMAIL. A gate
# that misattributes its own missing inputs to the product is worse than no gate,
# so the inputs are proven before anything is mutated.
needs_api=0
for group in "${REQUESTED_GROUPS[@]}"; do
  case "$group" in B|C|D) needs_api=1 ;; esac
done

if [ "$needs_api" -eq 1 ]; then
  [ -n "${FS_SMOKE_EMAIL:-}" ] && [ -n "${FS_SMOKE_PASSWORD:-}" ] || die \
    "groups B/C/D need FS_SMOKE_EMAIL and FS_SMOKE_PASSWORD; supply them through protected environment inputs"
  [ "${FS_SMOKE_ISOLATED:-0}" = 1 ] || die \
    "groups B/C/D mutate data and require FS_SMOKE_ISOLATED=1 against a disposable audit data plane"
  [ -n "${FS_SMOKE_PG_CONTAINER:-}" ] || die \
    "groups B/C/D require FS_SMOKE_PG_CONTAINER naming the isolated PostgreSQL container"
  if command -v docker >/dev/null 2>&1; then
    docker ps --filter "name=^/${FS_SMOKE_PG_CONTAINER}$" --filter 'status=running' \
      --format '{{.ID}}' 2>/dev/null | grep -q . || die \
      "isolated PostgreSQL container '${FS_SMOKE_PG_CONTAINER}' is not running"
  fi
  # FS_SMOKE_ISOLATED and FS_SMOKE_PG_CONTAINER only asserted that *a* disposable
  # container was running — nothing checked that the application under test was
  # bound to it. On this host the candidate at localhost:8099 writes to pg-fs (42
  # projects, 67 workflows, 134 jobs) while the flags named pg-audit, so groups
  # B/C/D would have purged, hard-deleted and dropped workspaces in real data
  # while the gate printed "isolated". The binding is now proven, or nothing runs.
  app_container="${FS_SMOKE_APP_CONTAINER:-}"
  if [ -z "$app_container" ] && command -v docker >/dev/null 2>&1; then
    app_container="$(docker ps --filter 'label=com.docker.compose.service=fs-app' \
      --filter "label=com.docker.compose.project=${FS_SMOKE_COMPOSE_PROJECT:-}" \
      --filter 'status=running' --format '{{.Names}}' 2>/dev/null | head -n 1)"
  fi
  [ -n "$app_container" ] || die \
    "cannot identify the application container under test; set FS_SMOKE_APP_CONTAINER"
  app_dsn_host="$(docker exec "$app_container" sh -c 'printf %s "$FS_DB_DSN"' 2>/dev/null \
    | tr ' ' '\n' | awk -F= '$1 == "host" { print $2; exit }')"
  [ -n "$app_dsn_host" ] || die \
    "could not read the database host of '$app_container'; refusing to mutate an unverified data plane"
  pg_aliases="$(docker inspect "$FS_SMOKE_PG_CONTAINER" \
    --format '{{.Name}} {{index .Config.Labels "com.docker.compose.service"}} {{range .NetworkSettings.Networks}}{{range .Aliases}}{{.}} {{end}}{{end}}' \
    2>/dev/null | tr -d '/' )"
  case " $pg_aliases " in
    *" $app_dsn_host "*) ;;
    *) die "REFUSING TO RUN: '$app_container' writes to database host '$app_dsn_host', which is not '${FS_SMOKE_PG_CONTAINER}' (aliases: ${pg_aliases:-none}). Point the candidate at the isolated data plane before running destructive groups." ;;
  esac
  printf 'data_plane_binding=verified app=%s db_host=%s\n' "$app_container" "$app_dsn_host"

  smoke_base="$SMOKE_BASE"
  login_code="$(curl -sk -o /dev/null -w '%{http_code}' -X POST "$smoke_base/api/auth/login" \
    -H 'Content-Type: application/json' -H "Origin: $smoke_base" \
    --data-binary "$(python3 - "$FS_SMOKE_EMAIL" "$FS_SMOKE_PASSWORD" \
      "${FS_SMOKE_ACCOUNT:-00000000-0000-0000-0000-000000000002}" <<'PY'
import json, sys
print(json.dumps({"email": sys.argv[1], "password": sys.argv[2], "account_id": sys.argv[3]}))
PY
)" 2>/dev/null || printf '000')"
  [ "$login_code" = 200 ] || die \
    "smoke credentials could not authenticate against $smoke_base (HTTP $login_code); refusing to run mutation groups"
  printf 'api_preflight=ok base=%s\n' "$smoke_base"
fi

CANDIDATE_LOG_WINDOW_START=""
if [ "$VERIFY_CLEAN_EXIT" = 1 ]; then
  CANDIDATE_LOG_WINDOW_START="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf 'candidate_log_window_start=%s\n' "$CANDIDATE_LOG_WINDOW_START"
fi

profile_enabled() {
  local wanted="$1"
  case ",$PROFILE_LIST," in *",$wanted,"*) return 0 ;; *) return 1 ;; esac
}

PASS=0
FAIL=0
SKIP=0
SELECTED=0
PROFILE_EXCLUDED=0

for group in "${REQUESTED_GROUPS[@]}"; do
  rows_file="$RUN_DIR/rows-$group.tsv"
  python3 - "$MANIFEST" "$group" >"$rows_file" <<'PY'
import json, sys
for check in json.load(open(sys.argv[1], encoding="utf-8"))["checks"]:
    if check["group"] == sys.argv[2]:
        print("\t".join((check["id"], check["profile"], "1" if check["required"] else "0")))
PY

  # Portable to bash 3.2: no mapfile, no associative arrays. The gate has to run
  # on whatever shell the operator has, or it is not a gate — macOS still ships
  # bash 3.2 and the first attempt died here without emitting a single result.
  selected_file="$RUN_DIR/selected-$group.tsv"
  : >"$selected_file"
  ids=()
  while IFS="$(printf '\t')" read -r check_id profile required; do
    [ -n "$check_id" ] || continue
    if ! profile_enabled "$profile"; then
      PROFILE_EXCLUDED=$((PROFILE_EXCLUDED + 1))
      continue
    fi
    if [ "$profile" = deploy ] && [ "${FS_SMOKE_DEPLOY:-0}" != 1 ]; then
      PROFILE_EXCLUDED=$((PROFILE_EXCLUDED + 1))
      continue
    fi
    ids[${#ids[@]}]="$check_id"
    printf '%s\t%s\n' "$check_id" "$required" >>"$selected_file"
  done <"$rows_file"
  [ "${#ids[@]}" -gt 0 ] || continue

  SELECTED=$((SELECTED + ${#ids[@]}))
  case "$group" in
    A) runner="$GROUP_ROOT/groupA/smoke_A.sh" ;;
    B) runner="$GROUP_ROOT/groupB/smoke_b.sh" ;;
    C) runner="$GROUP_ROOT/groupC/smoke_groupC.sh" ;;
    D) runner="$GROUP_ROOT/groupD/smoke_D.sh" ;;
    E) runner="$GROUP_ROOT/groupE/smoke_E.sh" ;;
  esac
  output="$RUN_DIR/group-$group.out"
  group_rc=0
  if [ ! -x "$runner" ]; then
    : >"$output"
    group_rc=126
  else
    FS_SMOKE_MANIFEST="$MANIFEST" "$runner" "${ids[@]}" >"$output" 2>&1 || group_rc=$?
  fi
  group_reported_nonpass=0
  if awk '$2 ~ /^(FAIL|SKIP):?$/ { found=1 } END { exit(found ? 0 : 1) }' "$output"; then
    group_reported_nonpass=1
  fi

  for check_id in "${ids[@]}"; do
    required="$(awk -v id="$check_id" -F'\t' '$1 == id { print $2; exit }' "$selected_file")"
    verdict="$(awk -v id="$check_id" '
      $1 == id {
        v = $2; sub(/:$/, "", v)
        if (v == "PASS" || v == "FAIL" || v == "SKIP") { print v; exit }
      }' "$output")"
    detail="$(awk -v id="$check_id" '
      $1 == id {
        v = $2; sub(/:$/, "", v)
        if (v == "PASS" || v == "FAIL" || v == "SKIP") {
          $1 = ""; $2 = ""; sub(/^[ \t]+/, "")
          print; exit
        }
      }' "$output")"
    case "${verdict:-MISSING}" in
      PASS)
        if [ "$group_rc" -ne 0 ] && [ "$group_reported_nonpass" -eq 0 ]; then
          FAIL=$((FAIL + 1)); printf '%s FAIL group runner exited %d%s\n' "$check_id" "$group_rc" "${detail:+: $detail}"
        else
          PASS=$((PASS + 1)); printf '%s PASS %s\n' "$check_id" "$detail"
        fi
        ;;
      SKIP)
        if [ "$required" = 1 ]; then
          FAIL=$((FAIL + 1)); printf '%s FAIL required check was skipped%s\n' "$check_id" "${detail:+: $detail}"
        else
          SKIP=$((SKIP + 1)); printf '%s SKIP %s\n' "$check_id" "$detail"
        fi
        ;;
      FAIL)
        FAIL=$((FAIL + 1)); printf '%s FAIL %s\n' "$check_id" "$detail"
        ;;
      *)
        FAIL=$((FAIL + 1)); printf '%s FAIL group emitted no result\n' "$check_id"
        ;;
    esac
  done
done

if [ "$VERIFY_CLEAN_EXIT" = 1 ]; then
  residue_rc=0
  "$SCRIPT_DIR/candidate_residue_check.py" || residue_rc=$?
  if [ "$residue_rc" -ne 0 ]; then
    FAIL=$((FAIL + 1))
    printf 'cleanup_residue=failed exit=%d\n' "$residue_rc"
  else
    printf 'cleanup_residue=ok\n'
  fi

  candidate_log_window_end="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  log_window_rc=0
  "$SCRIPT_DIR/candidate_log_window_check.py" \
    --compose-project "$FS_SMOKE_COMPOSE_PROJECT" \
    --since "$CANDIDATE_LOG_WINDOW_START" \
    --until "$candidate_log_window_end" \
    --allowlist "$LOG_ALLOWLIST" || log_window_rc=$?
  if [ "$log_window_rc" -ne 0 ]; then
    FAIL=$((FAIL + 1))
  fi
fi

if [ "$REQUESTED_PROFILE" = release ] && [ "${#POSITIONAL_GROUPS[@]}" -eq 0 ]; then
  [ "$SELECTED" -eq "$MANIFEST_TOTAL" ] || die \
    "release profile selected $SELECTED of $MANIFEST_TOTAL manifest checks"
  [ "$PROFILE_EXCLUDED" -eq 0 ] || die \
    "release profile excluded $PROFILE_EXCLUDED required manifest checks"
fi

printf 'totals pass=%d fail=%d skip=%d selected=%d profile_excluded=%d manifest=%d\n' \
  "$PASS" "$FAIL" "$SKIP" "$SELECTED" "$PROFILE_EXCLUDED" "$MANIFEST_TOTAL"

[ "$FAIL" -le 125 ] || FAIL=125
exit "$FAIL"
