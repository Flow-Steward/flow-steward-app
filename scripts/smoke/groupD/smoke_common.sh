#!/usr/bin/env bash
# regD smoke suite - shared preamble. source this first.
set -uo pipefail
BASE="${BASE:-${FS_SMOKE_BASE:-https://localhost:8099}}"
ACCOUNT_ID="${ACCOUNT_ID:-00000000-0000-0000-0000-000000000002}"
# Prefer the credentials the release runner validates and supplies for groups
# B/C/D; the e2e fixture user only exists on installations that seeded it, so
# defaulting to it makes every check 401 on a clean install.
FS_EMAIL="${FS_EMAIL:-${FS_SMOKE_EMAIL:-e2e_admin@example.com}}"
FS_PASSWORD="${FS_PASSWORD:-${FS_SMOKE_PASSWORD:-e2e_admin_password_123!}}"
# Derive the repository from this script's own location rather than one
# developer's absolute path.
REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || (cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd))}"
candidate_extensions_root() {
  local app="${FS_SMOKE_APP_CONTAINER:-}" mounted host_path
  [ -n "$app" ] || return 1
  mounted="$(docker inspect "$app" \
    --format '{{range .Mounts}}{{if eq .Destination "/app/extensions"}}{{.Source}}{{end}}{{end}}' \
    2>/dev/null)" || return 1
  [ -n "$mounted" ] || return 1
  case "$mounted" in
    /host_mnt/*)
      host_path="${mounted#/host_mnt}"
      [ -d "$host_path" ] && mounted="$host_path"
      ;;
  esac
  printf '%s' "$mounted"
}
if [ -z "${EXT_ROOT:-}" ]; then
  EXT_ROOT="$(candidate_extensions_root 2>/dev/null || true)"
fi
EXT_ROOT="${EXT_ROOT:-$REPO_ROOT/extensions}"
PGC="${FS_SMOKE_PG_CONTAINER:-${PGC:-}}"
PSQL() { docker exec "$PGC" psql -U flow_steward -d flow_steward -tAc "$1"; }
assert_project_queues_removed() {
  local project_id="$1" rabbit="${FS_SMOKE_RABBITMQ_CONTAINER:-}" queue queues orphaned=0
  [ -n "$rabbit" ] || { echo "FS_SMOKE_RABBITMQ_CONTAINER is required" >&2; return 1; }
  queues="$(docker exec "$rabbit" rabbitmqctl list_queues --quiet name)" || return 1
  for queue in "platform.project.$project_id" "platform.project.$project_id.dead_letter"; do
    if printf '%s\n' "$queues" | grep -Fqx "$queue"; then
      orphaned=1
      # Bounded fail-safe cleanup after recording a product assertion failure.
      docker exec "$rabbit" rabbitmqctl purge_queue "$queue" >/dev/null || return 1
    fi
  done
  [ "$orphaned" -eq 0 ]
}
_JAR="$(mktemp)"; _CODEF="$(mktemp)"; _CSRF=""; FS_CODE=""
# HTTP status of the most recent fs call (survives command substitution)
last_code() { cat "$_CODEF" 2>/dev/null; }
fs_login() {
  _CSRF=$(curl -sk -c "$_JAR" -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
    -H "Origin: $BASE" -d "{\"email\":\"$FS_EMAIL\",\"password\":\"$FS_PASSWORD\",\"account_id\":\"$ACCOUNT_ID\"}" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("csrf_token",""))')
}
# fs METHOD PATH [BODY] -> body on stdout, sets FS_CODE
fs() {
  [ -n "$_CSRF" ] || fs_login
  local m="$1" p="$2" b="${3:-}" a
  a=(-sk -b "$_JAR" -c "$_JAR" -X "$m" "$BASE$p" -H "Origin: $BASE" -H "x-csrf-token: $_CSRF" -w '\n%{http_code}')
  [ -n "$b" ] && a+=(-H 'Content-Type: application/json' -d "$b")
  local out; out=$(curl "${a[@]}")
  FS_CODE="${out##*$'\n'}"; printf '%s' "$FS_CODE" > "$_CODEF"; printf '%s' "${out%$'\n'*}"
}
# jget KEY -> value of a top-level JSON key ('' if absent/unparsable)
jget() { KEY="$1" python3 -c 'import sys,json,os
try: print(json.load(sys.stdin).get(os.environ["KEY"],"") or "")
except Exception: print("")'; }
ok()   { echo "$1 PASS";  return 0; }
bad()  { echo "$1 FAIL: $2"; SMOKE_RC=1; return 1; }
# A check whose subject is absent is not a pass and not a failure: say so, loudly,
# rather than passing vacuously or failing a stand that is simply configured
# differently.
skip() { echo "$1 SKIP: $2"; return 0; }
SMOKE_RC=0
RUNID="${RUNID:-$(date +%s)}"

# Build a throwaway scan bundle: mkbundle <dir> <extension_id> <artifact_prefix> [with_descriptor]
mkbundle() {
  local d="$1" eid="$2" pfx="$3" desc="${4:-yes}"
  rm -rf "$d"; mkdir -p "$d/artifacts/workflows" "$d/artifacts/tools" "$d/ui/pages" "$d/ui/components"
  cat > "$d/main.py" <<'PY'
import json,sys
json.loads(sys.stdin.read() or "{}") if sys.stdin else None
print(json.dumps({"ok": True, "result": {}, "errors": []}))
PY
  printf 'import json\nprint(json.dumps({"ok": True}))\n' > "$d/health.py"
  printf "version: '1'\npages:\n- ref: pages/overview.yaml\n" > "$d/ui/ui_manifest.yaml"
  printf 'page_id: overview\ntitle: Overview\ndescription: smoke.\ncomponents:\n- ref: ../components/overview.yaml\n' > "$d/ui/pages/overview.yaml"
  printf 'component_id: %s_overview\ntype: markdown\ntitle: Smoke\ncontent: smoke fixture.\n' "$pfx" > "$d/ui/components/overview.yaml"
  cat > "$d/extension.yaml" <<EOF
manifest_version: 2
lifecycle: {data_schema_version: 1, data_migrations: []}
extension_id: $eid
kind: tool_provider
version: 1.0.0
display_name: Smoke $pfx
description: Release smoke fixture. Safe to delete.
vendor: {name: Smoke, support_url: 'https://example.invalid/s', website_url: 'https://example.invalid'}
listing: {category: developer-tools, tags: [smoke], summary: Release smoke fixture.}
features: [tool, artifact_bundle]
required_scopes: [extension:invoke]
schemas:
  input: {type: object, properties: {action: {type: string}}, required: [action]}
  output: {type: object, properties: {ok: {type: boolean}}, required: [ok]}
account_scope: {scope_type: project, description: smoke}
entrypoint: {mode: subprocess, command: [python3, main.py]}
health: {command: [python3, health.py], timeout_seconds: 5}
ui_manifest: ui/ui_manifest.yaml
EOF
  cat > "$d/artifacts/tools/${pfx}_tool.yaml" <<EOF
tool_id: ${pfx}_tool
version: 1.0.0
display_name: Smoke Tool
description: smoke
enabled: true
category: demo
prompt: smoke
implementation: {kind: extension, type: extension, extension_key: $eid, action: invoke_tool}
interface:
  functions:
  - name: smoke_echo
    returns: {type: object, required: [echoed], properties: {echoed: {type: string}}}
    parameters: [{name: value, type: string, required: true}]
tags: [smoke]
EOF
  cat > "$d/artifacts/workflows/${pfx}_workflow.yaml" <<EOF
workflow_id: ${pfx}_workflow
version: 1.0.0
display_name: 'Smoke: $pfx'
description: smoke
enabled: true
trigger: {confidence_threshold: 0.0}
settings:
  callable:
    enabled: true
    trigger_channels: [ui, api, mcp]
    input_mode: fields
    strict_inputs: false
    expected_inputs: []
    output_mode: variables
    expected_outputs: [{name: weather_raw, type: json, required: true, description: raw}]
    allow_empty_inputs: true
  execution_graph:
    schema_version: 1
    entry: {kind: step, id: ${pfx}_http}
    edges:
    - source: {kind: step, id: ${pfx}_http}
      outcome: {name: success}
      target: {kind: step, id: ${pfx}_done}
      rework: false
builder_metadata_jsonb:
  purpose: smoke
  use_cases: [smoke]
  anti_use_cases: []
  composition_role: utility_workflow
  determinism_level: deterministic
  safe_for_auto_use: true
tags: [smoke]
steps:
- step_id: ${pfx}_http
  display_name: Call
  step_kind: http_request
  override:
    http_request:
      url: https://api.open-meteo.com/v1/forecast
      method: GET
      auth_mode: none
      query_params: {latitude: '54.69', longitude: '25.28', current: temperature_2m}
      response_target: state.weather_raw
      timeout_seconds: 20
      expected_statuses: [200]
- step_id: ${pfx}_done
  display_name: Finish
  step_kind: stop_success
  override: {stop_success: {message: done}}
groups: []
for_each: []
EOF
  if [ "$desc" = "yes" ]; then
    cat > "$d/extension-descriptor.yaml" <<EOF
schema_version: extension_descriptor_v1
lifecycle: {data_schema_version: 1, data_migrations: []}
extension_id: $eid
release_version: 1.0.0
extension_type: service_extension
identity: {display_name: Smoke $pfx, description: Release smoke fixture., tags: [smoke]}
vendor: {vendor_name: Smoke, support_url: 'https://example.invalid/s'}
listing: {visibility: public_reference, category: developer_examples}
distribution:
  channel: private_byo
  execution_modes: [local]
  allows_self_hosted: true
  requires_connected_control_plane: false
  import_sources: [file]
runtime: {contract_version: extension_host_v2, compatibility: {platform_min: 0.1.0}}
artifacts:
  workflows: [{workflow_id: ${pfx}_workflow, ref: artifacts/workflows/${pfx}_workflow.yaml, publish: true}]
  tools: [{tool_id: ${pfx}_tool, ref: artifacts/tools/${pfx}_tool.yaml, publish: true}]
provenance: {origin: imported}
EOF
  fi
}

# uninstall_ext <extension_id> -> prints HTTP code of the confirm call
uninstall_ext() {
  local eid="$1" rev
  rev=$(fs GET "/api/extensions/$eid/uninstall-plan" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("revision",""))
except Exception: print("")')
  [ -n "$rev" ] || { echo ""; return 1; }
  fs POST "/api/extensions/$eid/uninstall" "{\"revision\":\"$rev\"}" >/dev/null
  echo "$FS_CODE"
}
