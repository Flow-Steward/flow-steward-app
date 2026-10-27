#!/usr/bin/env bash
# Flow Steward — GROUP C (auth / tenancy / security) release smoke suite.
# Each check prints "C## PASS" or "C## FAIL"; the script exits non-zero if any failed.
#
#   ADMIN_EMAIL=... ADMIN_PASS=... ./smoke_groupC.sh          # runs everything
#   ./smoke_groupC.sh C04 C08                                 # runs a subset
#
# C04-C08 need a principal in a NON-default account; the suite provisions and
# tears one down automatically (needs docker access to the pg container).
set -uo pipefail

BASE=${BASE:-${FS_SMOKE_BASE:-https://localhost:8099}}
ACCT=${ACCT:-00000000-0000-0000-0000-000000000002}
# The release runner validates and supplies FS_SMOKE_EMAIL/FS_SMOKE_PASSWORD for
# groups B/C/D. Fall back to those before the standalone e2e defaults, otherwise
# every C check dies with "FATAL: admin login failed" on any installation that
# does not happen to have the e2e fixture user — which is every clean install.
ADMIN_EMAIL=${ADMIN_EMAIL:-${FS_SMOKE_EMAIL:-e2e_admin@example.com}}
ADMIN_PASS=${ADMIN_PASS:-${FS_SMOKE_PASSWORD:-e2e_admin_password_123!}}
PG=${FS_SMOKE_PG_CONTAINER:-${PG:-}}
MAILPIT=${FS_SMOKE_MAILPIT_BASE:-${MAILPIT:-}}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
FAILED=0

psql_() { docker exec "$PG" psql -U flow_steward -d flow_steward -tAc "$1"; }
say()  { printf '%-4s %-5s %s\n' "$1" "$2" "${3:-}"; [ "$2" = FAIL ] && FAILED=1; return 0; }
skip() { say "$1" SKIP "$2"; }

# ---- session helpers ---------------------------------------------------------
login() { # jar csrf_file email pass account
  local r; r=$(curl -sk -c "$1" -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
      -H "Origin: $BASE" -d "{\"email\":\"$3\",\"password\":\"$4\",\"account_id\":\"$5\"}")
  printf '%s' "$r" | sed -n 's/.*"csrf_token":"\([^"]*\)".*/\1/p' > "$2"
  [ -s "$2" ]
}
api() { # jar csrf method path [body] -> body + trailing __HTTP_nnn__
  local a=(-sk -b "$1" -c "$1" -X "$3" "$BASE$4" -H "Origin: $BASE" -H "x-csrf-token: $(cat "$2")" -w '__HTTP_%{http_code}__')
  [ -n "${5:-}" ] && a+=(-H 'Content-Type: application/json' -d "$5")
  curl "${a[@]}"
}
code() { printf '%s' "$1" | grep -o '__HTTP_[0-9]*__' | tr -dc '0-9'; }

# The password-reset throttle is 5 requests / 300s per client IP and lives in
# app memory (no table to clear), so C13-C15 report SKIP rather than a false
# FAIL when the quota is spent. Wait 15 min or restart fs-app to reset it.
reset_request() { # email -> http code
  curl -sk -o /dev/null -w '%{http_code}' -X POST "$BASE/api/auth/password-reset/request" \
    -H "Origin: $BASE" ${2:+-H "Host: $2"} -H 'Content-Type: application/json' -d "{\"email\":\"$1\"}"
}

WANT="$*"
want() { [ -z "${WANT// /}" ] && return 0; case " $WANT " in *" $1 "*) return 0;; esac; return 1; }
if [ "${FS_SMOKE_ISOLATED:-0}" != 1 ] || [ -z "$PG" ]; then
  for id in C01 C02 C03 C04 C05 C06 C07 C08 C09 C10 C11 C12 C13 C14 C15; do
    want "$id" && say "$id" SKIP "isolated audit data plane and FS_SMOKE_PG_CONTAINER are required"
  done
  exit 0
fi
if { want C14 || want C15; } && [ -z "$MAILPIT" ]; then
  want C14 && say C14 FAIL "FS_SMOKE_MAILPIT_BASE is required"
  want C15 && say C15 FAIL "FS_SMOKE_MAILPIT_BASE is required"
  exit 1
fi

AJ=$TMP/admin.jar; AC=$TMP/admin.csrf
login "$AJ" "$AC" "$ADMIN_EMAIL" "$ADMIN_PASS" "$ACCT" || { echo "FATAL: admin login failed"; exit 2; }
A() { api "$AJ" "$AC" "$@"; }

# ---- second-account principal (C04-C08) -------------------------------------
TENANT_READY=0; TB_ACCT=""; TB_USER="regc-smoke-tenantb-$$@regc.test"; TB_PASS='regC_smoke_tenantB_123!'
BJ=$TMP/b.jar; BC=$TMP/b.csrf
B() { api "$BJ" "$BC" "$@"; }
tenant_setup() {
  local r; r=$(A POST /api/auth/accounts "{\"account_id\":\"regc-smoke-b-$$\",\"display_name\":\"regC smoke tenant B\",\"is_active\":true}")
  TB_ACCT=$(printf '%s' "$r" | sed -n 's/.*"account_id":"\([^"]*\)".*/\1/p' | head -1)
  [ -n "$TB_ACCT" ] || return 1
  A POST /api/auth/users "{\"email\":\"$TB_USER\",\"password\":\"$TB_PASS\",\"display_name\":\"regC smoke tenant B\",\"roles\":[\"admin\"],\"is_active\":true,\"account_ids\":[\"$ACCT\"],\"primary_account_id\":\"$ACCT\"}" >/dev/null
  local uid; uid=$(psql_ "select id from users where username='$TB_USER';" | tr -d ' ')
  [ -n "$uid" ] || return 1
  psql_ "delete from user_account_memberships where user_id='$uid';
         insert into user_account_memberships (user_id,account_id,is_default) values ('$uid','$TB_ACCT',true);
         update users set account_id='$TB_ACCT' where id='$uid';" >/dev/null
  login "$BJ" "$BC" "$TB_USER" "$TB_PASS" "$TB_ACCT" && TENANT_READY=1
}
tenant_teardown() {
  [ -n "$TB_ACCT" ] || return 0
  psql_ "update users set is_active=false where username='$TB_USER';" >/dev/null
  A POST "/api/auth/accounts/$TB_ACCT/disable" '{}' >/dev/null
}

# =============================================================================
# C01 — auth audit trail writes rows for login / logout / password events
# =============================================================================
c01() {
  local before after j cs
  before=$(psql_ "select count(*) from platform_action_log where action_type like 'auth_%';")
  j=$TMP/c01.jar; cs=$TMP/c01.csrf
  login "$j" "$cs" "$ADMIN_EMAIL" "$ADMIN_PASS" "$ACCT" >/dev/null
  curl -sk -o /dev/null -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' -H "Origin: $BASE" \
    -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"deliberately-wrong\",\"account_id\":\"$ACCT\"}"
  api "$j" "$cs" POST /api/auth/logout '' >/dev/null
  psql_ "delete from auth_rate_limits where key='user:$ADMIN_EMAIL' or key like 'ip:%';" >/dev/null
  sleep 1
  after=$(psql_ "select count(*) from platform_action_log where action_type like 'auth_%';")
  local ok_login rej_login ok_logout
  ok_login=$(psql_  "select count(*) from platform_action_log where action_type='auth_login'  and status='ok'       and timestamp > now()-interval '2 min';")
  rej_login=$(psql_ "select count(*) from platform_action_log where action_type='auth_login'  and status='rejected' and timestamp > now()-interval '2 min';")
  ok_logout=$(psql_ "select count(*) from platform_action_log where action_type='auth_logout' and timestamp > now()-interval '2 min';")
  if [ "$after" -gt "$before" ] && [ "$ok_login" -ge 1 ] && [ "$rej_login" -ge 1 ] && [ "$ok_logout" -ge 1 ]; then
    say C01 PASS "auth rows $before -> $after (login ok/rejected/logout all present)"
  else
    say C01 FAIL "auth rows $before -> $after  ok_login=$ok_login rejected=$rej_login logout=$ok_logout"
  fi
}

# =============================================================================
# C02 — login rate limiter locks the abused USERNAME, not every user on the IP
#       NOTE: destructive to auth_rate_limits; it clears the table before+after.
# =============================================================================
c02() {
  local spray="regc-smoke-spray-$$@regc.test" i c
  psql_ "DELETE FROM auth_rate_limits;" >/dev/null
  for i in 1 2 3 4 5 6; do
    curl -sk -o /dev/null -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' -H "Origin: $BASE" \
      -d "{\"email\":\"$spray\",\"password\":\"wrong-$i\",\"account_id\":\"$ACCT\"}"
  done
  local locked; locked=$(psql_ "select count(*) from auth_rate_limits where key like 'ip:%' and locked_until > now();")
  c=$(curl -sk -o /dev/null -w '%{http_code}' -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
        -H "Origin: $BASE" -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASS\",\"account_id\":\"$ACCT\"}")
  psql_ "DELETE FROM auth_rate_limits;" >/dev/null
  [ "$locked" -ge 1 ] && [ "$c" = 200 ] \
    && say C02 PASS "limiter created $locked active lock(s); unrelated valid login stayed HTTP 200" \
    || say C02 FAIL "limiter locks=$locked; unrelated valid login HTTP $c (expected >=1 / 200)"
}

# =============================================================================
# C03 — user admin endpoints accept UUIDs (PUT / unlock / force-password-reset)
# =============================================================================
c03() {
  local em="regc-smoke-c03-$$@regc.test" r uid p u f
  r=$(A POST /api/auth/users "{\"email\":\"$em\",\"password\":\"regC_smoke_123!\",\"roles\":[\"viewer\"],\"is_active\":true,\"account_ids\":[\"$ACCT\"],\"primary_account_id\":\"$ACCT\"}")
  uid=$(printf '%s' "$r" | sed -n 's/.*"id":"\([0-9a-f-]\{36\}\)".*/\1/p' | head -1)
  [ -n "$uid" ] || { say C03 FAIL "could not create probe user: $(printf '%s' "$r" | head -c 120)"; return; }
  p=$(code "$(A PUT  "/api/auth/users/$uid" '{"display_name":"regC smoke renamed"}')")
  u=$(code "$(A POST "/api/auth/users/$uid/unlock" '{}')")
  f=$(code "$(A POST "/api/auth/users/$uid/force-password-reset" '{}')")
  A PUT "/api/auth/users/$uid" '{"is_active":false}' >/dev/null
  [ "$p" = 200 ] && [ "$u" = 200 ] && [ "$f" = 200 ] \
    && say C03 PASS "PUT=$p unlock=$u force-reset=$f" \
    || say C03 FAIL "PUT=$p unlock=$u force-reset=$f (expected 200/200/200)"
}

# =============================================================================
# C04 — guardrails are account-scoped
# =============================================================================
c04() {
  [ "$TENANT_READY" = 1 ] || { say C04 FAIL "second-account principal unavailable"; return; }
  local seen mine rc
  seen=$(B GET '/api/guardrails?per_page=200' | python3 -c '
import sys,json
d=json.loads(sys.stdin.read().split("__HTTP_")[0] or "[]")
print(len(d if isinstance(d,list) else d.get("guardrails",[])))' 2>/dev/null || echo ERR)
  rc=$(code "$(B POST /api/guardrails '{"display_name":"regC-smoke-cross","enabled":true,"mode":"deterministic","rule_type":"forbidden_patterns","severity":"error","config":{"check_in":"diff","patterns":["regC"]},"agents":["developer"]}')")
  # clean up anything the probe managed to create
  mine=$(psql_ "select id from registry_guardrails where display_name='regC-smoke-cross';" | tr -d ' ')
  [ -n "$mine" ] && { A DELETE "/api/guardrails/$mine" >/dev/null; A POST "/api/trash/registry_guardrail/$mine/hard-delete" '{}' >/dev/null; }
  if [ "$seen" = 0 ] && [ "$rc" != 200 ] && [ "$rc" != 201 ]; then
    say C04 PASS "cross-account read=0 rows, create HTTP $rc"
  else
    say C04 FAIL "second-account principal saw $seen guardrails and create returned HTTP $rc (expected 0 / 403)"
  fi
}

# =============================================================================
# C05 — registry_fields / registry_data_contracts are account-scoped
# =============================================================================
c05() {
  [ "$TENANT_READY" = 1 ] || { say C05 FAIL "second-account principal unavailable"; return; }
  local f d; f=$(code "$(B GET '/api/fields?per_page=50')"); d=$(code "$(B GET '/api/data-contracts?per_page=50')")
  { [ "$f" = 403 ] || [ "$f" = 404 ]; } && { [ "$d" = 403 ] || [ "$d" = 404 ]; } \
    && say C05 PASS "fields=$f data-contracts=$d" \
    || say C05 FAIL "fields=$f data-contracts=$d (expected 403)"
}

# =============================================================================
# C06 — /api/trash and /api/agents are account-scoped
# =============================================================================
c06() {
  [ "$TENANT_READY" = 1 ] || { say C06 FAIL "second-account principal unavailable"; return; }
  local t ag n
  t=$(code "$(B GET '/api/trash?per_page=20')")
  ag=$(B GET '/api/agents?per_page=200'); n=$(printf '%s' "$ag" | grep -o '"agent_id"' | wc -l | tr -d ' ')
  { [ "$t" = 403 ] || [ "$t" = 404 ]; } && [ "$n" = 0 ] \
    && say C06 PASS "trash=$t agents=$n rows" \
    || say C06 FAIL "trash=$t agents=$n rows (expected 403 / 0)"
}

# =============================================================================
# C07 — cross-account project creation into another account's workspace refused
# =============================================================================
c07() {
  [ "$TENANT_READY" = 1 ] || { say C07 FAIL "second-account principal unavailable"; return; }
  local ws rc pid del
  ws=$(psql_ "select id from workspaces where account_id='$ACCT' and deleted_at is null limit 1;" | tr -d ' ')
  local r; r=$(B POST /api/projects "{\"name\":\"regC-smoke-cross-$$\",\"workspace_id\":\"$ws\"}")
  rc=$(code "$r")
  pid=$(printf '%s' "$r" | sed -n 's/.*"project_id":"\([0-9a-f-]\{36\}\)".*/\1/p' | head -1)
  [ -n "$pid" ] && A DELETE "/api/projects/$pid" >/dev/null
  { [ "$rc" = 403 ] || [ "$rc" = 404 ]; } \
    && say C07 PASS "create into foreign workspace -> HTTP $rc" \
    || say C07 FAIL "create into foreign workspace -> HTTP $rc (expected 403/404)"
}

# =============================================================================
# C08 — a second account does not see another account's extension workflows
# =============================================================================
c08() {
  [ "$TENANT_READY" = 1 ] || { say C08 FAIL "second-account principal unavailable"; return; }
  local n orphans
  n=$(B GET '/api/workflows?per_page=200' | python3 -c '
import sys,json
d=json.loads(sys.stdin.read().split("__HTTP_")[0] or "[]")
print(len(d if isinstance(d,list) else d.get("workflows",[])))' 2>/dev/null || echo ERR)
  orphans=$(psql_ "select count(*) from registry_workflows where account_id is null and deleted_at is null;")
  [ "$n" = 0 ] \
    && say C08 PASS "second account sees 0 workflows" \
    || say C08 FAIL "second account sees $n workflows ($orphans registry_workflows rows have account_id IS NULL)"
}

# =============================================================================
# C09 — viewer cannot mutate; CSRF enforced on cross-origin mutations
# =============================================================================
c09() {
  local em="regc-smoke-c09-$$@regc.test" uid vj=$TMP/v.jar vc=$TMP/v.csrf r ws
  ws=$(psql_ "select id from workspaces where account_id='$ACCT' and deleted_at is null limit 1;" | tr -d ' ')
  r=$(A POST /api/auth/users "{\"email\":\"$em\",\"password\":\"regC_smoke_123!\",\"roles\":[\"viewer\"],\"is_active\":true,\"account_ids\":[\"$ACCT\"],\"primary_account_id\":\"$ACCT\"}")
  uid=$(printf '%s' "$r" | sed -n 's/.*"id":"\([0-9a-f-]\{36\}\)".*/\1/p' | head -1)
  login "$vj" "$vc" "$em" 'regC_smoke_123!' "$ACCT" >/dev/null
  local vp vg csrf_evil csrf_good
  vp=$(code "$(api "$vj" "$vc" POST /api/projects "{\"name\":\"regC-smoke-v-$$\",\"workspace_id\":\"$ws\"}")")
  vg=$(code "$(api "$vj" "$vc" POST /api/guardrails '{"display_name":"regC-smoke-v","enabled":true,"mode":"deterministic","rule_type":"forbidden_patterns","severity":"error","config":{"check_in":"diff","patterns":["x"]},"agents":["developer"]}')")
  # cross-origin POST with no CSRF token must be refused
  csrf_evil=$(curl -sk -o /dev/null -w '%{http_code}' -b "$AJ" -X POST "$BASE/api/projects" \
      -H 'Origin: https://evil.attacker.example' -H 'Content-Type: application/json' \
      -d "{\"name\":\"regC-smoke-csrf-$$\",\"workspace_id\":\"$ws\"}")
  [ -n "$uid" ] && A PUT "/api/auth/users/$uid" '{"is_active":false}' >/dev/null
  psql_ "delete from projects where name like 'regC-smoke-%';" >/dev/null
  [ "$vp" = 403 ] && [ "$vg" = 403 ] && [ "$csrf_evil" = 403 ] \
    && say C09 PASS "viewer project=$vp guardrail=$vg ; cross-origin no-token=$csrf_evil" \
    || say C09 FAIL "viewer project=$vp guardrail=$vg cross-origin=$csrf_evil (expected 403/403/403)"
}

# =============================================================================
# C10 — NUL byte in a text field returns a typed 422, never a bare 500
# =============================================================================
c10() {
  local ws out worst=0 label rc
  ws=$(psql_ "select id from workspaces where account_id='$ACCT' and deleted_at is null limit 1;" | tr -d ' ')
  out=$(BASE="$BASE" ACCT="$ACCT" WS="$ws" EMAIL="$ADMIN_EMAIL" PASS="$ADMIN_PASS" python3 - <<'PY'
import http.cookiejar, json, os, ssl, sys, urllib.request, urllib.error
B=os.environ["BASE"]; NUL=chr(0)
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
op=urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                               urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def call(m,p,body=None,csrf=""):
    d=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(B+p,data=d,method=m); r.add_header("Origin",B)
    if d: r.add_header("Content-Type","application/json")
    if csrf: r.add_header("x-csrf-token",csrf)
    try:
        with op.open(r,timeout=20) as x: return x.status
    except urllib.error.HTTPError as e: return e.code
    except Exception: return 0
st=call("POST","/api/auth/login",{"email":os.environ["EMAIL"],"password":os.environ["PASS"],
                                  "account_id":os.environ["ACCT"]})
if st!=200: print("LOGIN",st); sys.exit(3)
# re-login to capture csrf
import urllib.request as u2
req=u2.Request(B+"/api/auth/login",data=json.dumps({"email":os.environ["EMAIL"],
     "password":os.environ["PASS"],"account_id":os.environ["ACCT"]}).encode(),method="POST")
req.add_header("Origin",B); req.add_header("Content-Type","application/json")
csrf=json.loads(op.open(req,timeout=20).read())["csrf_token"]
ws=os.environ["WS"]
cases=[("POST","/api/projects",{"name":"regC-smoke"+NUL,"workspace_id":ws}),
       ("POST","/api/guardrails",{"display_name":"regC"+NUL,"enabled":True,"mode":"deterministic",
        "rule_type":"forbidden_patterns","severity":"error",
        "config":{"check_in":"diff","patterns":["x"]},"agents":["developer"]}),
       ("POST","/api/workspaces",{"workspace_key":"regc-smoke-nul","display_name":"regC"+NUL})]
codes=[call(m,p,b,csrf) for m,p,b in cases]
print(" ".join(str(c) for c in codes))
sys.exit(1 if any(c>=500 or c==0 for c in codes) else 0)
PY
)
  worst=$?
  [ "$worst" = 0 ] && say C10 PASS "NUL rejected with $out (all 4xx)" \
                   || say C10 FAIL "NUL produced $out (5xx / transport error)"
}

# =============================================================================
# C11 — invalid UUID in a path -> 4xx not 500; absurd page numbers don't crash
# =============================================================================
c11() {
  local p bad=0 detail="" c
  for p in /api/projects/not-a-uuid /api/jobs/not-a-uuid /api/workspaces/not-a-uuid \
           /api/agents/not-a-uuid /api/guardrails/not-a-uuid /api/workflows/not-a-uuid; do
    c=$(code "$(A GET "$p")")
    case "$c" in
      4??) ;;
      000|0|"") bad=1; detail="$detail $p=transport failure" ;;
      *) bad=1; detail="$detail $p invalid identifier unexpectedly returned HTTP $c" ;;
    esac
  done
  for p in '/api/projects?page=999999999' '/api/projects?page=99999999999999999999' \
           '/api/projects?page=-1' '/api/projects?per_page=0' '/api/jobs?page=999999999' \
           '/api/auth/users?page=999999999'; do
    c=$(code "$(A GET "$p")")
    case "$c" in
      2??|4??) ;;
      000|0|"") bad=1; detail="$detail $p=transport failure" ;;
      *) bad=1; detail="$detail $p=HTTP$c" ;;
    esac
  done
  [ "$bad" = 0 ] && say C11 PASS "malformed ids are 4xx and extreme pagination is bounded" \
                 || say C11 FAIL "invalid response:$detail"
}

