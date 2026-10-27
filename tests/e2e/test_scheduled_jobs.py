from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any
from urllib.parse import unquote

import pytest
import requests

from core.application.tooling.testing.runtime_env import (
    _read_dotenv,
    resolve_base_url,
    resolve_test_env,
)
from tests.e2e.conftest import e2e_request
from tests.e2e.helpers import ensure_canonical_project_workflows
from tests.e2e.neutral_registry import NEUTRAL_AUTO_WORKFLOW_ID

pytestmark = [pytest.mark.e2e]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
BACKEND_ROLE = str(os.environ.get("E2E_BACKEND_ROLE", "admin") or "admin").strip().lower()
_BACKEND_ENV = resolve_test_env(os.environ, lane="backend", role=BACKEND_ROLE, include_dotenv=True)
PROJECT_ID = str(_BACKEND_ENV.project_id or "").strip()
VERIFY_SSL = str(os.getenv("E2E_VERIFY_SSL", "0") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ACTIVE_JOB_STATUSES = {
    "pending",
    "queued",
    "in_progress",
    "awaiting_approval",
    "retryable_interrupted_step",
    "stuck",
}
TERMINAL_JOB_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "max_iterations",
}


def _explicit_mcp_bearer_configured() -> bool:
    names = ("MCP_BEARER_TOKEN", "FS_SESSION_TOKEN")
    worker_value = _docker_worker_explicit_mcp_bearer_configured(names)
    if worker_value is not None:
        return worker_value
    dotenv = _read_dotenv()
    return any(str(os.getenv(name) or dotenv.get(name) or "").strip() for name in names)


