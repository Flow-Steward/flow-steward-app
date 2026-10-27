#!/usr/bin/env bash
# ---- shared preamble (every B## snippet assumes these) ----
DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
FS="${FS:-$DIR/../fsapi.sh}"
PSQL(){ docker exec "$FS_SMOKE_PG_CONTAINER" psql -U "${FS_SMOKE_PG_USER:-flow_steward}" -d "${FS_SMOKE_PG_DATABASE:-flow_steward}" -tAc "$1"; }
WSD="${FS_SMOKE_WORKSPACE:-00000000-0000-0000-0000-000000000003}"
api(){ "$FS" "$@" 2>/dev/null; }
hc(){ grep -o '__HTTP_[0-9]*__' <<<"$1" | tail -1 | tr -cd '0-9'; }
jq_(){ python3 -c "import sys,json;d=json.loads(sys.stdin.read().split('__HTTP_')[0]);print($1)"; }
T=regb$RANDOM

response_body(){ printf '%s' "${1%%__HTTP_*}"; }
response_field(){
  local field="$2"
  response_body "$1" | FS_RESPONSE_FIELD="$field" python3 -c '
import json, os, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit
value = payload.get(os.environ["FS_RESPONSE_FIELD"], "") if isinstance(payload, dict) else ""
print(value if isinstance(value, (str, int, float, bool)) else "")
'
}

cleanup_project(){
  local project_id="$1"
  local check_id="${2:-smoke}"
  local trash_path trash_response trash_status trash_state
  local plan_path execute_path max_attempts retry_delay attempt
  local plan_response plan_status plan_version
  local delete_response delete_status delete_state delete_code
  local absent_path absent_response absent_status
  [ -n "$project_id" ] || {
    echo "$check_id FAIL (cleanup project id is empty)" >&2
    return 1
  }

  trash_path="/api/trash/project/$project_id/trash"
  trash_response=$(api POST "$trash_path" '{}')
  trash_status=$(hc "$trash_response")
  trash_state=$(response_field "$trash_response" status)
  if [ "$trash_status" != 200 ] || [ "$trash_state" != trashed ]; then
    echo "$check_id FAIL (cleanup $trash_path HTTP $trash_status body=$(response_body "$trash_response"); retained project=$project_id)" >&2
    return 1
  fi

  # A trashed project is purged through its lifecycle plan: the direct
  # hard-delete endpoint refuses projects, and execute names the plan revision.
  plan_path="/api/trash/project/$project_id/plan"
  execute_path="/api/trash/project/$project_id/execute"
  max_attempts="${FS_SMOKE_CLEANUP_MAX_ATTEMPTS:-31}"
  retry_delay="${FS_SMOKE_CLEANUP_RETRY_DELAY_SECONDS:-1}"
  attempt=1
  while [ "$attempt" -le "$max_attempts" ]; do
    plan_response=$(api POST "$plan_path" '{"action_type":"hard_delete"}')
    plan_status=$(hc "$plan_response")
    plan_version=$(response_field "$plan_response" plan_version)
    if [ "$plan_status" != 200 ] || [ -z "$plan_version" ]; then
      echo "$check_id FAIL (cleanup $plan_path HTTP $plan_status body=$(response_body "$plan_response"); retained project=$project_id)" >&2
      return 1
    fi
    delete_response=$(api POST "$execute_path" "{\"action_type\":\"hard_delete\",\"plan_version\":\"$plan_version\"}")
    delete_status=$(hc "$delete_response")
    delete_state=$(response_field "$delete_response" status)
    if [ "$delete_status" = 200 ] && [ "$delete_state" = hard_deleted ]; then
      break
    fi
    delete_code=$(response_field "$delete_response" code)
    if [ "$delete_status" != 409 ] || [ "$delete_code" != project_executors_not_quiesced ] || [ "$attempt" -ge "$max_attempts" ]; then
      echo "$check_id FAIL (cleanup $execute_path HTTP $delete_status body=$(response_body "$delete_response"); retained project=$project_id)" >&2
      return 1
    fi
    sleep "$retry_delay"
    attempt=$((attempt + 1))
  done

  absent_path="/api/projects/$project_id"
  absent_response=$(api GET "$absent_path")
  absent_status=$(hc "$absent_response")
  if [ "$absent_status" != 404 ]; then
    echo "$check_id FAIL (cleanup verification $absent_path HTTP $absent_status body=$(response_body "$absent_response"); retained project=$project_id)" >&2
    return 1
  fi
}