# =============================================================================
# C12 — no password hashes / raw secrets in API responses
# =============================================================================
c12() {
  local p bad=0 detail="" pid
  pid=$(psql_ "select id from projects where deleted_at is null limit 1;" | tr -d ' ')
  for p in /api/auth/me '/api/auth/users?per_page=200' /api/auth/tokens /api/auth/accounts \
           /api/auth/service-accounts /api/model-providers /api/llm-provider-extensions \
           /api/providers /api/extensions /api/workspaces "/api/projects/$pid" \
           "/api/projects/$pid/connections" "/api/projects/$pid/providers" \
           '/api/guardrails?per_page=200' '/api/agents?per_page=200' '/api/trash?per_page=100'; do
    local hit; hit=$(A GET "$p" | grep -Eo '"password_hash"|"token_hash"|\$2[aby]\$[0-9]{2}\$|pbkdf2[:$]|argon2[id]?\$|BEGIN [A-Z ]*PRIVATE KEY|sk-[A-Za-z0-9]{20,}|"client_secret"[[:space:]]*:[[:space:]]*"[^"]{4,}"' | sort -u | head -2 | tr '\n' ',')
    [ -n "$hit" ] && { bad=1; detail="$detail $p($hit)"; }
  done
  [ "$bad" = 0 ] && say C12 PASS "16 endpoints clean of hashes/secrets" \
                 || say C12 FAIL "secret material found in:$detail"
}

