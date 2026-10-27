#!/usr/bin/env bash
# regD smoke suite - D01..D09. Usage: ./smoke_D.sh [D01 D03 ...]   (no args = all)
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/smoke_common.sh"

if [ "${FS_SMOKE_ISOLATED:-0}" != 1 ] || [ -z "${FS_SMOKE_PG_CONTAINER:-}" ]; then
  TESTS=("$@"); [ ${#TESTS[@]} -eq 0 ] && TESTS=(D01 D02 D03 D04 D05 D06 D07 D08 D09 D10 D11)
  for check_id in "${TESTS[@]}"; do
    printf '%s SKIP isolated audit data plane and FS_SMOKE_PG_CONTAINER are required\n' "$check_id"
  done
  exit 0
fi

# Trash a project fixture and purge it through its hard-delete lifecycle plan; the
# direct project hard-delete endpoint refuses projects.
purge_project() {
  local pid="$1" version
  fs DELETE "/api/projects/$pid" >/dev/null
  version=$(fs POST "/api/trash/project/$pid/plan" '{"action_type":"hard_delete"}' | jget plan_version)
  [ -n "$version" ] || return 1
  fs POST "/api/trash/project/$pid/execute" "{\"action_type\":\"hard_delete\",\"plan_version\":\"$version\"}" >/dev/null
}

D01() { # directory-scanned bundle ships its declared artifacts
  local d="$EXT_ROOT/smoked_scan"
  mkbundle "$d" "smoked.scan" "smoked_s" yes
  fs POST /api/extensions/scan '{}' >/dev/null
  local st n
  st=$(PSQL "select state from extension_installs where extension_id='smoked.scan'")
  n=$(PSQL "select count(*) from extension_registry_projections where extension_id='smoked.scan' and is_active")
  uninstall_ext smoked.scan >/dev/null; rm -rf "$d"; fs POST /api/extensions/scan '{}' >/dev/null
  [ "$st" = "active" ] && [ "$n" = "2" ] && ok D01 || bad D01 "state=$st projections=$n (want active/2)"
}

D02() { # descriptor-imported extension survives scan reconcile (proxy for restart)
  # The bundle must live OUTSIDE the scanned extensions root, or scan would pick
  # it up as a directory bundle and the check would prove nothing. It previously
  # used $REPO_ROOT/tmp with a hardcoded container path of /app/tmp, which only
  # resolves when the whole repository is mounted at /app — true for the
  # source-mounted dev stack, false for a packaged deployment that mounts only
  # the extensions directory. Copy it into the app container instead, so the
  # check works on any topology.
  local app="${FS_SMOKE_APP_CONTAINER:-}"
  if [ -z "$app" ]; then
    bad D02 "FS_SMOKE_APP_CONTAINER is required to place a descriptor bundle outside the extensions root"
    return
  fi
  local d="${TMPDIR:-/tmp}/smoked_desc.$$"
  mkbundle "$d" "smoked.desc" "smoked_d" yes
  docker exec "$app" rm -rf /tmp/smoked_desc >/dev/null 2>&1
  docker cp "$d" "$app:/tmp/smoked_desc" >/dev/null 2>&1
  fs POST /api/extensions/descriptors/import/file \
     "{\"descriptor_path\":\"/tmp/smoked_desc/extension-descriptor.yaml\",\"install_after_import\":true,\"runtime_mode\":\"local\"}" >/dev/null
  local before after
  before=$(PSQL "select state from extension_installs where extension_id='smoked.desc'")
  fs POST /api/extensions/scan '{}' >/dev/null; fs POST /api/extensions/scan '{}' >/dev/null
  after=$(PSQL "select state from extension_installs where extension_id='smoked.desc'")
  uninstall_ext smoked.desc >/dev/null
  rm -rf "$d"; docker exec "$app" rm -rf /tmp/smoked_desc >/dev/null 2>&1
  [ "$before" = "active" ] && [ "$after" = "active" ] && ok D02 \
    || bad D02 "state before=$before after=$after (self-disabled by scan reconcile)"
}

D03() { # uninstall refuses while a required dependent is active
  local a="$EXT_ROOT/smoked_base" b="$EXT_ROOT/smoked_dep"
  mkbundle "$a" "smoked.base" "smoked_b" yes
  mkbundle "$b" "smoked.dep"  "smoked_p" no
  cat >> "$b/extension.yaml" <<'EOF'
dependencies:
  extensions:
  - extension_id: smoked.base
    min_version: 1.0.0
    max_version_exclusive: 2.0.0
    required: true
    reason: smoke dependency guard
EOF
  fs POST /api/extensions/scan '{}' >/dev/null
  fs POST /api/extensions/smoked.base/enable '{}' >/dev/null
  fs POST /api/extensions/smoked.dep/enable  '{}' >/dev/null
  local code; code=$(uninstall_ext smoked.base)
  fs POST /api/extensions/smoked.dep/disable '{}' >/dev/null
  uninstall_ext smoked.dep >/dev/null; uninstall_ext smoked.base >/dev/null
  rm -rf "$a" "$b"; fs POST /api/extensions/scan '{}' >/dev/null
  [ "$code" = "400" ] && ok D03 || bad D03 "uninstall with active required dependent returned HTTP $code (want 400)"
}

D04() { # uninstall must not delete the bundle directory from the working tree
  local d="$EXT_ROOT/smoked_keep"
  mkbundle "$d" "smoked.keep" "smoked_k" yes
  fs POST /api/extensions/scan '{}' >/dev/null
  local before after code
  before=$(find "$d" -type f | wc -l | tr -d ' ')
  code=$(uninstall_ext smoked.keep)
  after=$(find "$d" -type f 2>/dev/null | wc -l | tr -d ' ')
  rm -rf "$d"; fs POST /api/extensions/scan '{}' >/dev/null
  [ "$code" = "200" ] && [ "$before" = "$after" ] && [ "$after" != "0" ] && ok D04 \
    || bad D04 "http=$code files before=$before after=$after (uninstall destroyed the bundle dir)"
}

D05() { # extension-projected workflow reaches completed and publishes its declared effect
  local d="$EXT_ROOT/smoked_run" ws pid wf jid st detail effect deadline cleanup_ok=1
  mkbundle "$d" "smoked.run" "smoked_r" yes
  fs POST /api/extensions/scan '{}' >/dev/null
  ws=$(fs GET /api/workspaces | python3 -c 'import sys,json;print([w["id"] for w in json.load(sys.stdin)["workspaces"] if w.get("workspace_key")=="default"][0])')
  wf=$(fs GET '/api/workflows?search=smoked_r_workflow' | python3 -c 'import sys,json
d=json.load(sys.stdin); ws=d if isinstance(d,list) else d.get("workflows",[])
print(ws[0]["workflow_id"] if ws else "")')
  pid=$(fs POST /api/projects "{\"name\":\"smokeD-proj-$RUNID\",\"workspace_id\":\"$ws\",\"workflow_ids\":[\"$wf\"]}" | jget project_id)
  jid=$(fs POST /api/workflow-runs "{\"project_id\":\"$pid\",\"workflow_id\":\"$wf\",\"inputs\":{}}" | jget job_id)
  st=""; detail=""; deadline=$(( $(date +%s) + ${FS_SMOKE_JOB_TIMEOUT_SECONDS:-120} ))
  while :; do
    detail=$(fs GET "/api/jobs/$jid")
    st=$(printf '%s' "$detail" | jget status)
    case "$st" in
      completed|failed|cancelled|max_iterations|preflight_failed|retryable_interrupted_step) break ;;
    esac
    [ "$(date +%s)" -lt "$deadline" ] || break
    sleep "${FS_SMOKE_POLL_SECONDS:-2}"
  done
  effect=$(printf '%s' "$detail" | python3 -c '
import json,sys
# The workflow declares weather_raw as an expected_output, so the effect is
# "published" when it reaches the declared output contract — the same place the
# public run result is built from. Reading a bare top-level state key instead
# asserted an undeclared channel that the checkpoint does not persist.
try: d=json.load(sys.stdin)
except Exception: print("missing"); raise SystemExit
d=d if isinstance(d,dict) else {}
def _val(obj,*path):
    for key in path:
        if not isinstance(obj,dict): return None
        obj=obj.get(key)
    return obj
# workflow_run.result is the public run result a caller consumes.
found=(_val(d,"workflow_run","result","outputs","weather_raw")
       or _val(d,"thread_state","workflow_outputs","outputs","weather_raw")
       or _val(d,"thread_state","outputs","weather_raw"))
print("present" if found not in (None, "", {}, []) else "missing")
')
  local why=""
  [ "$st" = "failed" ] && why=$(fs GET "/api/jobs/$jid/errors" | head -c 400)
  case "$st" in
    completed|failed|cancelled|max_iterations|preflight_failed|retryable_interrupted_step) ;;
    *) [ -n "$jid" ] && fs POST "/api/jobs/$jid/cancel" '{"reason":"smoke-timeout"}' >/dev/null ;;
  esac
  purge_project "$pid" || cleanup_ok=0
  assert_project_queues_removed "$pid" || cleanup_ok=0
  uninstall_ext smoked.run >/dev/null; rm -rf "$d"; fs POST /api/extensions/scan '{}' >/dev/null
  [ "$st" = completed ] && [ "$effect" = present ] && [ "$cleanup_ok" = 1 ] \
    && ok D05 \
    || bad D05 "wf=$wf job=$jid status=$st declared_effect=$effect ${why:+reason=$why}"
}

D06() { # creating a project with a freshly installed extension workflow returns 201, not 500
  local d="$EXT_ROOT/smoked_proj" ws wf pid code cleanup_ok=1
  mkbundle "$d" "smoked.proj" "smoked_j" yes
  fs POST /api/extensions/scan '{}' >/dev/null
  ws=$(fs GET /api/workspaces | python3 -c 'import sys,json;print([w["id"] for w in json.load(sys.stdin)["workspaces"] if w.get("workspace_key")=="default"][0])')
  wf=$(fs GET '/api/workflows?search=smoked_j_workflow' | python3 -c 'import sys,json
d=json.load(sys.stdin); ws=d if isinstance(d,list) else d.get("workflows",[])
print(ws[0]["workflow_id"] if ws else "")')
  pid=$(fs POST /api/projects "{\"name\":\"smokeD-proj2-$RUNID\",\"workspace_id\":\"$ws\",\"workflow_ids\":[\"$wf\"]}" | jget project_id); code="$(last_code)"
  local linked; linked=$(PSQL "select count(*) from project_workflows where project_id='$pid' and workflow_id='$wf'")
  if [ -n "$pid" ]; then
    purge_project "$pid" || cleanup_ok=0
    assert_project_queues_removed "$pid" || cleanup_ok=0
  fi
  uninstall_ext smoked.proj >/dev/null; rm -rf "$d"; fs POST /api/extensions/scan '{}' >/dev/null
  [ "$code" = "201" ] && [ "$linked" = "1" ] && [ "$cleanup_ok" = 1 ] \
    && ok D06 || bad D06 "http=$code linked=$linked cleanup=$cleanup_ok wf=$wf"
}

D07() { # worker extension inbound polling thread starts and its store contract is intact
  local W="${FS_SMOKE_WORKER_CONTAINER:-${WORKER_CONTAINER:-}}"
  [ -n "$W" ] || { bad D07 "FS_SMOKE_WORKER_CONTAINER is required"; return; }
  local started crashed contract
  started=$(docker logs "$W" 2>&1 | grep -c "Started extension inbound poll loop")
  crashed=$(docker logs "$W" 2>&1 | grep -cE "inbound poll (crashed|scan failed)|object has no attribute 'get_extension'")
  contract=$(docker exec "$W" python3 -c "
from core.infrastructure.config.runtime_config import CONFIG
from core.infrastructure.db.platform_stores import PlatformInboundPollingStore, resolve_observability_dsn
b=PlatformInboundPollingStore(resolve_observability_dsn(str(CONFIG.get('fs_observability_dsn') or CONFIG.get('fs_db_dsn') or '')))
print(all(hasattr(b,m) for m in ('get_extension','get_projects','list_project_connections','get_provider_secret')) and bool(b.get_projects() is not None))
" 2>/dev/null | tr -d '\r')
  [ "$started" -ge 1 ] && [ "$crashed" = "0" ] && [ "$contract" = "True" ] && ok D07 \
    || bad D07 "worker=$W started=$started crash_lines=$crashed store_contract=$contract"
}

D08() { # shipped bundles resolve to a real in-place dir and their uninstall plan is a safe disable (READ-ONLY)
  # No bundle is named here. The subject is whichever extension the stand actually
  # has installed: naming one makes the smoke depend on that bundle shipping, and
  # the previous default was a `release_stage: placeholder` bundle, which is not a
  # thing a candidate smoke should be asserting on at all.
  local eid plan rem code struct
  eid="${SHIPPED_EXT:-$(fs GET "/api/extensions" | python3 -c 'import sys, json
rows = json.load(sys.stdin)
rows = rows if isinstance(rows, list) else rows.get("extensions") or []
print(next((str(r.get("extension_id") or "") for r in rows if r.get("install_id")), ""))' 2>/dev/null)}"
  if [ -z "$eid" ]; then
    skip D08 "no extension is installed on this stand"
    return
  fi
  plan=$(fs GET "/api/extensions/$eid/uninstall-plan"); code="$(last_code)"
  rem=$(printf '%s' "$plan" | python3 -c 'import sys,json
p=json.load(sys.stdin)
print("disable" if any("Disable system extension" in str(r.get("display_name","")) for r in p.get("remove",[])) else "remove")' 2>/dev/null)
  [ -n "${FS_SMOKE_APP_CONTAINER:-}" ] || { bad D08 "FS_SMOKE_APP_CONTAINER is required"; return; }
  struct=$(docker exec "$FS_SMOKE_APP_CONTAINER" python3 -c "
from core.application.extensions.managed_release_service import ManagedReleaseService as M
from core.infrastructure.extension_runtime.extension_runtime import extension_origin_metadata
r=M._managed_simple_extension_root('$eid')
m=extension_origin_metadata({'extension_id':'$eid'})
print(r.is_dir() and not r.is_symlink() and bool(m.get('is_system')))
" 2>/dev/null | tr -d '\r')
  [ "$code" = "200" ] && [ "$rem" = "disable" ] && [ "$struct" = "True" ] && ok D08 \
    || bad D08 "plan_http=$code plan_action=$rem shipped_path_is_system_inplace_dir=$struct"
}

D09() { # extension projection lifecycle on the synthetic probe; no bootstrap or repair
  local runner="$DIR/../candidate_semantic_checks.py" output
  output="$($runner D09 2>/dev/null || true)"
  if CHECK_ID=D09 CANDIDATE_COMMIT="${FS_SMOKE_CANDIDATE_COMMIT:-}" OUTPUT="$output" \
    python3 -c '
import json, os
payload = json.loads(os.environ["OUTPUT"])
assert payload.get("check_id") == os.environ["CHECK_ID"]
assert payload.get("candidate_commit") == os.environ["CANDIDATE_COMMIT"]
assert payload.get("passed") is True
assert payload.get("assertions") and all(row.get("passed") is True for row in payload["assertions"])
' >/dev/null 2>&1; then
    ok D09
  else
    bad D09 "candidate extension lifecycle evidence is invalid or incomplete"
  fi
}

D10() { # external-effect persistence and action scope on the synthetic probe
  local runner="$DIR/../candidate_semantic_checks.py" output
  output="$($runner D10 2>/dev/null || true)"
  if CHECK_ID=D10 CANDIDATE_COMMIT="${FS_SMOKE_CANDIDATE_COMMIT:-}" OUTPUT="$output" \
    python3 -c '
import json, os
payload = json.loads(os.environ["OUTPUT"])
assert payload.get("check_id") == os.environ["CHECK_ID"]
assert payload.get("candidate_commit") == os.environ["CANDIDATE_COMMIT"]
assert payload.get("passed") is True
assert payload.get("assertions") and all(row.get("passed") is True for row in payload["assertions"])
' >/dev/null 2>&1; then
    ok D10
  else
    bad D10 "candidate external-effect evidence is invalid or incomplete"
  fi
}

D11() { # authored workflow diagnostics, terminal routing, defaults, and tabular checkpoints
  local runner="$DIR/../candidate_semantic_checks.py" output
  output="$($runner D11 2>/dev/null || true)"
  if CHECK_ID=D11 CANDIDATE_COMMIT="${FS_SMOKE_CANDIDATE_COMMIT:-}" OUTPUT="$output" \
    python3 -c '
import json, os
payload = json.loads(os.environ["OUTPUT"])
assert payload.get("check_id") == os.environ["CHECK_ID"]
assert payload.get("candidate_commit") == os.environ["CANDIDATE_COMMIT"]
assert payload.get("passed") is True
assert payload.get("assertions") and all(row.get("passed") is True for row in payload["assertions"])
' >/dev/null 2>&1; then
    ok D11
  else
    bad D11 "candidate authored-workflow semantic evidence is invalid or incomplete"
  fi
}


TESTS=("$@"); [ ${#TESTS[@]} -eq 0 ] && TESTS=(D01 D02 D03 D04 D05 D06 D07 D08 D09 D10 D11)
for t in "${TESTS[@]}"; do "$t"; done
exit $SMOKE_RC