b01_job_is_terminal(){
  case "$1" in
    completed|failed|cancelled|max_iterations|retryable_interrupted_step) return 0 ;;
    *) return 1 ;;
  esac
}

b01_observe_and_settle_job(){
  local job_id="$1"
  local project_id="$2"
  local observation_attempts observation_delay attempt
  local cancel_path cancel_response cancel_status
  local settle_attempts settle_delay settle_path settle_response settle_status

  B01_JOB_STATUS=pending
  B01_WORKER_ID=""
  observation_attempts="${FS_SMOKE_B01_OBSERVATION_ATTEMPTS:-30}"
  observation_delay="${FS_SMOKE_B01_OBSERVATION_DELAY_SECONDS:-4}"
  attempt=1
  while [ "$attempt" -le "$observation_attempts" ]; do
    B01_JOB_STATUS=$(PSQL "select status from jobs where id='$job_id';")
    B01_WORKER_ID=$(PSQL "select coalesce(worker_id,'') from jobs where id='$job_id';")
    if b01_job_is_terminal "$B01_JOB_STATUS"; then
      return 0
    fi
    if [ "$attempt" -lt "$observation_attempts" ]; then
      sleep "$observation_delay"
    fi
    attempt=$((attempt + 1))
  done

  cancel_path="/api/jobs/$job_id/cancel"
  cancel_response=$(api POST "$cancel_path" '{"reason":"release smoke B01 observation deadline"}')
  cancel_status=$(hc "$cancel_response")
  if [ "$cancel_status" != 200 ] && [ "$cancel_status" != 409 ]; then
    echo "B01 FAIL (settlement $cancel_path HTTP $cancel_status body=$(response_body "$cancel_response"); retained job=$job_id project=$project_id)" >&2
    return 1
  fi

  settle_attempts="${FS_SMOKE_B01_SETTLE_ATTEMPTS:-31}"
  settle_delay="${FS_SMOKE_B01_SETTLE_DELAY_SECONDS:-1}"
  settle_path="/api/jobs/$job_id"
  settle_response=""
  settle_status=""
  attempt=1
  while [ "$attempt" -le "$settle_attempts" ]; do
    settle_response=$(api GET "$settle_path")
    settle_status=$(hc "$settle_response")
    if [ "$settle_status" = 200 ]; then
      B01_JOB_STATUS=$(response_field "$settle_response" status)
      if b01_job_is_terminal "$B01_JOB_STATUS"; then
        return 0
      fi
    fi
    if [ "$attempt" -lt "$settle_attempts" ]; then
      sleep "$settle_delay"
    fi
    attempt=$((attempt + 1))
  done

  echo "B01 FAIL (settlement $settle_path HTTP $settle_status body=$(response_body "$settle_response"); retained job=$job_id project=$project_id)" >&2
  return 1
}

# A dataset connector id embeds the install id of the extension that provides it,
# so it differs on every installation. Discover it instead of hardcoding one
# machine's value, otherwise these checks can only ever pass on that machine.
db_connector(){
  api GET /api/datasets/connectors | python3 -c "
import sys,json
raw=sys.stdin.read().split('__HTTP_')[0]
try: d=json.loads(raw)
except Exception: print(''); raise SystemExit
rows=d if isinstance(d,list) else (d.get('connectors') or d.get('items') or [])
for row in rows:
    cid=str((row or {}).get('connector_id') or '')
    if cid.endswith('.toolbox_database'): print(cid); break
else: print('')
"
}

