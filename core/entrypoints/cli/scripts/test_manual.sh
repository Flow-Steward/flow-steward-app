#!/usr/bin/env bash
# =============================================================================
# Flow Steward Orchestrator — Manual Test Script
# =============================================================================
#
# Usage:
#   # Run all checks
#   ./core/entrypoints/cli/scripts/test_manual.sh
#
#   # Run inside Docker
#   docker compose -f infra/docker/docker-compose.yaml exec fs-app \
#     bash /app/core/entrypoints/cli/scripts/test_manual.sh
#
#   # Full cycle smoke test
#   ./core/entrypoints/cli/scripts/test_manual.sh full-cycle PROJECT_ID WORKFLOW_ID '{"topic":"Weekly summary"}'
#
# Requirements:
#   - Flow Steward server running (http://localhost:8099)
#   - curl, jq installed
#   - Optional: wscat (npm install -g wscat) for WebSocket tests
#
# =============================================================================

set -euo pipefail

BASE_URL="${APP_BASE_URL:-http://localhost:8099}"
if [ -n "${WS_URL:-}" ]; then
    WS_BASE_URL="${WS_URL}"
elif [[ "${BASE_URL}" == https://* ]]; then
    WS_BASE_URL="wss://${BASE_URL#https://}"
elif [[ "${BASE_URL}" == http://* ]]; then
    WS_BASE_URL="ws://${BASE_URL#http://}"
else
    WS_BASE_URL="${BASE_URL}"
fi
PROJECT_ID="${TEST_PROJECT_BACKEND_ID:-}"
VERIFY_SSL="-k"  # Accept self-signed cert

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

pass() { echo -e "${GREEN}✓ PASS${NC} $1"; }
fail() { echo -e "${RED}✗ FAIL${NC} $1"; }
info() { echo -e "${BLUE}ℹ INFO${NC} $1"; }
warn() { echo -e "${YELLOW}⚠ WARN${NC} $1"; }

# =============================================================================
# 1. Registry Health Check
# =============================================================================
check_registry_health() {
    echo ""
    echo "=== 1. Registry Health Check ==="

    HEALTH=$(curl -sf ${VERIFY_SSL} "${BASE_URL}/health" 2>&1) || {
        fail "Server not reachable at ${BASE_URL}"
        return 1
    }
    pass "Server reachable: ${HEALTH}"

    # Check registry mode
    AGENTS=$(curl -sf ${VERIFY_SSL} "${BASE_URL}/api/agents" 2>&1)
    if echo "$AGENTS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d) if isinstance(d,list) else 'dict')" 2>/dev/null; then
        pass "Registry mode: ENABLED"
        AGENT_COUNT=$(echo "$AGENTS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d) if isinstance(d,list) else len(d.get('agents',[])))" 2>/dev/null || echo "?")
        info "Agents loaded: ${AGENT_COUNT}"
    else
        warn "Registry mode: disabled or API error"
    fi
}

# =============================================================================
# 2. Graph Compilation Check
# =============================================================================
check_graph_compilation() {
    echo ""
    echo "=== 2. Graph Compilation Check ==="

    RESULT=$(python3 -c "
import sys
sys.path.insert(0, '/app')
try:
    from graph.builder import build_graph_from_registry
    from registry.registry_manager import RegistryManager
    rm = RegistryManager.instance()
    g = build_graph_from_registry(rm)
    print('Registry graph: OK, nodes=' + str(len(list(g.nodes))))
except Exception as e:
    print('ERROR: ' + str(e))
    sys.exit(1)
" 2>&1) || {
        fail "Graph compilation: ${RESULT}"
        return 1
    }
    pass "Graph compilation: ${RESULT}"
}

# =============================================================================
# 5. Manual Job Submit + Monitor (curl)
# =============================================================================
submit_job() {
    local PROJECT_ID="$1"
    local WORKFLOW_ID="$2"
    local INPUTS_JSON="${3:-}"
    if [ -z "$INPUTS_JSON" ]; then INPUTS_JSON='{}'; fi

    echo ""
    echo "=== 6. Submit Job ==="
    info "Project: ${PROJECT_ID}"
    info "Workflow: ${WORKFLOW_ID}"

    RESPONSE=$(curl -sf ${VERIFY_SSL} \
        -X POST "${BASE_URL}/api/workflow-runs" \
        -H "Content-Type: application/json" \
        -d "{
            \"project_id\": \"${PROJECT_ID}\",
            \"workflow_id\": \"${WORKFLOW_ID}\",
            \"inputs\": ${INPUTS_JSON}
        }" 2>&1) || {
        fail "Submit job failed: ${RESPONSE}"
        return 1
    }

    JOB_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('job_id') or d.get('id',''))" 2>/dev/null || echo "")

    if [ -z "${JOB_ID}" ]; then
        fail "No job_id in response: ${RESPONSE}"
        return 1
    fi

    pass "Job submitted: ID=${JOB_ID}"
    echo "${JOB_ID}"
}

# =============================================================================
# 7. Approve Job from CLI (curl)
# =============================================================================
approve_job() {
    local JOB_ID="$1"
    local APPROVED="${2:-true}"
    local COMMENT="${3:-}"

    echo ""
    echo "=== 7. Approve Job ${JOB_ID} ==="

    # Get job to find thread_id
    JOB=$(curl -sf ${VERIFY_SSL} "${BASE_URL}/api/jobs/${JOB_ID}" 2>&1) || {
        fail "Could not fetch job ${JOB_ID}"
        return 1
    }

    STATUS=$(echo "$JOB" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "unknown")
    THREAD_ID=$(echo "$JOB" | python3 -c "import sys,json; print(json.load(sys.stdin).get('thread_id',''))" 2>/dev/null || echo "")

    info "Job status: ${STATUS}"

    if [ "${STATUS}" != "awaiting_approval" ]; then
        warn "Job is not awaiting approval (status=${STATUS}) — skipping approval"
        return 0
    fi

    if [ -z "${THREAD_ID}" ]; then
        fail "No thread_id in job response"
        return 1
    fi

    APPROVAL=$(curl -sf ${VERIFY_SSL} "${BASE_URL}/api/jobs/${JOB_ID}/approval" 2>&1) || {
        fail "Could not fetch approval gate for job ${JOB_ID}"
        return 1
    }
    APPROVAL_ID=$(echo "$APPROVAL" | python3 -c "import sys,json; print(json.load(sys.stdin).get('approval_id',''))" 2>/dev/null || echo "")
    if [ -z "${APPROVAL_ID}" ]; then
        fail "No approval_id in approval gate response"
        return 1
    fi
    APPROVAL_URL="${BASE_URL}/api/approvals/${THREAD_ID}"
    APPROVAL_BODY=$(APPROVAL_ID="${APPROVAL_ID}" APPROVED="${APPROVED}" COMMENT="${COMMENT}" python3 -c 'import json,os; print(json.dumps({"approval_id": os.environ["APPROVAL_ID"], "approved": os.environ["APPROVED"].lower() == "true", "comment": os.environ["COMMENT"]}))')

    RESP=$(curl -sS ${VERIFY_SSL} -X POST -H "Content-Type: application/json" -H "Idempotency-Key: manual-approval:${APPROVAL_ID}:${APPROVED}" -d "${APPROVAL_BODY}" -w $'\n%{http_code}' "${APPROVAL_URL}" 2>&1)
    RESP_CODE="${RESP##*$'\n'}"
    RESP="${RESP%$'\n'*}"
    if [[ "${RESP_CODE}" =~ ^(200|202|302|303)$ ]]; then
        pass "Approved job ${JOB_ID} via thread ${THREAD_ID} (HTTP ${RESP_CODE})"
    else
        fail "Approval failed with HTTP ${RESP_CODE}"
    fi
}

# =============================================================================
# 8. Verify Events in PostgreSQL
# =============================================================================
check_db_events() {
    local JOB_ID="${1:-}"

    echo ""
    echo "=== 8. Verify Events in PostgreSQL ==="

    RESULT=$(python3 -c "
import sys, os
sys.path.insert(0, '/app')
try:
    import psycopg_compat
    dsn = os.environ.get('FS_DB_DSN', '')
    if not dsn:
        print('FS_DB_DSN not set — skipping')
        sys.exit(0)

    conn = psycopg_compat.connect(dsn)
    cur = conn.cursor()

    # Check events table
    job_filter = '${JOB_ID}'
    if job_filter:
        cur.execute('SELECT COUNT(*) FROM platform_events WHERE job_id = %s', (job_filter,))
    else:
        cur.execute('SELECT COUNT(*) FROM platform_events')

    count = cur.fetchone()[0]
    print(f'Events in DB: {count}')

    # Check recent events
    cur.execute('SELECT event_type, title FROM platform_events ORDER BY timestamp DESC, id DESC LIMIT 5')
    events = cur.fetchall()
    for ev_type, ev_title in events:
        print(f'  [{ev_type}] {ev_title}')

    conn.close()
    print('DB events check: OK')
except Exception as e:
    print('ERROR:', str(e))
    sys.exit(1)
" 2>&1) || {
        warn "DB events check: ${RESULT}"
        return 0
    }
    pass "DB events: ${RESULT}"
}

# =============================================================================
# 10. WebSocket Test (requires wscat)
# =============================================================================
check_websocket() {
    local JOB_ID="${1:-}"

    echo ""
    echo "=== 10. WebSocket Test ==="

    if ! command -v wscat &>/dev/null; then
        warn "wscat not installed. Run: npm install -g wscat"
        info "Manual WebSocket test commands:"
        if [ -n "${JOB_ID}" ]; then
            echo "  wscat -c '${WS_BASE_URL}/ws/jobs/${JOB_ID}' --no-check"
        fi
        return 0
    fi

    if [ -z "${JOB_ID}" ]; then
        warn "No job_id provided for WebSocket test"
        return 0
    fi

    JOB_WS_URL="${WS_BASE_URL}/ws/jobs/${JOB_ID}"
    info "Testing WebSocket: ${JOB_WS_URL}"

    # Connect for 5 seconds and capture messages
    MESSAGES=$(timeout 5 wscat --no-check -c "${JOB_WS_URL}" 2>&1 || true)
    MSG_COUNT=$(echo "${MESSAGES}" | grep -c "^<" || echo "0")

    if [ "${MSG_COUNT}" -gt 0 ]; then
        pass "WebSocket: received ${MSG_COUNT} messages"
    else
        warn "WebSocket: no messages received (job may be completed)"
        info "Output: ${MESSAGES:0:200}"
    fi
}

# =============================================================================
# 11. Full Cycle Smoke Test
# =============================================================================
full_cycle_test() {
    local PROJECT_ID="$1"
    local WORKFLOW_ID="$2"
    local INPUTS_JSON="${3:-}"
    local MAX_WAIT=300  # seconds

    echo ""
    echo "=============================================="
    echo "  FULL CYCLE SMOKE TEST"
    echo "  Project: ${PROJECT_ID}"
    echo "  Workflow: ${WORKFLOW_ID}"
    echo "=============================================="

    # Submit job
    JOB_ID=$(submit_job "${PROJECT_ID}" "${WORKFLOW_ID}" "${INPUTS_JSON}") || {
        fail "Full cycle: job submission failed"
        return 1
    }

    info "Monitoring job ${JOB_ID}..."
    DEADLINE=$(($(date +%s) + MAX_WAIT))
    APPROVAL_COUNT=0
    MAX_APPROVALS=5

    while [ $(date +%s) -lt "${DEADLINE}" ] && [ "${APPROVAL_COUNT}" -lt "${MAX_APPROVALS}" ]; do
        sleep 10

        JOB=$(curl -sf ${VERIFY_SSL} "${BASE_URL}/api/jobs/${JOB_ID}" 2>&1) || continue
        STATUS=$(echo "$JOB" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "unknown")
        info "Job ${JOB_ID} status: ${STATUS} (approvals: ${APPROVAL_COUNT})"

        case "${STATUS}" in
            "completed")
                pass "Full cycle: COMPLETED after ${APPROVAL_COUNT} approvals"
                check_db_events "${JOB_ID}"
                return 0
                ;;
            "failed")
                fail "Full cycle: FAILED"
                return 1
                ;;
            "awaiting_approval")
                approve_job "${JOB_ID}" "true"
                APPROVAL_COUNT=$((APPROVAL_COUNT + 1))
                ;;
            *)
                # Still running
                ;;
        esac
    done

    # Final status
    JOB=$(curl -sf ${VERIFY_SSL} "${BASE_URL}/api/jobs/${JOB_ID}" 2>&1) || true
    STATUS=$(echo "$JOB" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "unknown")

    if [ "${STATUS}" = "completed" ]; then
        pass "Full cycle: COMPLETED"
    elif [ "${STATUS}" = "awaiting_approval" ]; then
        warn "Full cycle: still awaiting approval after ${MAX_WAIT}s (job: ${JOB_ID})"
    else
        warn "Full cycle: ended with status=${STATUS} after ${MAX_WAIT}s"
    fi

    check_websocket "${JOB_ID}"
    check_db_events "${JOB_ID}"
}