# =============================================================================
# C13 — password-reset delivery failure is recorded, not silently swallowed
#       Uses an address the mailer rejects (".." in the local part) so no SMTP
#       breakage is needed.
# =============================================================================
c13() {
  local em="regc..smoke$$@regc.test" uid ok_row fail_row warn
  local r; r=$(A POST /api/auth/users "{\"email\":\"$em\",\"password\":\"regC_smoke_123!\",\"roles\":[\"viewer\"],\"is_active\":true,\"account_ids\":[\"$ACCT\"],\"primary_account_id\":\"$ACCT\"}")
  uid=$(printf '%s' "$r" | sed -n 's/.*"id":"\([0-9a-f-]\{36\}\)".*/\1/p' | head -1)
  warn=$(printf '%s' "$r" | grep -c 'not delivered')
  local rc; rc=$(reset_request "$em")
  if [ "$rc" = 429 ]; then
    [ -n "$uid" ] && A PUT "/api/auth/users/$uid" '{"is_active":false}' >/dev/null
    skip C13 "password-reset throttle spent for this IP (HTTP 429) — retry in 15 min"; return
  fi
  sleep 1
  fail_row=$(psql_ "select count(*) from platform_action_log where action_type='auth_password_reset_request' and result::text like '%invalid_recipient%' and timestamp > now()-interval '2 min';")
  [ -n "$uid" ] && A PUT "/api/auth/users/$uid" '{"is_active":false}' >/dev/null
  psql_ "delete from auth_rate_limits;" >/dev/null
  [ "$fail_row" -ge 1 ] && [ "$warn" -ge 1 ] \
    && say C13 PASS "audit row records email_delivery=invalid_recipient; user-create returns a delivery warning" \
    || say C13 FAIL "delivery failure not recorded (audit rows=$fail_row, create warning=$warn)"
}