# ---------- B01 : a project completes a dispatched workflow task ----------
b01(){
 WF="${SMOKE_WF:-$(api GET '/api/workflows?per_page=500&enabled_only=true' | jq_ 'next((w.get("workflow_id") or w.get("id") or "" for w in d.get("workflows",[])), "")')}"
 PROVIDER="${FS_SMOKE_MODEL_PROVIDER:-}"
 MODEL="${FS_SMOKE_MODEL:-}"
 if [ -z "$WF" ]; then
   echo "B01 FAIL (no enabled workflow is available; set SMOKE_WF)"
   return
 fi
 if [ -z "$PROVIDER" ]; then
   echo "B01 FAIL (FS_SMOKE_MODEL_PROVIDER is required for a runnable workflow)"
   return
 fi
 PROJECT_MODEL_DEFAULTS=""
 if [ -z "$MODEL" ]; then
   echo "B01 FAIL (FS_SMOKE_MODEL is required for a chat provider)"
   return
 fi
 case "$PROVIDER" in
       custom:*)
         PROVIDER_ID="${PROVIDER#custom:}"
         ROUTE_READY=$(api GET /api/model-providers | \
           FS_PROVIDER_ID="$PROVIDER_ID" FS_PROVIDER_MODEL="$MODEL" \
           FS_PROVIDER_ACCOUNT="${FS_SMOKE_ACCOUNT:-00000000-0000-0000-0000-000000000002}" \
           python3 -c '
import json, os, sys
raw = sys.stdin.read().split("__HTTP_")[0]
try:
    rows = json.loads(raw).get("providers", [])
except Exception:
    print("invalid")
    raise SystemExit
for row in rows:
    if str(row.get("id") or "") != os.environ["FS_PROVIDER_ID"]:
        continue
    ok = (
        str(row.get("account_id") or "") == os.environ["FS_PROVIDER_ACCOUNT"]
        and str(row.get("default_model") or "") == os.environ["FS_PROVIDER_MODEL"]
        and str(row.get("verification_status") or "") == "passed"
    )
    print("ready" if ok else "mismatch")
    break
else:
    print("missing")
')
         if [ "$ROUTE_READY" != ready ]; then
           echo "B01 FAIL (account-scoped provider route/model is not ready: $ROUTE_READY)"
           return
         fi
         ;;
 esac
 PROJECT_MODEL_DEFAULTS=",\"default_provider\":\"$PROVIDER\",\"default_model\":\"$MODEL\""
 p=$(api POST /api/projects "{\"name\":\"$T-run\",\"workspace_id\":\"$WSD\",\"workflow_ids\":[\"$WF\"]$PROJECT_MODEL_DEFAULTS}" | jq_ 'd["project_id"]')
 J="{\"project_id\":\"$p\",\"workflow_id\":\"$WF\",\"inputs\":{}}"
 job=$(api POST /api/workflow-runs "$J" | jq_ 'd.get("job_id","")')
 st=pending; wk=""
 if [ -n "$job" ]; then
   b01_observe_and_settle_job "$job" "$p" || return
   st="$B01_JOB_STATUS"
   wk="$B01_WORKER_ID"
 fi
 err=$(PSQL "select left(coalesce(last_error,''),90) from jobs where id='$job';")
 cleanup_project "$p" B01 || return
 if [ -z "$job" ]; then echo "B01 FAIL (POST /api/workflow-runs returned no job_id)"
 elif [ -z "$wk" ]; then echo "B01 FAIL (job $job failed before worker attribution; status=$st)${err:+ [error: $err]}"
 elif [ "$st" = completed ]; then echo "B01 PASS (task completed through worker $wk)"
 else echo "B01 FAIL (job $job was consumed by $wk but terminal status=$st, expected completed)${err:+ [worker error: $err]}"; fi
}

# ---------- B03 : duplicate project name -> 409 ----------
b03(){
 J="{\"name\":\"$T-dup\",\"workspace_id\":\"$WSD\"}"
 p=$(api POST /api/projects "$J" | jq_ 'd["project_id"]')
 c=$(hc "$(api POST /api/projects "$J")")
 cleanup_project "$p" B03 || return
 [ "$c" = 409 ] && echo "B03 PASS" || echo "B03 FAIL (duplicate name -> HTTP $c, expected 409)"
}

