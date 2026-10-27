#!/usr/bin/env python3
"""
Comprehensive workflow smoke tests for the Flow Steward orchestrator.
Submits tasks for all workflow types, tests gates, rejection, abort, and APIs.
Run inside Docker: flow-steward testing smoke-test
"""

import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
import urllib3

from core.application.management.command_models import WorkflowRunInput
from core.application.tooling.testing.runtime_env import resolve_base_url, resolve_test_env
from core.application.tooling.testing.smoke_test_scenarios_admin import (
    run_admin_smoke_scenarios,
)
from core.application.tooling.testing.smoke_test_scenarios_workflows import (
    run_workflow_smoke_scenarios,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_CA_CERT = _REPO_ROOT / ".ssl" / "cert.pem"
_RUN_ENV: dict[str, str] = {}
TEST_ENV = None
BASE = ""
PROJECT_ID_RAW = ""
USERNAME = ""
PASSWORD = ""
PROJECT_ID = ""
WORKFLOW_ID = ""
S: requests.Session | None = None
H: dict[str, str] = {}
BUGS: list[tuple[str, str]] = []


class SmokeFixtureError(RuntimeError):
    """The selected smoke-test project cannot run the task scenarios."""


def _resolve_verify_setting() -> bool | str:
    raw = str(_RUN_ENV.get("E2E_VERIFY_SSL", "1")).strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    cert_override = str(_RUN_ENV.get("E2E_CA_CERT") or "").strip()
    cert_path = Path(cert_override) if cert_override else _DEFAULT_CA_CERT
    if cert_path.is_file():
        return str(cert_path)
    return True


def setup_session():
    s = requests.Session()
    s.verify = _resolve_verify_setting()
    if s.verify is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    login_payload = {"email": USERNAME, "password": PASSWORD}
    r = s.post(f"{BASE}/api/auth/login", json=login_payload)
    if r.status_code == 409:
        payload = r.json()
        options = payload.get("account_options") if isinstance(payload, dict) else None
        active = [
            row
            for row in options or []
            if isinstance(row, dict)
            and bool(row.get("is_active", True))
            and str(row.get("account_id") or "").strip()
        ]
        defaults = [row for row in active if bool(row.get("is_default"))]
        selected = None
        if len(defaults) == 1:
            selected = defaults[0]
        elif len(active) == 1:
            selected = active[0]
        if payload.get("requires_account_selection") and selected is not None:
            r = s.post(
                f"{BASE}/api/auth/login",
                json={**login_payload, "account_id": str(selected["account_id"]).strip()},
            )
    if r.status_code != 200:
        print(f"Login failed: {r.status_code} {r.text[:200]}")
        sys.exit(1)
    csrf = s.cookies.get("fs_csrf", "")
    print(f"✓ Login OK (CSRF: {bool(csrf)})")
    return s, {"x-csrf-token": csrf}


def _session() -> requests.Session:
    if S is None:
        raise RuntimeError("Smoke test session is not initialized.")
    return S


def bug(test_name: str, msg: str):
    BUGS.append((test_name, msg))
    print(f"  🐛 BUG [{test_name}]: {msg}")


def ok(msg: str):
    print(f"  ✓ {msg}")


def submit_task(summary: str, mode: str | None = None, workflow_id=None):
    """Launch the selected workflow; summary/mode are smoke scenario labels only."""
    selected_workflow_id = str(workflow_id or WORKFLOW_ID or "").strip()
    if not selected_workflow_id:
        raise SmokeFixtureError("No compatible workflow was selected by smoke preflight")
    request = WorkflowRunInput(
        project_id=PROJECT_ID,
        workflow_id=selected_workflow_id,
    )
    payload = request.model_dump(
        mode="json",
        include={"project_id", "workflow_id", "inputs"},
        exclude_none=True,
    )
    r = _session().post(f"{BASE}/api/workflow-runs", json=payload, headers=H)
    job_id = None
    if r.status_code in (200, 201, 202):
        data = r.json()
        job_id = data.get("job_id") or data.get("id")
    return job_id, r


def assert_retired_task_form_removed(summary: str, workflow_id=None):
    """Verify the removed Jinja-era project task form route stays unavailable."""
    data = {"task_summary": summary}
    if workflow_id:
        data["workflow_id"] = str(workflow_id)
    return _session().post(
        f"{BASE}/projects/{PROJECT_ID}/tasks", data=data, headers=H, allow_redirects=False
    )


def _safe_request(method, url, **kwargs):
    """Retry on connection errors (server may be busy with graph execution)."""
    for attempt in range(5):
        try:
            return method(url, **kwargs)
        except requests.exceptions.ConnectionError:
            if attempt < 4:
                time.sleep(3)
            else:
                raise
    return None


def get_job(job_id):
    r = _safe_request(_session().get, f"{BASE}/api/jobs/{job_id}", headers=H)
    return r.json() if r.status_code == 200 else {}


def wait_for_status(job_id, targets, timeout=120, interval=5):
    start = time.time()
    last = None
    while time.time() - start < timeout:
        try:
            j = get_job(job_id)
        except requests.exceptions.ConnectionError:
            time.sleep(interval)
            continue
        last = j.get("status", "")
        if last in targets:
            return last, j
        if last in ("completed", "failed", "cancelled", "error"):
            return last, j
        time.sleep(interval)
    try:
        return last, get_job(job_id)
    except requests.exceptions.ConnectionError:
        return last, {}


def _approval_decision(job_id, *, action, comment):
    j = get_job(job_id)
    tid = j.get("thread_id", "")
    session = _session()
    gate = _safe_request(session.get, f"{BASE}/api/jobs/{job_id}/approval", headers=H)
    approval = gate.json() if gate.status_code == 200 else {}
    approval_id = str(approval.get("approval_id") or "").strip()
    if not tid or not approval_id:
        return gate
    headers = dict(H)
    headers["Idempotency-Key"] = f"smoke-approval:{approval_id}:{action}"
    return _safe_request(
        session.post,
        f"{BASE}/api/approvals/{tid}",
        json={"approval_id": approval_id, "action": action, "comment": comment},
        headers=headers,
    )


def approve(job_id, comment="Auto-approved by smoke test"):
    return _approval_decision(job_id, action="approve", comment=comment)


def reject(job_id, comment="Rejected by smoke test"):
    return _approval_decision(job_id, action="reject", comment=comment)


def abort_gate(job_id, comment="Aborted by smoke test"):
    return _approval_decision(job_id, action="abort", comment=comment)


def cancel(job_id):
    return _safe_request(_session().post, f"{BASE}/api/jobs/{job_id}/cancel", headers=H)


def get_workflows():
    r = _session().get(f"{BASE}/api/workflows", headers=H)
    data = r.json()
    if isinstance(data, dict):
        return data.get("workflows", [])
    return data if isinstance(data, list) else []


def _workflow_contract_error(workflow_id: str) -> str:
    encoded_project_id = quote(PROJECT_ID, safe="")
    encoded_workflow_id = quote(workflow_id, safe="")
    response = _session().get(
        f"{BASE}/api/projects/{encoded_project_id}/workflows/{encoded_workflow_id}/contract",
        headers=H,
    )
    if response.status_code != 200:
        return f"contract endpoint returned {response.status_code}: {response.text[:200]}"
    payload = response.json()
    if not isinstance(payload, dict):
        return "contract endpoint returned a non-object payload"
    if str(payload.get("workflow_id") or "").strip() != workflow_id:
        return "contract workflow_id does not match the selected workflow"
    if not bool(payload.get("enabled")):
        return "workflow contract is disabled"
    interface = payload.get("interface")
    input_schema = interface.get("input_schema") if isinstance(interface, dict) else None
    if isinstance(input_schema, dict):
        required = input_schema.get("required", [])
    else:
        input_contract = interface.get("input") if isinstance(interface, dict) else None
        required = input_contract.get("required") if isinstance(input_contract, dict) else None
        if required is None:
            return "workflow input contract does not declare a JSON schema"
    if not isinstance(required, list):
        return "workflow input contract required field is not a list"
    required_names = [str(item).strip() for item in required if str(item).strip()]
    if required_names:
        return "smoke fixture does not provide required workflow inputs: " + ", ".join(
            required_names
        )
    return ""


def _resolve_smoke_workflow(requested_workflow_id: str = "") -> str:
    encoded_project_id = quote(PROJECT_ID, safe="")
    response = _session().get(
        f"{BASE}/api/projects/{encoded_project_id}/workflow-catalog",
        headers=H,
    )
    if response.status_code != 200:
        raise SmokeFixtureError(
            f"Project workflow catalog returned {response.status_code}: {response.text[:200]}"
        )
    payload = response.json()
    rows = payload.get("workflows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SmokeFixtureError("Project workflow catalog returned an incompatible payload")

    compatible_ids = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        workflow_id = str(row.get("workflow_id") or "").strip()
        availability = str(row.get("availability_status") or "available").strip().lower()
        if (
            workflow_id
            and bool(row.get("enabled"))
            and not bool(row.get("new_runs_blocked"))
            and availability not in {"missing", "removed", "unavailable"}
        ):
            compatible_ids.append(workflow_id)

    requested = str(requested_workflow_id or "").strip()
    if requested:
        if requested not in compatible_ids:
            raise SmokeFixtureError(
                f"Requested workflow '{requested}' is not linked and enabled for project {PROJECT_ID}"
            )
        candidate_ids = [requested]
    else:
        candidate_ids = list(compatible_ids)

    if not candidate_ids:
        raise SmokeFixtureError(
            f"Project {PROJECT_ID} has no linked, enabled workflow available for smoke tasks"
        )

    contract_errors: list[str] = []
    for workflow_id in candidate_ids:
        error = _workflow_contract_error(workflow_id)
        if not error:
            return workflow_id
        contract_errors.append(f"{workflow_id}: {error}")
    raise SmokeFixtureError("No compatible smoke workflow contract: " + "; ".join(contract_errors))


def _enabled_workflow_id():
    if WORKFLOW_ID:
        return WORKFLOW_ID
    enabled = []
    for workflow in get_workflows():
        if not isinstance(workflow, dict) or not workflow.get("enabled", True):
            continue
        workflow_id = str(workflow.get("workflow_id") or workflow.get("id") or "").strip()
        if workflow_id:
            enabled.append(workflow_id)
    return enabled[0] if enabled else ""


def _scheduled_jobs_payload():
    r = _session().get(f"{BASE}/api/projects/{PROJECT_ID}/scheduled-jobs", headers=H)
    if r.status_code != 200:
        raise RuntimeError(f"scheduled jobs API failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"scheduled jobs API returned unexpected payload: {data!r}")
    return data


def _scheduled_runs_for_job(scheduled_job_id):
    rows = _scheduled_jobs_payload().get("runs", [])
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("scheduled_job_id") or "").strip() == str(scheduled_job_id)
    ]


def _wait_for_scheduled_run(scheduled_job_id, baseline_run_ids=None, timeout=30):
    baseline_run_ids = set(baseline_run_ids or set())
    deadline = time.time() + timeout
    while time.time() < deadline:
        for row in _scheduled_runs_for_job(scheduled_job_id):
            run_id = str(row.get("id") or "").strip()
            if run_id and run_id not in baseline_run_ids:
                return row
        time.sleep(1)
    return None


def _extract_jobs_rows(payload):
    if isinstance(payload, dict):
        jobs = payload.get("jobs", [])
    elif isinstance(payload, list):
        jobs = payload
    else:
        return []
    if not isinstance(jobs, list):
        return []
    return [
        {**row, "id": row.get("id") or row.get("job_id")}
        if isinstance(row, dict) and (row.get("id") or row.get("job_id"))
        else row
        for row in jobs
    ]


def _job_identifier(row):
    if not isinstance(row, dict):
        return None
    return row.get("id") or row.get("job_id") or row.get("jobId")


def get_active_jobs():
    r = _session().get(f"{BASE}/api/jobs?limit=200", headers=H)
    data = r.json()
    jobs = _extract_jobs_rows(data)
    return [
        j
        for j in jobs
        if isinstance(j, dict)
        and str(j.get("project_id") or "").strip() == PROJECT_ID
        and j.get("status") in ("awaiting_approval", "in_progress", "queued")
    ]


def _explicit_run_env(
    *,
    base_url: str,
    project_id: str,
    lane: str,
    verify_ssl: bool,
) -> dict[str, str]:
    keys = (
        "APP_BASE_URL",
        "FS_SERVE_SSL",
        "TEST_RUNNER_IN_DOCKER",
        "TEST_PROJECT_BACKEND_ID",
        "TEST_PROJECT_UI_ID",
        "TEST_PROJECT_BACKEND_NAME",
        "TEST_PROJECT_UI_NAME",
        "E2E_ADMIN_USERNAME",
        "E2E_OPERATOR_USERNAME",
        "E2E_VIEWER_USERNAME",
        "E2E_ADMIN_PASSWORD",
        "E2E_OPERATOR_PASSWORD",
        "E2E_VIEWER_PASSWORD",
        "E2E_ADMIN_DISPLAY_NAME",
        "E2E_OPERATOR_DISPLAY_NAME",
        "E2E_VIEWER_DISPLAY_NAME",
        "E2E_CA_CERT",
    )
    run_env = {
        key: str(_runtime_env.get(key) or "").strip()
        for key in keys
        if str(_runtime_env.get(key) or "").strip()
    }
    if base_url:
        run_env["APP_BASE_URL"] = str(base_url).strip()
    run_env["E2E_VERIFY_SSL"] = "1" if verify_ssl else "0"
    clean_lane = str(lane or "backend").strip().lower()
    if project_id:
        run_env["TEST_PROJECT_UI_ID" if clean_lane == "ui" else "TEST_PROJECT_BACKEND_ID"] = str(
            project_id
        ).strip()
    return run_env


def run_smoke_test(
    *,
    project_id: str = "",
    workflow_id: str = "",
    lane: str = "backend",
    base_url: str = "",
    verify_ssl: bool = False,
) -> int:
    global \
        BASE, \
        BUGS, \
        H, \
        PASSWORD, \
        PROJECT_ID, \
        PROJECT_ID_RAW, \
        S, \
        TEST_ENV, \
        USERNAME, \
        WORKFLOW_ID, \
        _RUN_ENV

    _RUN_ENV = _explicit_run_env(
        base_url=base_url,
        project_id=project_id,
        lane=lane,
        verify_ssl=verify_ssl,
    )
    TEST_ENV = resolve_test_env(_RUN_ENV, lane=lane, include_dotenv=False)
    BASE = resolve_base_url(_RUN_ENV, include_dotenv=False)
    PROJECT_ID_RAW = str(TEST_ENV.project_id or "").strip()
    USERNAME = TEST_ENV.username
    PASSWORD = TEST_ENV.password
    BUGS = []
    WORKFLOW_ID = ""

    if not PROJECT_ID_RAW:
        print("TEST_PROJECT_BACKEND_ID or TEST_PROJECT_UI_ID is required for smoke testing")
        return 1

    PROJECT_ID = PROJECT_ID_RAW
    S, H = setup_session()

    try:
        WORKFLOW_ID = _resolve_smoke_workflow(workflow_id)
    except SmokeFixtureError as exc:
        print(f"Smoke preflight failed: {exc}")
        return 1
    print(f"Smoke preflight selected workflow: {WORKFLOW_ID}")

    print("\n=== SETUP ===")
    active = get_active_jobs()
    print(f"Active jobs to cancel: {len(active)}")
    for j in active:
        job_id = _job_identifier(j)
        if not job_id:
            print(f"  Skipping active job without id: {j}")
            continue
        print(f"  Cancelling {job_id}: {j['status']}")
        cancel(job_id)
    time.sleep(2)

    workflows = get_workflows()
    print(f"\nWorkflows ({len(workflows)}):")
    for w in workflows:
        if isinstance(w, dict):
            wid = w.get("workflow_id", w.get("id", "?"))
            print(f"  {wid}: {w.get('display_name', w.get('name', ''))} enabled={w.get('enabled')}")

    context = {
        "S": S,
        "H": H,
        "BASE": BASE,
        "PROJECT_ID": PROJECT_ID,
        "BUGS": BUGS,
        "workflows": workflows,
        "task_workflow_id": WORKFLOW_ID,
        "bug": bug,
        "ok": ok,
        "submit_task": submit_task,
        "assert_retired_task_form_removed": assert_retired_task_form_removed,
        "get_job": get_job,
        "wait_for_status": wait_for_status,
        "approve": approve,
        "reject": reject,
        "abort_gate": abort_gate,
        "cancel": cancel,
        "get_active_jobs": get_active_jobs,
        "_enabled_workflow_id": _enabled_workflow_id,
        "_scheduled_runs_for_job": _scheduled_runs_for_job,
        "_wait_for_scheduled_run": _wait_for_scheduled_run,
        "_extract_jobs_rows": _extract_jobs_rows,
    }

    run_workflow_smoke_scenarios(context)
    run_admin_smoke_scenarios(context)
    return 1 if BUGS else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in {"-h", "--help"} for arg in args):
        print(__doc__.strip())
        print("\nEnvironment:")
        print("  TEST_PROJECT_BACKEND_ID or lane defaults must point at a test project.")
        print("  E2E_VERIFY_SSL=0 disables TLS verification for local smoke runs.")
        return 0
    return run_smoke_test()


if __name__ == "__main__":
    raise SystemExit(main())