# =============================================================================
# Main
# =============================================================================
main() {
    echo ""
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║  Flow Steward Orchestrator — Manual Test Suite       ║"
    echo "╠══════════════════════════════════════════════════════╣"
    echo "║  Base URL: ${BASE_URL}"
    echo "║  Project ID: ${PROJECT_ID:-<not set>}"
    echo "╚══════════════════════════════════════════════════════╝"

    # Handle full-cycle mode
    if [ "${1:-}" = "full-cycle" ]; then
        if [ -z "${2:-}" ] || [ -z "${3:-}" ]; then
            echo "Usage: $0 full-cycle PROJECT_ID WORKFLOW_ID '[declared inputs JSON]'"
            exit 1
        fi
        full_cycle_test "$2" "$3" "${4:-}"
        exit $?
    fi

    # Run all checks
    check_registry_health
    check_graph_compilation
    check_db_events

    echo ""
    echo "=============================================="
    echo "  Manual test summary complete."
    echo ""
    echo "  To run full cycle smoke test:"
    echo "    $0 full-cycle PROJECT_ID WORKFLOW_ID '[declared inputs JSON]'"
    echo ""
    echo "  To run automated test suite:"
    echo "    docker compose -f infra/docker/docker-compose.yaml exec fs-app python -m pytest tests/unit/ -v"
    echo "    docker compose -f infra/docker/docker-compose.yaml exec fs-app python -m pytest tests/integration/ -v"
    echo "    docker compose -f infra/docker/docker-compose.yaml exec fs-app python -m pytest tests/e2e/ -v -s --timeout=300"
    echo "    docker compose -f infra/docker/docker-compose.yaml exec fs-app python -m pytest tests/ui/ -v --timeout=60"
    echo "=============================================="
}

main "$@"