# ---------- B04 : PUT /api/projects/{id} is fast ----------
b04(){
 J="{\"name\":\"$T-put\",\"workspace_id\":\"$WSD\"}"
 p=$(api POST /api/projects "$J" | jq_ 'd["project_id"]')
 U="{\"name\":\"$T-put\",\"description\":\"smoke\"}"
 s=$(date +%s%N); out=$(api PUT "/api/projects/$p" "$U"); e=$(date +%s%N); ms=$(( (e-s)/1000000 )); c=$(hc "$out")
 cleanup_project "$p" B04 || return
 if [ "$c" = 200 ] && [ "$ms" -lt 5000 ]; then
   echo "B04 PASS (HTTP 200, ${ms}ms)"
 else
   echo "B04 FAIL (PUT -> HTTP $c in ${ms}ms, expected HTTP 200 in <5000ms)"
 fi
}

# ---------- B05 : GET /api/projects lists non-default workspaces ----------
b05(){
 W="{\"workspace_key\":\"$T-ws\",\"display_name\":\"$T ws\"}"
 w=$(api POST /api/workspaces "$W" | jq_ 'd["id"]')
 J="{\"name\":\"$T-otherws\",\"workspace_id\":\"$w\"}"
 pr=$(api POST /api/projects "$J" | jq_ 'd["project_id"]')
 n=$(api GET '/api/projects?per_page=500' | jq_ "sum(1 for x in d['projects'] if x['name']=='$T-otherws')")
 cleanup_project "$pr" B05 || return
 api POST "/api/trash/workspace/$w/delete-execute" '{"project_actions":[]}' >/dev/null
 api POST "/api/trash/workspace/$w/hard-delete" '{}' >/dev/null
 [ "$n" = 1 ] && echo "B05 PASS" || echo "B05 FAIL (project in a non-default workspace missing from GET /api/projects)"
}

# ---------- B06 : partial workspace PUT preserves workspace_key ----------
b06(){
 W="{\"workspace_key\":\"$T-k\",\"display_name\":\"$T d\",\"description\":\"$T desc\"}"
 w=$(api POST /api/workspaces "$W" | jq_ 'd["id"]')
 api PUT "/api/workspaces/$w" '{"display_name":"renamed only"}' >/dev/null
 got=$(PSQL "select workspace_key||'|'||coalesce(description,'') from workspaces where id='$w';")
 api POST "/api/trash/workspace/$w/delete-execute" '{"project_actions":[]}' >/dev/null
 api POST "/api/trash/workspace/$w/hard-delete" '{}' >/dev/null
 [ "$got" = "$T-k|$T desc" ] && echo "B06 PASS" || echo "B06 FAIL (partial PUT rewrote key/description: '$got')"
}

# ---------- B07 : skill rename preserves the I/O contract ----------
b07(){
 sk="${T}_sk7"
 S="{\"skill_id\":\"$sk\",\"display_name\":\"v1\",\"description\":\"smoke\",\"category\":\"analysis\",\"skill_type\":\"prompt\",\"instructions\":[\"x\"],\"expected_inputs\":[{\"name\":\"x\",\"type\":\"text\",\"required\":true}],\"expected_outputs\":[{\"name\":\"y\",\"type\":\"text\",\"required\":true},{\"name\":\"z\",\"type\":\"json\",\"required\":false}]}"
 api POST /api/skills "$S" >/dev/null
 Q="select coalesce(output_schema::text,'')||'#'||coalesce(input_schema::text,'')||'#'||(select count(*) from registry_skill_outputs where skill_id='$sk')||'/'||(select count(*) from registry_skill_inputs where skill_id='$sk') from registry_skills where skill_id='$sk';"
 pre=$(PSQL "$Q")
 api PATCH "/api/skills/$sk" '{"display_name":"v2 renamed"}' >/dev/null
 api PUT   "/api/skills/$sk" '{"display_name":"v3 renamed"}' >/dev/null
 post=$(PSQL "$Q")
 api POST "/api/trash/registry_skill/$sk/trash" '{}' >/dev/null; api POST "/api/trash/registry_skill/$sk/hard-delete" '{}' >/dev/null
 { [ -n "$pre" ] && [ "$pre" = "$post" ]; } && echo "B07 PASS" || echo "B07 FAIL (rename changed the contract: before=$pre after=$post)"
}