# =============================================================================
# C14 — /api/auth/password-reset/verify must not disclose the account email
# =============================================================================
# Reset links are emailed through an email extension; without one there is no email to read.
system_email_off() {
  curl -sk "$BASE/api/app/bootstrap" -H "Origin: $BASE" 2>/dev/null \
    | python3 -c 'import json,sys; sys.exit(0 if not (json.load(sys.stdin).get("auth") or {}).get("system_email_available") else 1)' 2>/dev/null
}

c14() {
  if system_email_off; then skip C14 "no email extension is connected, so no reset email is sent"; return; fi
  local em="regc-smoke-c14-$$@regc.test" uid tok body
  local r; r=$(A POST /api/auth/users "{\"email\":\"$em\",\"password\":\"regC_smoke_123!\",\"roles\":[\"viewer\"],\"is_active\":true,\"account_ids\":[\"$ACCT\"],\"primary_account_id\":\"$ACCT\"}")
  uid=$(printf '%s' "$r" | sed -n 's/.*"id":"\([0-9a-f-]\{36\}\)".*/\1/p' | head -1)
  local rc; rc=$(reset_request "$em")
  if [ "$rc" = 429 ]; then
    [ -n "$uid" ] && A PUT "/api/auth/users/$uid" '{"is_active":false}' >/dev/null
    skip C14 "password-reset throttle spent for this IP (HTTP 429) — retry in 15 min"; return
  fi
  sleep 2
  tok=$(curl -s "$MAILPIT/api/v1/search?query=to%3A$em" 2>/dev/null | python3 -c '
import sys,json,re,urllib.request
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
for m in d.get("messages",[]):
    f=json.load(urllib.request.urlopen("'"$MAILPIT"'/api/v1/message/"+m["ID"]))
    t=(f.get("Text") or "")+" "+(f.get("HTML") or "")
    g=re.search(r"reset-password\?token=([A-Za-z0-9_.-]+)",t)
    if g: print(g.group(1)); break
')
  # NOTE: verify BEFORE deactivating — an inactive user makes the token invalid,
  # which would mask the disclosure and produce a false PASS.
  if [ -z "$tok" ]; then
    [ -n "$uid" ] && A PUT "/api/auth/users/$uid" '{"is_active":false}' >/dev/null
    say C14 FAIL "could not obtain a reset token (mailpit unreachable?)"; return
  fi
  body=$(curl -sk -X POST "$BASE/api/auth/password-reset/verify" -H "Origin: $BASE" \
         -H 'Content-Type: application/json' -d "{\"token\":\"$tok\"}")
  [ -n "$uid" ] && A PUT "/api/auth/users/$uid" '{"is_active":false}' >/dev/null
  psql_ "delete from auth_rate_limits;" >/dev/null
  printf '%s' "$body" | grep -q "$em" \
    && say C14 FAIL "verify echoes the account address: $(printf '%s' "$body" | head -c 120)" \
    || say C14 PASS "verify response carries no address"
}

# =============================================================================
# C15 — verification links are built from configured base URL, not Host header
# =============================================================================
c15() {
  if system_email_off; then skip C15 "no email extension is connected, so no reset email is sent"; return; fi
  local em="regc-smoke-c15-$$@regc.test" uid link
  local r; r=$(A POST /api/auth/users "{\"email\":\"$em\",\"password\":\"regC_smoke_123!\",\"roles\":[\"viewer\"],\"is_active\":true,\"account_ids\":[\"$ACCT\"],\"primary_account_id\":\"$ACCT\"}")
  uid=$(printf '%s' "$r" | sed -n 's/.*"id":"\([0-9a-f-]\{36\}\)".*/\1/p' | head -1)
  local rc; rc=$(reset_request "$em" evil.attacker.example)
  if [ "$rc" = 429 ]; then
    [ -n "$uid" ] && A PUT "/api/auth/users/$uid" '{"is_active":false}' >/dev/null
    skip C15 "password-reset throttle spent for this IP (HTTP 429) — retry in 15 min"; return
  fi
  sleep 2
  link=$(curl -s "$MAILPIT/api/v1/search?query=to%3A$em" 2>/dev/null | python3 -c '
import sys,json,re,urllib.request
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
for m in d.get("messages",[]):
    f=json.load(urllib.request.urlopen("'"$MAILPIT"'/api/v1/message/"+m["ID"]))
    t=(f.get("Text") or "")+" "+(f.get("HTML") or "")
    g=re.search(r"https?://[^\s\"<>]*reset-password[^\s\"<>]*",t)
    if g: print(g.group(0)); break
')
  [ -n "$uid" ] && A PUT "/api/auth/users/$uid" '{"is_active":false}' >/dev/null
  psql_ "delete from auth_rate_limits;" >/dev/null
  [ -n "$link" ] || { say C15 FAIL "no reset link produced (mailpit unreachable?)"; return; }
  LINK="$link" EXPECTED_ORIGIN="${FS_PUBLIC_BASE_URL:-$BASE}" python3 - <<'PY' >/dev/null 2>&1
import os
from urllib.parse import parse_qs, urlparse

actual = urlparse(os.environ["LINK"])
expected = urlparse(os.environ["EXPECTED_ORIGIN"])
assert (actual.scheme, actual.hostname, actual.port) == (
    expected.scheme,
    expected.hostname,
    expected.port,
)
assert actual.path.endswith("/reset-password")
assert any(parse_qs(actual.query).values())
PY
  [ "$?" -eq 0 ] \
    && say C15 PASS "reset link matches candidate origin: ${link%%\?*}" \
    || say C15 FAIL "reset link does not match FS_PUBLIC_BASE_URL: ${link%%\?*}"
}

# ---- run --------------------------------------------------------------------
needs_tenant=0
for id in C04 C05 C06 C07 C08; do want "$id" && needs_tenant=1; done
[ "$needs_tenant" = 1 ] && { tenant_setup || echo "WARN: tenant-B provisioning failed"; }

for id in C01 C02 C03 C04 C05 C06 C07 C08 C09 C10 C11 C12 C13 C14 C15; do
  want "$id" && "$(printf '%s' "$id" | tr 'A-Z' 'a-z')"
done

[ "$needs_tenant" = 1 ] && tenant_teardown
psql_ "delete from auth_rate_limits;" >/dev/null
echo "---"
[ "$FAILED" = 0 ] && echo "GROUP C: ALL PASS" || echo "GROUP C: FAILURES PRESENT"
exit $FAILED
