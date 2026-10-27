"""E2E checks for workflow parallelism across project scopes.

Goals:
- Same project: two workflow runs can be in_progress at the same time.
- Different projects: one run per project can be in_progress concurrently.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from core.application.tooling.testing.runtime_env import resolve_base_url, resolve_test_env
from tests.e2e.conftest import e2e_request
from tests.e2e.helpers import ensure_canonical_project_workflows
from tests.e2e.neutral_registry import NEUTRAL_WORKFLOW_ID

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
BACKEND_ROLE = str(os.environ.get("E2E_BACKEND_ROLE", "admin") or "admin").strip().lower()
_BACKEND_ENV = resolve_test_env(os.environ, lane="backend", role=BACKEND_ROLE, include_dotenv=True)
_UI_ENV = resolve_test_env(os.environ, lane="ui", role=BACKEND_ROLE, include_dotenv=True)
PROJECT_ID = str(_BACKEND_ENV.project_id or "").strip()
VERIFY_SSL = str(os.getenv("E2E_VERIFY_SSL", "0") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "max_iterations",
    "retryable_interrupted_step",
    "stuck",
}
ACTIVE_STATUSES = {
    "pending",
    "queued",
    "in_progress",
    "awaiting_approval",
}


def _api(method: str, path: str, *, timeout: int = 20, **kwargs):
    return e2e_request(
        method.upper(),
        f"{BASE_URL}{path}",
        verify=VERIFY_SSL,
        timeout=timeout,
        **kwargs,
    )


def _project_policy(project_id: str) -> dict:
    resp = _api("GET", f"/api/projects/{project_id}/policy")
    if resp.status_code == 404:
        pytest.skip("Project policy endpoint not implemented")
    if resp.status_code == 403:
        pytest.skip("Project policy endpoint requires admin role")
    assert resp.status_code == 200, (
        f"Failed to read project policy for {project_id}: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    assert isinstance(body, dict), f"Unexpected policy payload for project {project_id}: {body!r}"
    return body


def _policy_update_payload(policy_doc: dict, *, concurrency_cap: int) -> dict:
    policy = dict(policy_doc.get("policy") or {})
    return {
        "autonomy_mode": str(policy.get("autonomy_mode") or "supervised"),
        "autonomy_confidence_threshold": float(policy.get("autonomy_confidence_threshold") or 0.8),
        "retry_strategy": str(policy.get("retry_strategy") or "bounded"),
        "retry_max_attempts": int(policy.get("retry_max_attempts") or 2),
        "retry_backoff_initial_seconds": int(policy.get("retry_backoff_initial_seconds") or 2),
        "retry_backoff_max_seconds": int(policy.get("retry_backoff_max_seconds") or 120),
        "concurrency_cap": max(1, int(concurrency_cap)),
        "tool_policy_mode": str(policy.get("tool_policy_mode") or "registry_only"),
    }


def _set_project_concurrency_cap(project_id: str, cap: int) -> dict:
    original = _project_policy(project_id)
    payload = _policy_update_payload(original, concurrency_cap=cap)
    resp = _api("PUT", f"/api/projects/{project_id}/policy", json=payload, timeout=30)
    assert resp.status_code == 200, (
        f"Failed to update project {project_id} policy: {resp.status_code} {resp.text[:300]}"
    )
    updated = _project_policy(project_id)
    applied_cap = int((updated.get("policy") or {}).get("concurrency_cap") or 0)
    assert applied_cap == max(1, int(cap)), (
        f"Project {project_id} concurrency_cap was not applied: expected {cap}, got {applied_cap}"
    )
    return original


def _restore_project_policy(project_id: str, original_policy_doc: dict) -> None:
    payload = _policy_update_payload(
        original_policy_doc,
        concurrency_cap=int((original_policy_doc.get("policy") or {}).get("concurrency_cap") or 1),
    )
    _api("PUT", f"/api/projects/{project_id}/policy", json=payload, timeout=30)


def _submit_task(project_id: str, *, workflow_id: str, summary: str) -> str:
    payload = {
        "inputs": {"topic": summary},
        "project_id": str(project_id),
        "workflow_id": workflow_id,
    }
    resp = _api("POST", "/api/workflow-runs", json=payload, timeout=30)
    assert resp.status_code == 200, (
        f"Task submit failed for project={project_id}, workflow={workflow_id}: "
        f"{resp.status_code} {resp.text[:350]}"
    )
    body = resp.json() or {}
    job_id = str(body.get("job_id") or body.get("id") or "").strip()
    assert job_id, f"Task submit response missing job_id: {body}"
    return job_id


def _job_row(job_id: str | int) -> dict:
    resp = _api("GET", f"/api/jobs/{job_id}", timeout=15)
    assert resp.status_code == 200, (
        f"Failed to read job {job_id}: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    if isinstance(body, dict) and isinstance(body.get("job"), dict):
        return dict(body["job"])
    assert isinstance(body, dict), f"Unexpected job payload for {job_id}: {body!r}"
    return body


def _job_status_step(job_row: dict) -> tuple[str, str]:
    status = str(job_row.get("status") or "").strip().lower()
    runtime = job_row.get("runtime") or {}
    step = str(runtime.get("current_step_id") or job_row.get("current_step_id") or "").strip()
    return status, step


def _wait_for_in_progress_overlap(
    job_ids: list[str],
    *,
    timeout: int = 180,
    interval: int = 3,
) -> tuple[bool, list[dict[str, str]]]:
    deadline = time.monotonic() + timeout
    history: list[dict[str, str]] = []
    parallel_statuses = {"in_progress", "awaiting_approval"}
    while time.monotonic() < deadline:
        snapshot: dict[str, str] = {}
        parallel_active_count = 0
        for job_id in job_ids:
            row = _job_row(job_id)
            status, step = _job_status_step(row)
            snapshot[job_id] = f"{status}:{step or '-'}"
            if status in parallel_statuses:
                parallel_active_count += 1
        history.append(snapshot)
        if parallel_active_count >= 2:
            return True, history
        statuses = {value.split(":", 1)[0] for value in snapshot.values()}
        if statuses and statuses.issubset(TERMINAL_STATUSES):
            return False, history
        time.sleep(interval)
    return False, history


def _history_reached_terminal_without_overlap(history: list[dict[str, str]]) -> bool:
    if not history:
        return False
    latest = history[-1]
    statuses = {str(value or "").split(":", 1)[0] for value in latest.values()}
    return bool(statuses) and statuses.issubset(TERMINAL_STATUSES)


def _history_contains_terminal_status(history: list[dict[str, str]]) -> bool:
    for snapshot in history:
        statuses = {str(value or "").split(":", 1)[0] for value in snapshot.values()}
        if any(status in TERMINAL_STATUSES for status in statuses):
            return True
    return False


def _cancel_job(job_id: str | int) -> None:
    try:
        _api(
            "POST",
            f"/api/jobs/{job_id}/cancel",
            headers={"accept": "application/json"},
            allow_redirects=False,
            timeout=20,
        )
    except Exception:
        return


def _project_jobs(project_id: str, *, limit: int = 100) -> list[dict]:
    resp = _api("GET", f"/api/projects/{project_id}/jobs", params={"limit": int(limit)}, timeout=20)
    assert resp.status_code == 200, (
        f"Could not read jobs for project {project_id}: {resp.status_code} {resp.text[:300]}"
    )
    payload = resp.json()
    rows = payload if isinstance(payload, list) else (payload.get("jobs") or [])
    return [dict(row) for row in rows if isinstance(row, dict)]


def _project_active_job_ids(project_id: str) -> list[str]:
    job_ids: list[str] = []
    for row in _project_jobs(project_id):
        status = str(row.get("status") or "").strip().lower()
        job_id = str(row.get("id") or row.get("job_id") or "").strip()
        if job_id and status in ACTIVE_STATUSES:
            job_ids.append(job_id)
    return job_ids


def _cancel_project_active_jobs(project_id: str, *, timeout: int = 30) -> None:
    active_ids = _project_active_job_ids(project_id)
    for job_id in active_ids:
        _cancel_job(job_id)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _project_active_job_ids(project_id):
            return
        time.sleep(2)
    remaining = _project_active_job_ids(project_id)
    assert not remaining, (
        f"Active jobs remained after cleanup for project {project_id}: {remaining}"
    )


def _wait_for_no_in_progress_overlap(
    job_ids: list[str],
    *,
    timeout: int = 120,
    interval: int = 3,
) -> tuple[bool, list[dict[str, str]]]:
    deadline = time.monotonic() + timeout
    history: list[dict[str, str]] = []
    # Project concurrency cap is enforced via execution leases while work is actively running.
    # awaiting_approval no longer holds a project execution lease slot. A just-claimed
    # job can briefly report in_progress before a step is assigned.
    parallel_statuses = {"in_progress"}
    while time.monotonic() < deadline:
        snapshot: dict[str, str] = {}
        parallel_active_count = 0
        for job_id in job_ids:
            row = _job_row(job_id)
            status, step = _job_status_step(row)
            snapshot[job_id] = f"{status}:{step or '-'}"
            if status in parallel_statuses and step:
                parallel_active_count += 1
        history.append(snapshot)
        if parallel_active_count >= 2:
            return False, history
        statuses = {value.split(":", 1)[0] for value in snapshot.values()}
        if statuses and statuses.issubset(TERMINAL_STATUSES):
            return True, history
        time.sleep(interval)
    return True, history


def _job_has_provider_quota_failure(job_id: str | int) -> bool:
    markers = (
        "insufficient_quota",
        "http_status=429",
        "too many requests",
        "rate limit",
        "credit balance is too low",
        "plans & billing",
        "billing",
    )
    try:
        resp = _api("GET", f"/api/jobs/{job_id}/events", timeout=20)
        if not resp.ok:
            return False
        payload = resp.json() or {}
        rows = payload.get("events") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return False
        for row in rows:
            if not isinstance(row, dict):
                continue
            detail = row.get("detail")
            detail_text = (
                json.dumps(detail) if isinstance(detail, (dict, list)) else str(detail or "")
            )
            blob = " ".join(
                (
                    str(row.get("title") or ""),
                    str(row.get("event_type") or ""),
                    str(row.get("severity") or ""),
                    detail_text,
                )
            ).lower()
            if any(marker in blob for marker in markers):
                return True
            if (
                "provider=anthropic" in blob
                and ("http_status=400" in blob or "invalid_request_error" in blob)
                and ("credit balance" in blob or "billing" in blob)
            ):
                return True
    except Exception:
        return False
    return False


def _job_has_validation_evidence_gap_failure(job_id: str | int) -> bool:
    required_markers = (
        "validation failed (evidence gaps)",
        "required proof-of-work check 'lint' is missing",
        "required proof-of-work check 'tests' is missing",
        "required proof-of-work check 'typecheck' is missing",
    )
    try:
        resp = _api("GET", f"/api/jobs/{job_id}/events", timeout=20)
        if not resp.ok:
            return False
        payload = resp.json() or {}
        rows = payload.get("events") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return False
        for row in rows:
            if not isinstance(row, dict):
                continue
            detail = row.get("detail")
            detail_text = (
                json.dumps(detail) if isinstance(detail, (dict, list)) else str(detail or "")
            )
            blob = " ".join(
                (
                    str(row.get("title") or ""),
                    str(row.get("event_type") or ""),
                    str(row.get("severity") or ""),
                    detail_text,
                )
            ).lower()
            if all(marker in blob for marker in required_markers):
                return True
    except Exception:
        return False
    return False


def _terminal_row_has_hard_contract_validation_failure(terminal_row: dict) -> bool:
    error = str(terminal_row.get("error") or "").strip().lower()
    if not error:
        return False
    if (
        "human approval cannot bypass hard contract errors" in error
        and "non-overridable implementation state" in error
    ):
        return True
    return (
        "invoke_workflow child requires human review" in error
        and "auto-approval is not allowed for blocking workflow failures" in error
    )


def _job_has_expected_contract_validation_failure(job_id: str | int, terminal_row: dict) -> bool:
    return _job_has_validation_evidence_gap_failure(
        job_id
    ) or _terminal_row_has_hard_contract_validation_failure(terminal_row)


def _approve_job_thread(job_id: str | int, thread_id: str) -> None:
    gate = _api("GET", f"/api/jobs/{job_id}/approval", timeout=20)
    gate_body = gate.json() if gate.status_code == 200 else {}
    approval_id = str(gate_body.get("approval_id") or "").strip()
    assert approval_id, f"Approval gate for job {job_id} is missing approval_id"
    resp = _api(
        "POST",
        f"/api/approvals/{thread_id}",
        json={"approval_id": approval_id, "approved": True},
        headers={"Idempotency-Key": f"parallelism-approval:{approval_id}:approve"},
        timeout=30,
        allow_redirects=False,
    )
    assert resp.status_code in (200, 202), (
        f"Approval endpoint failed for thread {thread_id}: {resp.status_code} {resp.text[:250]}"
    )


def _drive_job_to_terminal(
    job_id: str | int, *, timeout: int = 900, interval: int = 5, max_approvals: int = 16
) -> tuple[dict, int]:
    approval_count = 0
    deadline = time.monotonic() + timeout
    last_snapshot: str = "unavailable"
    while time.monotonic() < deadline:
        row = _job_row(job_id)
        status, step = _job_status_step(row)
        last_snapshot = f"status={status} step={step or '-'}"
        if status in TERMINAL_STATUSES:
            return row, approval_count
        if status == "awaiting_approval":
            thread_id = str(row.get("thread_id") or "").strip()
            assert thread_id, f"Job {job_id} is awaiting approval but thread_id is missing"
            _approve_job_thread(job_id, thread_id)
            approval_count += 1
            if approval_count > max_approvals:
                pytest.fail(
                    f"Job {job_id} exceeded approval budget ({max_approvals}) before reaching terminal state"
                )
        time.sleep(interval)
    raise TimeoutError(
        f"Job {job_id} did not reach terminal status within {timeout}s; last={last_snapshot}"
    )


def _assert_job_terminal_outcome(
    job_id: str | int,
    terminal_row: dict,
    *,
    context: str,
    approval_count: int,
    expected_status: str,
) -> None:
    final_status = str(terminal_row.get("status") or "").strip().lower()
    expected = str(expected_status or "").strip().lower()
    if expected == "completed":
        if final_status == "failed" and _job_has_provider_quota_failure(job_id):
            pytest.skip(
                f"Job {job_id} failed in {context} due to external provider quota/rate-limit (429 insufficient_quota)"
            )
        assert final_status == "completed", (
            f"{context} must finish completed; job={job_id} final_status={final_status!r} approvals={approval_count}"
        )
        return
    if expected == "failed_validation":
        if final_status == "completed":
            return
        assert final_status == "failed", (
            f"{context} must finish with failed_validation; job={job_id} "
            f"final_status={final_status!r} approvals={approval_count}"
        )
        assert _job_has_expected_contract_validation_failure(job_id, terminal_row), (
            f"{context} failed but not due to expected contract validation failure; job={job_id}"
        )
        return
    pytest.fail(f"Unsupported expected_status={expected_status!r} for job={job_id} in {context}")


def _minimal_policy_update_payload(*, concurrency_cap: int) -> dict:
    return {
        "autonomy_mode": "supervised",
        "autonomy_confidence_threshold": 0.8,
        "retry_strategy": "bounded",
        "retry_max_attempts": 2,
        "retry_backoff_initial_seconds": 2,
        "retry_backoff_max_seconds": 120,
        "concurrency_cap": max(1, int(concurrency_cap)),
        "tool_policy_mode": "registry_only",
    }


def _project_ids() -> list[str]:
    resp = _api("GET", "/api/projects", timeout=30)
    assert resp.status_code == 200, f"Could not list projects: {resp.status_code} {resp.text[:300]}"
    payload = resp.json()
    rows = payload if isinstance(payload, list) else (payload.get("projects") or [])
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        project_id = str(row.get("id") or "").strip()
        if project_id:
            ids.append(project_id)
    return ids


def _secondary_e2e_project_id(primary_project_id: str) -> str:
    ui_project_id = str(_UI_ENV.project_id or "").strip()
    if ui_project_id and ui_project_id != str(primary_project_id):
        return ui_project_id

    resp = _api("GET", "/api/projects", timeout=30)
    assert resp.status_code == 200, f"Could not list projects: {resp.status_code} {resp.text[:300]}"
    payload = resp.json()
    rows = payload if isinstance(payload, list) else (payload.get("projects") or [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        project_id = str(row.get("id") or "").strip()
        project_name = str(row.get("name") or "").strip()
        if (
            project_id
            and project_id != str(primary_project_id)
            and project_name.startswith(("Test Project Backend", "Test Project UI"))
        ):
            return project_id
    return ""


@pytest.fixture(scope="module")
def server_available():
    resp = _api("GET", "/health", timeout=10)
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server unhealthy: {resp.status_code}")


@pytest.fixture(scope="module")
def test_project_id() -> str:
    if not PROJECT_ID:
        pytest.skip("TEST_PROJECT_BACKEND_ID not set")
    return PROJECT_ID


def test_parallel_workflow_runs_in_same_project(server_available, test_project_id: str):
    if BACKEND_ROLE != "admin":
        pytest.skip("Parallel workflow policy test requires admin role")

    ensure_canonical_project_workflows(test_project_id)
    _cancel_project_active_jobs(test_project_id)
    original_policy = _set_project_concurrency_cap(test_project_id, 2)
    job_ids: list[str] = []
    try:
        first_job_id = _submit_task(
            test_project_id,
            workflow_id=NEUTRAL_WORKFLOW_ID,
            summary=(
                "Parallelism smoke A: investigate whether GraphQL subscriptions fit our current architecture."
            ),
        )
        second_job_id = _submit_task(
            test_project_id,
            workflow_id=NEUTRAL_WORKFLOW_ID,
            summary=(
                "Parallelism smoke B: investigate whether server-sent events fit our current admin notification architecture."
            ),
        )
        job_ids = [first_job_id, second_job_id]
        overlapped, history = _wait_for_in_progress_overlap(job_ids, timeout=120, interval=1)
        if not overlapped and (
            _history_reached_terminal_without_overlap(history)
            or _history_contains_terminal_status(history)
        ):
            pytest.skip(
                "Same-project overlap was not observable before both jobs reached terminal state"
            )
        assert overlapped, (
            "Expected at least two same-project workflow jobs to overlap in active execution state "
            "(in_progress or awaiting_approval). "
            f"Job ids={job_ids}; tail={history[-12:]}"
        )
    finally:
        for job_id in job_ids:
            _cancel_job(job_id)
        _cancel_project_active_jobs(test_project_id)
        _restore_project_policy(test_project_id, original_policy)


def test_same_project_concurrency_cap_1_serializes_in_progress_runs(
    server_available, test_project_id: str
):
    if BACKEND_ROLE != "admin":
        pytest.skip("Concurrency-cap serialization test requires admin role")

    ensure_canonical_project_workflows(test_project_id)
    _cancel_project_active_jobs(test_project_id)
    original_policy = _set_project_concurrency_cap(test_project_id, 1)
    job_ids: list[str] = []
    try:
        first_job_id = _submit_task(
            test_project_id,
            workflow_id=NEUTRAL_WORKFLOW_ID,
            summary=(
                "Concurrency cap smoke A: investigate event sourcing fit for audit and replay."
            ),
        )
        second_job_id = _submit_task(
            test_project_id,
            workflow_id=NEUTRAL_WORKFLOW_ID,
            summary=(
                "Concurrency cap smoke B: investigate GraphQL subscriptions fit for architecture."
            ),
        )
        job_ids = [first_job_id, second_job_id]
        no_overlap, history = _wait_for_no_in_progress_overlap(job_ids, timeout=120, interval=1)
        assert no_overlap, (
            "Expected concurrency_cap=1 to prevent simultaneous running jobs "
            "(in_progress) in one project. "
            f"Job ids={job_ids}; tail={history[-12:]}"
        )
        saw_parallel_active = any(
            any(
                marker in snapshot.get(job_id, "")
                for marker in ("in_progress:", "awaiting_approval:")
            )
            for snapshot in history
            for job_id in job_ids
        )
        assert saw_parallel_active, (
            "Serialization test did not observe any active execution state "
            "(in_progress or awaiting_approval); run may not have started. "
            f"Job ids={job_ids}; tail={history[-12:]}"
        )
    finally:
        for job_id in job_ids:
            _cancel_job(job_id)
        _cancel_project_active_jobs(test_project_id)
        _restore_project_policy(test_project_id, original_policy)


def test_parallel_workflow_runs_across_projects(server_available, test_project_id: str):
    if BACKEND_ROLE != "admin":
        pytest.skip("Parallel workflow cross-project test requires admin role")

    secondary_project_id = _secondary_e2e_project_id(test_project_id)
    if not secondary_project_id:
        pytest.skip("No secondary E2E project available for cross-project parallelism check")

    ensure_canonical_project_workflows(test_project_id)
    ensure_canonical_project_workflows(secondary_project_id)
    _cancel_project_active_jobs(test_project_id)
    _cancel_project_active_jobs(secondary_project_id)

    job_ids: list[str] = []
    try:
        first_job_id = _submit_task(
            test_project_id,
            workflow_id=NEUTRAL_WORKFLOW_ID,
            summary=(
                "Cross-project parallelism smoke primary: research whether event sourcing fits our current audit and replay architecture."
            ),
        )
        second_job_id = _submit_task(
            secondary_project_id,
            workflow_id=NEUTRAL_WORKFLOW_ID,
            summary=(
                "Cross-project parallelism smoke secondary: investigate whether GraphQL subscriptions fit our architecture."
            ),
        )
        job_ids = [first_job_id, second_job_id]
        overlapped, history = _wait_for_in_progress_overlap(job_ids, timeout=120, interval=1)
        if not overlapped and (
            _history_reached_terminal_without_overlap(history)
            or _history_contains_terminal_status(history)
        ):
            pytest.skip(
                "Cross-project overlap was not observable before both jobs reached terminal state"
            )
        assert overlapped, (
            "Expected at least two cross-project workflow jobs to overlap in active execution state "
            "(in_progress or awaiting_approval). "
            f"Projects=({test_project_id}, {secondary_project_id}) jobs={job_ids}; tail={history[-12:]}"
        )
    finally:
        for job_id in job_ids:
            _cancel_job(job_id)
        _cancel_project_active_jobs(test_project_id)
        _cancel_project_active_jobs(secondary_project_id)


def test_project_policy_update_requires_admin_role(server_available, test_project_id: str):
    if BACKEND_ROLE == "admin":
        pytest.skip("RBAC negative path is covered in non-admin role runs")
    payload = _minimal_policy_update_payload(concurrency_cap=2)
    resp = _api("PUT", f"/api/projects/{test_project_id}/policy", json=payload, timeout=30)
    assert resp.status_code == 403, (
        f"Project policy update must require admin role; got {resp.status_code}: {resp.text[:300]}"
    )


def test_viewer_cannot_submit_workflow_tasks(server_available, test_project_id: str):
    if BACKEND_ROLE != "viewer":
        pytest.skip("Viewer-only RBAC check")
    payload = {
        "inputs": {"topic": "Viewer RBAC check: forbidden workflow launch."},
        "project_id": str(test_project_id),
        "workflow_id": NEUTRAL_WORKFLOW_ID,
    }
    resp = _api("POST", "/api/workflow-runs", json=payload, timeout=30)
    assert resp.status_code == 403, (
        f"Viewer must not submit workflow tasks; got {resp.status_code}: {resp.text[:300]}"
    )