# ---------- B08 : purging a skill used by a DISABLED agent must be guarded ----------
b08(){
 sk="${T}_sk8"; ag="${T}_ag8"
 S="{\"skill_id\":\"$sk\",\"display_name\":\"s8\",\"description\":\"smoke\",\"category\":\"analysis\",\"skill_type\":\"prompt\",\"instructions\":[\"x\"],\"expected_inputs\":[{\"name\":\"x\",\"type\":\"text\",\"required\":true}],\"expected_outputs\":[{\"name\":\"y\",\"type\":\"text\",\"required\":true}]}"
 A="{\"agent_id\":\"$ag\",\"display_name\":\"a8\",\"description\":\"smoke\",\"role_type\":\"analyst\",\"base_skill\":\"$sk\",\"enabled\":false,\"category\":\"analysis\"}"
 api POST /api/skills "$S" >/dev/null; api PATCH "/api/skills/$sk" '{"enabled":true}' >/dev/null
 api POST /api/agents "$A" >/dev/null
 api POST "/api/trash/registry_skill/$sk/trash" '{}' >/dev/null
 c=$(hc "$(api POST "/api/trash/registry_skill/$sk/hard-delete" '{}')")
 left=$(PSQL "select count(*) from registry_agent_skills where agent_id='$ag';")
 agent_trash=$(hc "$(api POST "/api/trash/registry_agent/$ag/trash" '{}')")
 agent_delete=$(hc "$(api POST "/api/trash/registry_agent/$ag/hard-delete" '{}')")
 skill_retrash=$(hc "$(api POST "/api/trash/registry_skill/$sk/trash" '{}')")
 skill_delete=$(hc "$(api POST "/api/trash/registry_skill/$sk/hard-delete" '{}')")
 remaining=$(PSQL "select count(*) from registry_skills where skill_id='$sk';")
 { { [ "$c" = 409 ] || [ "$left" != 0 ]; } \
   && [ "$agent_trash" = 200 ] && [ "$agent_delete" = 200 ] \
   && [ "$skill_retrash" = 200 ] && [ "$skill_delete" = 200 ] \
   && [ "$remaining" = 0 ]; } && echo "B08 PASS" \
   || echo "B08 FAIL (guard=$c links=$left cleanup agent=$agent_trash/$agent_delete skill=$skill_retrash/$skill_delete remaining=$remaining)"
}