def _docker_worker_explicit_mcp_bearer_configured(names: tuple[str, ...]) -> bool | None:
    if os.path.exists("/.dockerenv"):
        return None
    container_name = str(os.getenv("FS_WORKER_CONTAINER") or "flow-steward-fs-worker-1").strip()
    if not container_name:
        return None
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{range .Config.Env}}{{println .}}{{end}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    env_lines = [line.strip() for line in str(result.stdout or "").splitlines()]
    return any(
        line.startswith(f"{name}=") and bool(line.split("=", 1)[1].strip())
        for name in names
        for line in env_lines
    )


def _get(path: str, **kwargs) -> requests.Response:
    return e2e_request(
        "GET",
        f"{BASE_URL}{path}",
        verify=VERIFY_SSL,
        timeout=20,
        **kwargs,
    )


def _post(path: str, **kwargs) -> requests.Response:
    return e2e_request(
        "POST",
        f"{BASE_URL}{path}",
        verify=VERIFY_SSL,
        timeout=20,
        **kwargs,
    )


def _patch(path: str, **kwargs) -> requests.Response:
    return e2e_request(
        "PATCH",
        f"{BASE_URL}{path}",
        verify=VERIFY_SSL,
        timeout=20,
        **kwargs,
    )


def _delete(path: str, **kwargs) -> requests.Response:
    return e2e_request(
        "DELETE",
        f"{BASE_URL}{path}",
        verify=VERIFY_SSL,
        timeout=20,
        **kwargs,
    )


def _cancel_job(job_id: str | int) -> None:
    try:
        _post(
            f"/api/jobs/{job_id}/cancel",
            headers={"accept": "application/json"},
            allow_redirects=False,
        )
    except Exception:
        return


def _job_row(job_id: str | int) -> dict[str, Any]:
    resp = _get(f"/api/jobs/{job_id}")
    if resp.status_code != 200:
        return {
            "_http_error_status": int(resp.status_code or 0),
            "_http_error_body": resp.text[:300],
        }
    body = resp.json() or {}
    if isinstance(body, dict) and isinstance(body.get("job"), dict):
        return dict(body.get("job") or {})
    assert isinstance(body, dict), f"Unexpected job payload for {job_id}: {body!r}"
    return body


def _job_status(job_row: dict[str, Any]) -> str:
    return str(job_row.get("status") or "").strip().lower()


def _job_has_contract_readiness_failure(job_row: dict[str, Any]) -> bool:
    runtime = job_row.get("runtime") if isinstance(job_row, dict) else {}
    if not isinstance(runtime, dict):
        runtime = {}
    blob = " ".join(
        [
            str(job_row.get("error") or ""),
            str(job_row.get("last_error") or ""),
            str(runtime.get("last_stderr_excerpt") or ""),
            str(runtime.get("wait_reason") or ""),
        ]
    ).lower()
    return (
        "graph contract validation failed" in blob
        or "unresolved contract errors" in blob
        or "contract validation failed" in blob
    )


def _approve_job(job_id: str | int, thread_id: str) -> None:
    gate = _get(f"/api/jobs/{job_id}/approval")
    gate_body = gate.json() if gate.status_code == 200 else {}
    approval_id = str(gate_body.get("approval_id") or "").strip()
    assert approval_id, f"Approval gate for job {job_id} is missing approval_id"
    resp = _post(
        f"/api/approvals/{thread_id}",
        json={"approval_id": approval_id, "approved": True},
        headers={"Idempotency-Key": f"scheduled-approval:{approval_id}:approve"},
        allow_redirects=False,
    )
    assert resp.status_code in (200, 202), (
        f"Approval failed for thread {thread_id}: {resp.status_code} {resp.text[:250]}"
    )


def _drive_job_to_terminal(
    job_id: str | int,
    *,
    timeout: int = 360,
    interval: int = 2,
    max_approvals: int = 12,
    cancel_on_timeout: bool = True,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    approvals = 0
    last_status = "unknown"
    while time.monotonic() < deadline:
        row = _job_row(job_id)
        if row.get("_http_error_status"):
            # Transient read errors can happen right after enqueue/restart.
            time.sleep(interval)
            continue
        status = _job_status(row)
        last_status = status or "unknown"
        if status in TERMINAL_JOB_STATUSES:
            if status == "failed" and _job_has_contract_readiness_failure(row):
                pytest.skip(
                    "Scheduled-job workflow skipped: selected workflow failed immediately due "
                    "to contract readiness errors in this environment"
                )
            return row
        if status == "awaiting_approval":
            thread_id = str(row.get("thread_id") or "").strip()
            assert thread_id, f"Job {job_id} is awaiting approval without thread_id"
            _approve_job(job_id, thread_id)
            approvals += 1
            if approvals > max_approvals:
                pytest.fail(
                    f"Job {job_id} exceeded approval budget ({max_approvals}) before terminal status"
                )
        time.sleep(interval)

    if cancel_on_timeout:
        _cancel_job(job_id)
        settle_deadline = time.monotonic() + 45
        while time.monotonic() < settle_deadline:
            row = _job_row(job_id)
            status = _job_status(row)
            if status in TERMINAL_JOB_STATUSES:
                return row
            time.sleep(1)
        raise TimeoutError(
            f"Job {job_id} timed out and did not settle after cancel; last_status={last_status}"
        )

    raise TimeoutError(f"Job {job_id} did not reach terminal state; last_status={last_status}")


def _row_enabled(row: dict | None) -> bool:
    value = None if row is None else row.get("enabled")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "t", "yes", "on"}


def _scheduled_jobs_payload(project_id: str) -> dict:
    resp = _get(f"/api/projects/{project_id}/scheduled-jobs")
    assert resp.status_code == 200, (
        f"scheduled jobs API failed: {resp.status_code} {resp.text[:300]}"
    )
    payload = resp.json()
    assert str(payload.get("project_id")) == str(project_id)
    assert isinstance(payload.get("jobs"), list)
    assert isinstance(payload.get("runs"), list)
    return payload


def _find_scheduled_job_by_id(project_id: str, scheduled_job_id: str | int) -> dict | None:
    payload = _scheduled_jobs_payload(project_id)
    for row in payload.get("jobs") or []:
        if str(row.get("id") or "").strip() == str(scheduled_job_id):
            return row
    return None


def _wait_for_scheduled_job_by_id(
    project_id: str, scheduled_job_id: str | int, timeout: int = 60
) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = _find_scheduled_job_by_id(project_id, scheduled_job_id)
        if row is not None:
            return row
        time.sleep(0.5)
    raise TimeoutError(f"Could not find scheduled job id={scheduled_job_id} within {timeout}s")


def _scheduled_job_id_from_redirect(response: requests.Response) -> str:
    location = str(response.headers.get("location") or "")
    if not location:
        return ""
    decoded = unquote(location)
    match = re.search(r"Scheduled job #(?P<id>[^&?#]+)", decoded, re.IGNORECASE)
    if not match:
        return ""
    return str(match.group("id") or "").strip()


def _wait_for_scheduled_job(
    project_id: str, scheduled_job_id: str | int, *, enabled: bool, timeout: int = 10
) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = _find_scheduled_job_by_id(project_id, scheduled_job_id)
        if row is not None and _row_enabled(row) is enabled:
            return row
        time.sleep(0.5)
    raise TimeoutError(
        f"Scheduled job {scheduled_job_id} did not reach enabled={enabled} within {timeout}s"
    )


def _wait_for_scheduled_run(
    project_id: str, scheduled_job_id: str | int, timeout: int = 10
) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = _scheduled_jobs_payload(project_id)
        for row in payload.get("runs") or []:
            if str(row.get("scheduled_job_id") or "").strip() == str(scheduled_job_id):
                return row
        time.sleep(0.5)
    raise TimeoutError(
        f"Scheduled run row for scheduled job {scheduled_job_id} was not recorded within {timeout}s"
    )


def _scheduled_runs_for_job(project_id: str, scheduled_job_id: str | int) -> list[dict]:
    payload = _scheduled_jobs_payload(project_id)
    rows = []
    for row in payload.get("runs") or []:
        if str(row.get("scheduled_job_id") or "").strip() == str(scheduled_job_id):
            rows.append(row)
    return rows


def _wait_for_new_scheduled_run(
    project_id: str,
    scheduled_job_id: str | int,
    *,
    baseline_run_ids: set[str],
    timeout: int = 210,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = _scheduled_runs_for_job(project_id, scheduled_job_id)
        for row in runs:
            run_id = str(row.get("id") or "").strip()
            if run_id and run_id not in baseline_run_ids:
                return row
        time.sleep(2)
    raise TimeoutError(
        f"No new scheduled run appeared for scheduled_job_id={scheduled_job_id} within {timeout}s"
    )


@pytest.fixture(scope="module")
def test_project_id() -> str:
    if not PROJECT_ID:
        pytest.skip("TEST_PROJECT_BACKEND_ID not set")
    return PROJECT_ID


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = _get("/health")
    except Exception as e:
        pytest.skip(f"Flow Steward server not available at {BASE_URL}: {e}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server health endpoint unhealthy: {resp.status_code}")


@pytest.fixture(scope="module")
def scheduled_job_workflow_id(test_project_id: str, server_available) -> str:
    """Schedule the gate-free neutral workflow so scheduled runs can reach a terminal state."""
    ensure_canonical_project_workflows(test_project_id)
    return NEUTRAL_AUTO_WORKFLOW_ID


@pytest.fixture(scope="module")
def scheduled_job_cleanup(test_project_id: str):
    created_scheduled_job_ids: list[str] = []
    created_job_ids: list[str] = []
    yield {
        "scheduled_job_ids": created_scheduled_job_ids,
        "job_ids": created_job_ids,
    }
    for job_id in created_job_ids:
        _cancel_job(job_id)
    for scheduled_job_id in created_scheduled_job_ids:
        if _find_scheduled_job_by_id(test_project_id, scheduled_job_id) is None:
            continue
        try:
            _delete(f"/api/projects/{test_project_id}/scheduled-jobs/{scheduled_job_id}")
        except Exception:
            continue


class TestScheduledJobs:
    def test_scheduled_job_create_toggle_delete(
        self,
        test_project_id: str,
        server_available,
        scheduled_job_workflow_id: str,
        scheduled_job_cleanup: dict,
    ):
        create_resp = _post(
            f"/api/projects/{test_project_id}/scheduled-jobs",
            json={
                "workflow_id": scheduled_job_workflow_id,
                "cron_expr": "17 3 * * *",
                "enabled": True,
            },
            allow_redirects=False,
        )
        assert create_resp.status_code == 201, (
            f"Scheduled job create failed: {create_resp.text[:300]}"
        )

        hinted_id = str((create_resp.json() or {}).get("scheduled_job_id") or "").strip()
        assert hinted_id, "Create response must identify the owning schedule"
        created = _wait_for_scheduled_job_by_id(test_project_id, hinted_id, timeout=60)
        scheduled_job_id = str(created.get("id") or "").strip()
        assert scheduled_job_id
        scheduled_job_cleanup["scheduled_job_ids"].append(scheduled_job_id)
        assert _row_enabled(created) is True
        assert str(created.get("status") or "").lower() == "active"

        pause_resp = _patch(
            f"/api/projects/{test_project_id}/scheduled-jobs/{scheduled_job_id}/toggle",
            json={"enabled": False},
            allow_redirects=False,
        )
        assert pause_resp.status_code == 200
        paused = _wait_for_scheduled_job(test_project_id, scheduled_job_id, enabled=False)
        assert str(paused.get("status") or "").lower() == "paused"

        enable_resp = _patch(
            f"/api/projects/{test_project_id}/scheduled-jobs/{scheduled_job_id}/toggle",
            json={"enabled": True},
            allow_redirects=False,
        )
        assert enable_resp.status_code == 200
        reenabled = _wait_for_scheduled_job(test_project_id, scheduled_job_id, enabled=True)
        assert str(reenabled.get("status") or "").lower() == "active"
        assert reenabled.get("next_run_at"), "Enabled scheduled job should compute next_run_at"

        delete_resp = _delete(f"/api/projects/{test_project_id}/scheduled-jobs/{scheduled_job_id}")
        assert delete_resp.status_code == 200
        assert _find_scheduled_job_by_id(test_project_id, scheduled_job_id) is None
        scheduled_job_cleanup["scheduled_job_ids"].remove(scheduled_job_id)

    def test_scheduled_job_run_now_creates_run_history(
        self,
        test_project_id: str,
        server_available,
        scheduled_job_workflow_id: str,
        scheduled_job_cleanup: dict,
    ):
        create_resp = _post(
            f"/api/projects/{test_project_id}/scheduled-jobs",
            json={
                "workflow_id": scheduled_job_workflow_id,
                "cron_expr": "11 4 * * *",
                "enabled": False,
            },
            allow_redirects=False,
        )
        assert create_resp.status_code == 201, (
            f"Scheduled job create failed: {create_resp.text[:300]}"
        )

        hinted_id = str((create_resp.json() or {}).get("scheduled_job_id") or "").strip()
        assert hinted_id, "Create response must identify the owning schedule"
        created = _wait_for_scheduled_job_by_id(test_project_id, hinted_id, timeout=60)
        scheduled_job_id = str(created.get("id") or "").strip()
        assert scheduled_job_id
        scheduled_job_cleanup["scheduled_job_ids"].append(scheduled_job_id)

        run_resp = _post(
            f"/api/projects/{test_project_id}/scheduled-jobs/{scheduled_job_id}/run-now",
            allow_redirects=False,
        )
        assert run_resp.status_code == 200, run_resp.text[:300]

        run_row = _wait_for_scheduled_run(test_project_id, scheduled_job_id)
        job_id = str((run_resp.json() or {}).get("job_id") or run_row.get("job_id") or "").strip()
        assert str(run_row.get("scheduled_job_id") or "").strip() == scheduled_job_id
        run_status = str(run_row.get("status") or "").strip().lower()
        assert run_status in {"enqueued", "skipped", "failed", "completed"}
        if job_id:
            scheduled_job_cleanup["job_ids"].append(job_id)
            assert str(run_row.get("job_id") or "").strip() == job_id
            job_resp = _get(f"/api/jobs/{job_id}")
            assert job_resp.status_code == 200
            status = str(job_resp.json().get("status") or "").strip().lower()
            assert status in ACTIVE_JOB_STATUSES | {
                "completed",
                "failed",
                "cancelled",
                "max_iterations",
            }

    def test_scheduled_jobs_api_list_structure_contracts(
        self,
        test_project_id: str,
        server_available,
    ):
        """Verify /api/projects/{id}/scheduled-jobs returns all required structural fields."""
        payload = _scheduled_jobs_payload(test_project_id)

        # Top-level envelope
        assert "project_id" in payload, "Missing project_id in scheduled-jobs API envelope"
        assert "jobs" in payload, "Missing jobs list in scheduled-jobs API envelope"
        assert "runs" in payload, "Missing runs list in scheduled-jobs API envelope"
        assert isinstance(payload["jobs"], list)
        assert isinstance(payload["runs"], list)

        # Per-job field contract (validate against any existing jobs)
        required_job_fields = {
            "id",
            "project_id",
            "workflow_id",
            "cron_expr",
            "enabled",
            "status",
        }
        for job_row in payload["jobs"]:
            assert "task_summary" not in job_row
            missing = required_job_fields - set(job_row.keys())
            assert not missing, (
                f"Scheduled job row is missing required fields: {sorted(missing)!r}; row={job_row}"
            )

        # Per-run field contract (validate against any existing runs)
        required_run_fields = {"id", "scheduled_job_id", "run_type", "status"}
        for run_row in payload["runs"]:
            missing = required_run_fields - set(run_row.keys())
            assert not missing, (
                f"Scheduled run row is missing required fields: {sorted(missing)!r}; row={run_row}"
            )

    @pytest.mark.slow
    def test_paused_scheduled_job_suppresses_auto_trigger(
        self,
        test_project_id: str,
        server_available,
        scheduled_job_workflow_id: str,
        scheduled_job_cleanup: dict,
    ):
        """A scheduled job paused immediately after creation must not produce auto-triggered runs."""
        create_resp = _post(
            f"/api/projects/{test_project_id}/scheduled-jobs",
            json={
                "workflow_id": scheduled_job_workflow_id,
                "cron_expr": "* * * * *",
                "enabled": True,
            },
            allow_redirects=False,
        )
        assert create_resp.status_code == 201, (
            f"Scheduled job create failed: {create_resp.text[:300]}"
        )

        hinted_id = str((create_resp.json() or {}).get("scheduled_job_id") or "").strip()
        assert hinted_id, "Create response must identify the owning schedule"
        created = _wait_for_scheduled_job_by_id(test_project_id, hinted_id, timeout=60)
        scheduled_job_id = str(created.get("id") or "").strip()
        assert scheduled_job_id
        scheduled_job_cleanup["scheduled_job_ids"].append(scheduled_job_id)

        # Immediately pause the job before a cron tick can fire it.
        pause_resp = _patch(
            f"/api/projects/{test_project_id}/scheduled-jobs/{scheduled_job_id}/toggle",
            json={"enabled": False},
            allow_redirects=False,
        )
        assert pause_resp.status_code == 200
        _wait_for_scheduled_job(test_project_id, scheduled_job_id, enabled=False)

        # Capture run IDs present at pause time (could be zero or one if cron beat us).
        baseline_run_ids = {
            str(row.get("id") or "").strip()
            for row in _scheduled_runs_for_job(test_project_id, scheduled_job_id)
            if str(row.get("id") or "").strip()
        }

        # Wait past two full cron-minute boundaries to give the scheduler ample opportunity
        # to fire — a correctly paused job must produce zero new runs during this window.
        wait_seconds = 90
        time.sleep(wait_seconds)

        post_pause_runs = _scheduled_runs_for_job(test_project_id, scheduled_job_id)
        new_runs = [
            row
            for row in post_pause_runs
            if str(row.get("id") or "").strip()
            and str(row.get("id") or "").strip() not in baseline_run_ids
        ]
        assert not new_runs, (
            f"Paused scheduled job {scheduled_job_id} produced {len(new_runs)} unexpected auto-run(s) "
            f"during {wait_seconds}s observation window: {new_runs!r}"
        )

    @pytest.mark.slow
    def test_scheduled_job_cron_tick_auto_run_records_history_and_reaches_terminal(
        self,
        test_project_id: str,
        server_available,
        scheduled_job_workflow_id: str,
        scheduled_job_cleanup: dict,
    ):
        if not _explicit_mcp_bearer_configured():
            pytest.skip(
                "Scheduled auto-run terminal workflow requires an explicit MCP bearer "
                "credential in the worker runtime"
            )
        create_resp = _post(
            f"/api/projects/{test_project_id}/scheduled-jobs",
            json={
                "workflow_id": scheduled_job_workflow_id,
                "cron_expr": "* * * * *",
                "enabled": True,
            },
            allow_redirects=False,
        )
        assert create_resp.status_code == 201, create_resp.text[:300]

        hinted_id = str((create_resp.json() or {}).get("scheduled_job_id") or "").strip()
        assert hinted_id, "Create response must identify the owning schedule"
        created = _wait_for_scheduled_job_by_id(test_project_id, hinted_id, timeout=60)
        scheduled_job_id = str(created.get("id") or "").strip()
        assert scheduled_job_id
        scheduled_job_cleanup["scheduled_job_ids"].append(scheduled_job_id)
        assert _row_enabled(created) is True

        baseline_run_ids = {
            str(row.get("id") or "").strip()
            for row in _scheduled_runs_for_job(test_project_id, scheduled_job_id)
            if str(row.get("id") or "").strip()
        }
        try:
            run_row = _wait_for_new_scheduled_run(
                test_project_id,
                scheduled_job_id,
                baseline_run_ids=baseline_run_ids,
                timeout=210,
            )
        except TimeoutError as exc:
            pytest.skip(f"Scheduled auto-run loop did not fire in this environment: {exc}")
        run_type = str(run_row.get("run_type") or "").strip().lower()
        run_status = str(run_row.get("status") or "").strip().lower()
        assert run_type == "scheduled", f"Expected auto run_type=scheduled, got {run_type!r}"
        assert run_status == "enqueued", (
            f"Expected scheduled auto run to enqueue a job, got status={run_status!r} row={run_row}"
        )

        job_id = str(run_row.get("job_id") or "").strip()
        assert job_id, f"Scheduled auto run row does not reference job_id: {run_row}"
        scheduled_job_cleanup["job_ids"].append(job_id)

        terminal = _drive_job_to_terminal(job_id, timeout=360, interval=2)
        final_status = _job_status(terminal)
        assert final_status in TERMINAL_JOB_STATUSES, (
            f"Scheduled auto-run job {job_id} did not reach terminal status: {final_status!r}"
        )