# ---------- B09 : a purged workflow cannot be resurrected from history ----------
b09(){
 wf="${T}_wf9"
 api POST /api/workflows "{\"workflow_id\":\"$wf\",\"display_name\":\"w9\",\"description\":\"smoke\",\"enabled\":false}" >/dev/null
 eid=$(api GET "/api/changelog/workflow/$wf/history" | jq_ 'd[-1]["id"] if d else ""')
 api POST "/api/trash/registry_workflow/$wf/trash" '{}' >/dev/null
 api POST "/api/trash/registry_workflow/$wf/hard-delete" '{}' >/dev/null
 c=$(hc "$(api POST "/api/workflows/$wf/restore" "{\"entry_id\":\"$eid\"}")")
 n=$(PSQL "select count(*) from registry_workflows where workflow_key='$wf';")
 [ "$n" = 0 ] && echo "B09 PASS (restore -> $c)" || echo "B09 FAIL (purged workflow resurrected via history entry $eid)"
}

# ---------- B10 : workspace hard-delete must not orphan data connections ----------
# The connection config is complete on purpose (RS5-014): a postgres source now
# requires host, database and user, and a bare {prebuilt} is refused. These two
# checks are about workspace deletion, not about config validation - an
# incomplete fixture would fail them in setup and say nothing about their subject.
b10(){
 cn="$T-conn10"
 CONNECTOR=$(db_connector)
 [ -n "$CONNECTOR" ] || { echo "B10 FAIL (no *.toolbox_database dataset connector is available on this installation)"; return; }
 w=$(api POST /api/workspaces "{\"workspace_key\":\"$T-w10\",\"display_name\":\"$T w10\"}" | jq_ 'd["id"]')
 C="{\"connection_id\":\"$cn\",\"workspace_id\":\"$w\",\"connector_id\":\"$CONNECTOR\",\"display_name\":\"smoke\",\"connection_config_jsonb\":{\"prebuilt\":\"postgres\",\"host\":\"${FS_SMOKE_DB_HOST:?set FS_SMOKE_DB_HOST}\",\"database\":\"flow_steward\",\"user\":\"flow_steward\"}}"
 api POST /api/datasets/data-connections "$C" >/dev/null
 api POST "/api/trash/workspace/$w/delete-execute" "{\"project_actions\":[],\"connection_actions\":[{\"connection_id\":\"$cn\",\"action\":\"delete\"}]}" >/dev/null
 c=$(hc "$(api POST "/api/trash/workspace/$w/hard-delete" '{}')")
 orph=$(PSQL "select count(*) from workspace_data_connections c where c.connection_id='$cn' and not exists (select 1 from workspaces w where w.id=c.workspace_id);")
 remaining=$(PSQL "select count(*) from workspace_data_connections where connection_id='$cn';")
 api DELETE "/api/datasets/data-connections/$cn" >/dev/null
 PSQL "delete from workspace_data_connections where connection_id='$cn';" >/dev/null
 api POST "/api/trash/workspace/$w/hard-delete" '{}' >/dev/null
 { [ "$c" = 200 ] && [ "$orph" = 0 ] && [ "$remaining" = 0 ]; } && echo "B10 PASS" \
   || echo "B10 FAIL (hard-delete -> HTTP $c, orphaned connections=$orph, remaining connections=$remaining)"
}

# ---------- B11 : workspace delete-plan counts the data connections that exist ----------
b11(){
 cn="$T-conn11"
 CONNECTOR=$(db_connector)
 [ -n "$CONNECTOR" ] || { echo "B11 FAIL (no *.toolbox_database dataset connector is available on this installation)"; return; }
 w=$(api POST /api/workspaces "{\"workspace_key\":\"$T-w11\",\"display_name\":\"$T w11\"}" | jq_ 'd["id"]')
 C="{\"connection_id\":\"$cn\",\"workspace_id\":\"$w\",\"connector_id\":\"$CONNECTOR\",\"display_name\":\"smoke\",\"connection_config_jsonb\":{\"prebuilt\":\"postgres\",\"host\":\"${FS_SMOKE_DB_HOST:?set FS_SMOKE_DB_HOST}\",\"database\":\"flow_steward\",\"user\":\"flow_steward\"}}"
 api POST /api/datasets/data-connections "$C" >/dev/null
 n=$(api POST "/api/trash/workspace/$w/delete-plan" '{}' | jq_ 'd["summary"]["data_connection_count"]')
 api DELETE "/api/datasets/data-connections/$cn" >/dev/null
 PSQL "delete from workspace_data_connections where connection_id='$cn';" >/dev/null
 api POST "/api/trash/workspace/$w/delete-execute" '{"project_actions":[]}' >/dev/null
 api POST "/api/trash/workspace/$w/hard-delete" '{}' >/dev/null
 [ "$n" = 1 ] && echo "B11 PASS" || echo "B11 FAIL (delete-plan reports data_connection_count=$n while 1 connection is live)"
}

# ---------- B12 : guardrails + data contracts trash / restore / purge ----------
b12(){
 G="{\"display_name\":\"$T-gr\",\"description\":\"smoke\",\"enabled\":true,\"mode\":\"deterministic\",\"rule_type\":\"forbidden_patterns\",\"severity\":\"error\",\"config\":{\"check_in\":\"diff\",\"patterns\":[\"$T\"]},\"agents\":[\"developer\"]}"
 g=$(api POST /api/guardrails "$G" | jq_ 'd["id"]')
 ct="$T.contract.v1"
 D="{\"contract_id\":\"$ct\",\"version\":\"1.0.0\",\"display_name\":\"smoke\",\"kind\":\"json\",\"schema_json\":{\"type\":\"object\"},\"enabled\":true}"
 api POST /api/data-contracts "$D" >/dev/null
 ok=1; detail=""
 for e in "registry_guardrail/$g" "registry_data_contract/$ct"; do
   for step in trash restore trash hard-delete; do
     c=$(hc "$(api POST "/api/trash/$e/$step" '{}')")
     [ "$c" = 200 ] || { ok=0; detail="$detail $e/$step=$c"; }
   done
 done
 [ "$ok" = 1 ] && echo "B12 PASS" || echo "B12 FAIL (lifecycle broke:$detail)"
}

# ---------- B13 : revision history + restore, restore logged as a new entry ----------
b13(){
 p=$(api POST /api/projects "{\"name\":\"$T-h\",\"workspace_id\":\"$WSD\"}" | jq_ 'd["project_id"]')
 api PUT "/api/projects/$p" "{\"name\":\"$T-h\",\"description\":\"v2\"}" >/dev/null
 rev=$(PSQL "select id from project_settings_revisions where project_id='$p' and action='create' limit 1;")
 api POST "/api/projects/$p/history/$rev/restore" '{}' >/dev/null
 last=$(PSQL "select action||'|'||author from project_settings_revisions where project_id='$p' order by created_at desc limit 1;")
 desc=$(PSQL "select coalesce(description,'') from projects where id='$p';")
 cleanup_project "$p" B13 || return
 { [ "${last%%|*}" = restore ] && [ -z "$desc" ]; } && echo "B13 PASS ($last)" \
   || echo "B13 FAIL (last revision='$last', description after restore='$desc')"
}

# ---------- B14 : trash and restore write an audit row ----------
b14(){
 sk="${T}_sk14"
 S="{\"skill_id\":\"$sk\",\"display_name\":\"s14\",\"description\":\"smoke\",\"category\":\"analysis\",\"skill_type\":\"prompt\",\"instructions\":[\"x\"],\"expected_inputs\":[{\"name\":\"x\",\"type\":\"text\",\"required\":true}],\"expected_outputs\":[{\"name\":\"y\",\"type\":\"text\",\"required\":true}]}"
 api POST /api/skills "$S" >/dev/null
 n(){ PSQL "select count(*) from registry_changelog where entity_id='$sk';"; }
 a=$(n); api POST "/api/trash/registry_skill/$sk/trash"   '{}' >/dev/null; b=$(n)
        api POST "/api/trash/registry_skill/$sk/restore" '{}' >/dev/null; c=$(n)
 api POST "/api/trash/registry_skill/$sk/trash" '{}' >/dev/null; api POST "/api/trash/registry_skill/$sk/hard-delete" '{}' >/dev/null
 { [ "$b" -gt "$a" ] && [ "$c" -gt "$b" ]; } && echo "B14 PASS" \
   || echo "B14 FAIL (changelog rows create=$a afterTrash=$b afterRestore=$c — lifecycle not audited)"
}

# ---------- B15 : registry changelog records the real actor (incl. guardrails) ----------
b15(){
 G="{\"display_name\":\"$T-gr15\",\"description\":\"smoke\",\"enabled\":true,\"mode\":\"deterministic\",\"rule_type\":\"forbidden_patterns\",\"severity\":\"error\",\"config\":{\"check_in\":\"diff\",\"patterns\":[\"$T\"]},\"agents\":[\"developer\"]}"
 g=$(api POST /api/guardrails "$G" | jq_ 'd["id"]')
 au=$(PSQL "select author from registry_changelog where entity_id='$g' and action='create' limit 1;")
 api POST "/api/trash/registry_guardrail/$g/trash" '{}' >/dev/null; api POST "/api/trash/registry_guardrail/$g/hard-delete" '{}' >/dev/null
 case "$au" in ''|admin|system|admin_restore) echo "B15 FAIL (guardrail changelog author='$au', not the authenticated user)";; *) echo "B15 PASS ($au)";; esac
}

# ---------- B16 : GET /api/tools?search= keeps the unfiltered schema ----------
b16(){
 python3 - "$FS" <<'PY'
import json,subprocess,sys
fs=sys.argv[1]
def get(p):
    d=json.loads(subprocess.run([fs,'GET',p],capture_output=True,text=True).stdout.split('__HTTP_')[0])
    if isinstance(d,dict):
        k=[x for x in d if isinstance(d[x],list)]; d=d[k[0]] if k else []
    return d
a=get('/api/tools?per_page=200'); s=get('/api/tools?per_page=200&search=kb')
if not a or not s: print('B16 FAIL (empty result set)'); raise SystemExit
miss=sorted(set(a[0])-set(s[0])); nullid=[r for r in s if not r.get('tool_id')]
print('B16 PASS' if not miss and not nullid
      else f'B16 FAIL ({len(miss)} fields dropped {miss}; tool_id null in {len(nullid)}/{len(s)} rows)')
PY
}

# ---------- B17 : GET /api/workflows?search= schema + enabled_only ----------
b17(){
 python3 - "$FS" <<'PY'
import json,subprocess,sys
fs=sys.argv[1]
def get(p):
    d=json.loads(subprocess.run([fs,'GET',p],capture_output=True,text=True).stdout.split('__HTTP_')[0])
    if isinstance(d,dict):
        k=[x for x in d if isinstance(d[x],list)]; d=d[k[0]] if k else []
    return d
a=get('/api/workflows?per_page=200')
if not a: print('B17 FAIL (no workflows)'); raise SystemExit
s=get('/api/workflows?per_page=200&search='+a[0]['workflow_key'][:6])
miss=sorted(set(a[0])-set(s[0])) if s else ['<search returned nothing>']
dis=[w['workflow_key'] for w in a if not w.get('enabled')]
bad=[w['workflow_key'] for w in get('/api/workflows?per_page=200&enabled_only=true&search='+dis[0])
     if not w.get('enabled')] if dis else []
print('B17 PASS' if not miss and not bad else f'B17 FAIL (schema drift {miss}; enabled_only ignored for {bad})')
PY
}

# ---------- B18 : project detail must not inline DB schema snapshots ----------
b18(){
 python3 - "$FS" <<'PY'
import json,subprocess,sys
fs=sys.argv[1]
ids=[p['id'] for p in json.loads(subprocess.run([fs,'GET','/api/projects?per_page=500'],
     capture_output=True,text=True).stdout.split('__HTTP_')[0])['projects']]
worst=(0,None,0)
for i in ids:
    raw=subprocess.run([fs,'GET',f'/api/projects/{i}'],capture_output=True,text=True).stdout.split('__HTTP_')[0]
    d=json.loads(raw)
    snap=sum(len(json.dumps(c.get('schema_snapshot') or {},separators=(',',':')))
             for c in (d.get('assigned_data_connections') or []))
    if len(raw)>worst[0]: worst=(len(raw),i,snap)
n,i,snap=worst
print('B18 PASS (largest project detail %d bytes)'%n if n<200000 and snap<50000
      else f'B18 FAIL (project {i} returns {n} bytes, {snap} of them an inlined schema_snapshot)')
PY
}

# ---------- B19 : project settings history records a real author + source ----------
b19(){
 p=$(api POST /api/projects "{\"name\":\"$T-19\",\"workspace_id\":\"$WSD\"}" | jq_ 'd["project_id"]')
 api PUT "/api/projects/$p" "{\"name\":\"$T-19\",\"description\":\"edit\"}" >/dev/null
 n=$(PSQL "select count(*) from project_settings_revisions where project_id='$p';")
 bad=$(PSQL "select count(*) from project_settings_revisions where project_id='$p' and (coalesce(author,'') in ('','admin','system') or coalesce(source,'')='');")
 cleanup_project "$p" B19 || return
 { [ "$n" -ge 2 ] && [ "$bad" = 0 ]; } && echo "B19 PASS" || echo "B19 FAIL ($n revisions, $bad with a placeholder author/source)"
}

for f in "$@"; do "$(printf '%s' "$f" | tr 'A-Z' 'a-z')"; done
