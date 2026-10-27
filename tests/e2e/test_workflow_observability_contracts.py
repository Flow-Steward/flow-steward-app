"""E2E contracts for workflow observability and lifecycle safety.

Covers:
- soft-delete vs hard-delete behavior for jobs and observability rows;
- stream ordering + pagination invariants for events/messages/logs;
- restart and interrupted-step retry lifecycle endpoints.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
import requests

from core.application.auth.config import DEFAULT_WORKSPACE_ID
from core.application.tooling.testing.runtime_env import (
    _read_dotenv,
    resolve_base_url,
    resolve_test_env,
)
from tests.e2e.conftest import e2e_request
from tests.e2e.neutral_registry import NEUTRAL_WORKFLOW_ID, NEUTRAL_WORKFLOW_IDS
from tests.e2e.retry_step_helpers import (
    cleanup_job as cleanup_retryable_job,
)
from tests.e2e.retry_step_helpers import (
    runtime_journal as retry_runtime_journal,
)
from tests.e2e.retry_step_helpers import (
    seed_retryable_interrupted_step_job,
)

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

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
APPROVAL_GATE_TIMEOUT = int(os.getenv("E2E_APPROVAL_GATE_TIMEOUT", "300"))
QUEUE_DISPATCH_TIMEOUT = int(os.getenv("E2E_QUEUE_DISPATCH_TIMEOUT", "900"))
WORKFLOW_COMPLETION_TIMEOUT = int(os.getenv("E2E_WORKFLOW_STATUS_TIMEOUT", "900"))
WORKFLOW_ACTIVITY_TIMEOUT = int(os.getenv("E2E_WORKFLOW_ACTIVITY_TIMEOUT", "240"))
CANONICAL_WORKFLOW_IDS = NEUTRAL_WORKFLOW_IDS


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


TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "max_iterations",
    "retryable_interrupted_step",
    "stuck",
}


def _approval_needed(row: dict[str, Any]) -> bool:
    status = _job_status(row)
    if status == "awaiting_approval":
        return True
    current_agent = str(row.get("current_agent") or "").strip().lower()
    if current_agent != "human_approval_gate":
        return False
    runtime = row.get("runtime") or {}
    if not isinstance(runtime, dict):
        runtime = {}
    decision_detail = (runtime.get("last_routing_decision") or {}).get("detail") or {}
    failure_class = str(decision_detail.get("failure_class") or "").strip().lower()
    wait_reason = str(runtime.get("wait_reason") or "").strip().lower()
    return failure_class == "human_blocked" or "human_approval_gate" in wait_reason


ACTIVE_STATUSES = {"pending", "queued", "in_progress", "awaiting_approval"}
TRANSIENT_GATEWAY_STATUSES = {502, 503, 504}
QUEUE_WAIT_STATUSES = {"pending", "queued"}


def _api(method: str, path: str, *, timeout: int = 20, **kwargs):
    return e2e_request(
        method.upper(),
        f"{BASE_URL}{path}",
        verify=VERIFY_SSL,
        timeout=timeout,
        **kwargs,
    )


def _workflow_contract_error_preview(workflow_id: str) -> str:
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        return ""
    resp = _api("GET", f"/api/workflows/{workflow_id}/contract-validation", timeout=20)
    if resp.status_code != 200:
        return ""
    payload = resp.json() or {}
    validation = payload.get("validation") if isinstance(payload, dict) else {}
    errors = (validation or {}).get("errors") if isinstance(validation, dict) else []
    if not isinstance(errors, list):
        return ""
    for issue in errors:
        if not isinstance(issue, dict):
            continue
        message = str(issue.get("message") or "").strip()
        if message:
            return message
    return ""


def _get_json_with_retry(path: str, *, timeout: int, label: str, attempts: int = 5) -> Any:
    last_response = None
    for attempt in range(max(1, int(attempts))):
        resp = _api("GET", path, timeout=timeout)
        last_response = resp
        if resp.status_code == 200:
            return resp.json() or {}
        if resp.status_code not in TRANSIENT_GATEWAY_STATUSES or attempt >= attempts - 1:
            break
        time.sleep(min(3, 1 + attempt))
    assert last_response is not None
    assert last_response.status_code == 200, (
        f"{label}: {last_response.status_code} {last_response.text[:300]}"
    )
    return {}


def _extract_rows(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get(key)
        if not isinstance(rows, list):
            return []
        return [dict(row) for row in rows if isinstance(row, dict)]
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    return []


def _extract_row_id(row: dict[str, Any], *, fallback_key: str = "") -> str:
    value = row.get("id")
    if value in (None, "") and fallback_key:
        value = row.get(fallback_key)
    return str(value or "").strip()


def _job_row(job_id: str | int) -> dict[str, Any]:
    body = _get_json_with_retry(
        f"/api/jobs/{job_id}",
        timeout=20,
        label=f"Could not read job {job_id}",
    )
    if isinstance(body, dict) and isinstance(body.get("job"), dict):
        return dict(body["job"])
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


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _job_progress_marker(job_row: dict[str, Any]) -> tuple[str, str, str, str]:
    runtime = job_row.get("runtime") or {}
    if not isinstance(runtime, dict):
        runtime = {}
    return (
        _job_status(job_row),
        str(runtime.get("current_step_id") or job_row.get("current_step_id") or "").strip().lower(),
        str(job_row.get("current_agent") or "").strip().lower(),
        str(runtime.get("wait_reason") or "").strip().lower(),
    )


def _descendant_job_progress_marker(job_row: dict[str, Any]) -> str:
    project_id = str(job_row.get("project_id") or "").strip()
    parent_task_id = str(job_row.get("task_id") or "").strip()
    if not project_id or not parent_task_id:
        return ""
    child_prefix = f"{parent_task_id}:invoke:"
    markers: list[str] = []
    for row in _project_jobs(project_id, page=1, per_page=200):
        task_id = str(row.get("task_id") or "").strip()
        if not task_id.startswith(child_prefix):
            continue
        markers.append(
            ":".join(
                (
                    str(_extract_row_id(row)),
                    _job_status(row),
                    str(row.get("current_step_id") or "").strip().lower(),
                    str(row.get("current_agent") or "").strip().lower(),
                )
            )
        )
    return "|".join(sorted(markers))


def _project_jobs(project_id: str, *, page: int = 1, per_page: int = 100) -> list[dict[str, Any]]:
    resp = _api(
        "GET",
        f"/api/projects/{project_id}/jobs",
        params={"page": max(1, int(page)), "per_page": max(1, int(per_page))},
        timeout=25,
    )
    assert resp.status_code == 200, (
        f"Could not list jobs for project {project_id}: {resp.status_code} {resp.text[:300]}"
    )
    payload = resp.json() or {}
    return _extract_rows(payload, "jobs")


def _project_job_ids(project_id: str, *, page: int = 1, per_page: int = 100) -> list[str]:
    ids: list[str] = []
    for row in _project_jobs(project_id, page=page, per_page=per_page):
        job_id = _extract_row_id(row)
        if job_id:
            ids.append(job_id)
    return ids


def _find_job_id_by_task_id(project_id: str, task_id: str, *, timeout: int = 30) -> str:
    task_id = str(task_id or "").strip()
    if not task_id:
        return ""
    deadline = time.monotonic() + max(1, int(timeout))
    while time.monotonic() < deadline:
        for row in _project_jobs(project_id, page=1, per_page=200):
            if str(row.get("task_id") or "").strip() != task_id:
                continue
            job_id = _extract_row_id(row)
            if job_id:
                return job_id
        time.sleep(1)
    return ""


def _project_detail(project_id: str) -> dict[str, Any]:
    body = _get_json_with_retry(
        f"/api/projects/{project_id}",
        timeout=20,
        label=f"Failed to read project detail for {project_id}",
    )
    assert isinstance(body, dict), f"Unexpected project detail payload for {project_id}: {body!r}"
    return body


def _project_workflows(project_id: str) -> dict[str, Any]:
    body = _get_json_with_retry(
        f"/api/projects/{project_id}/workflows",
        timeout=20,
        label=f"Failed to read project workflows for {project_id}",
    )
    assert isinstance(body, dict), (
        f"Unexpected project workflows payload for {project_id}: {body!r}"
    )
    return body


def _ensure_project_workflow_linked(project_id: str, workflow_id: str) -> None:
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        return
    workflows_payload = _project_workflows(project_id)
    workflow_rows = workflows_payload.get("project_workflows") or []
    workflow_ids = [
        str(row.get("workflow_id") or "").strip()
        for row in workflow_rows
        if isinstance(row, dict) and str(row.get("workflow_id") or "").strip()
    ]
    workflow_enabled = any(
        isinstance(row, dict)
        and str(row.get("workflow_id") or "").strip() == workflow_id
        and bool(row.get("enabled", True))
        for row in workflow_rows
    )
    if workflow_enabled:
        return
    workflow_ids.append(workflow_id)
    default_workflow_id = str(
        workflows_payload.get("operator_default_workflow_id") or workflow_id
    ).strip()
    resp = _api(
        "PUT",
        f"/api/projects/{project_id}/workflows",
        json={
            "workflow_ids": workflow_ids,
            "operator_default_workflow_id": default_workflow_id,
        },
        timeout=30,
    )
    assert resp.status_code == 200, (
        f"Failed to link workflow {workflow_id!r} to project {project_id}: "
        f"{resp.status_code} {resp.text[:300]}"
    )


def _project_policy(project_id: str) -> dict[str, Any]:
    resp = _api("GET", f"/api/projects/{project_id}/policy", timeout=20)
    if resp.status_code == 403:
        pytest.skip("Project policy update requires admin role")
    assert resp.status_code == 200, (
        f"Failed to read project policy for {project_id}: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    assert isinstance(body, dict), f"Unexpected project policy payload: {body!r}"
    return body


def _project_policy_update_payload(
    policy_doc: dict[str, Any],
    *,
    autonomy_mode: str | None = None,
    autonomy_confidence_threshold: float | None = None,
) -> dict[str, Any]:
    policy = dict(policy_doc.get("policy") or {})
    return {
        "autonomy_mode": autonomy_mode or str(policy.get("autonomy_mode") or "supervised"),
        "autonomy_confidence_threshold": float(
            autonomy_confidence_threshold
            if autonomy_confidence_threshold is not None
            else policy.get("autonomy_confidence_threshold") or 0.8
        ),
        "retry_strategy": str(policy.get("retry_strategy") or "bounded"),
        "retry_max_attempts": int(policy.get("retry_max_attempts") or 2),
        "retry_backoff_initial_seconds": int(policy.get("retry_backoff_initial_seconds") or 2),
        "retry_backoff_max_seconds": int(policy.get("retry_backoff_max_seconds") or 120),
        "concurrency_cap": max(1, int(policy.get("concurrency_cap") or 1)),
        "tool_policy_mode": str(policy.get("tool_policy_mode") or "registry_only"),
    }


def _set_project_policy(
    project_id: str,
    *,
    autonomy_mode: str,
    autonomy_confidence_threshold: float,
) -> dict[str, Any]:
    original = _project_policy(project_id)
    payload = _project_policy_update_payload(
        original,
        autonomy_mode=autonomy_mode,
        autonomy_confidence_threshold=autonomy_confidence_threshold,
    )
    resp = _api("PUT", f"/api/projects/{project_id}/policy", json=payload, timeout=30)
    if resp.status_code == 403:
        pytest.skip("Project policy update requires admin role")
    assert resp.status_code == 200, (
        f"Failed to update project policy for {project_id}: {resp.status_code} {resp.text[:300]}"
    )
    applied = _project_policy(project_id)
    applied_policy = applied.get("policy") or {}
    assert str(applied_policy.get("autonomy_mode") or "").strip() == autonomy_mode
    return original


def _restore_project_policy(project_id: str, original_policy_doc: dict[str, Any]) -> None:
    payload = _project_policy_update_payload(original_policy_doc)
    with contextlib.suppress(Exception):
        _api("PUT", f"/api/projects/{project_id}/policy", json=payload, timeout=30)


def _create_isolated_project_clone(source_project_id: str) -> str:
    if BACKEND_ROLE != "admin":
        pytest.skip("Isolated workflow project cloning requires admin role")

    source_detail = _project_detail(source_project_id)
    source_workflows = _project_workflows(source_project_id)
    source_policy = _project_policy(source_project_id)

    project_name = f"workflow-isolated-{uuid.uuid4().hex[:8]}"
    workspace_id = str(source_detail.get("workspace_id") or DEFAULT_WORKSPACE_ID).strip()
    create_payload = {
        "name": project_name,
        "workspace_id": workspace_id,
        "project_rules": str(source_detail.get("project_rules") or ""),
    }
    create_resp = _api("POST", "/api/projects", json=create_payload, timeout=30)
    assert create_resp.status_code in (200, 201), (
        f"Failed to create isolated project clone from {source_project_id}: "
        f"{create_resp.status_code} {create_resp.text[:300]}"
    )
    create_body = create_resp.json() or {}
    project_id = str(create_body.get("id") or create_body.get("project_id") or "").strip()
    assert project_id, f"Create project response missing id: {create_body!r}"

    source_default = str(
        source_workflows.get("operator_default_workflow_id") or NEUTRAL_WORKFLOW_ID
    ).strip()
    if source_default not in CANONICAL_WORKFLOW_IDS:
        source_default = NEUTRAL_WORKFLOW_ID
    unresolved_workflows: list[str] = []
    for workflow_id in CANONICAL_WORKFLOW_IDS:
        preview = _workflow_contract_error_preview(workflow_id)
        if preview:
            unresolved_workflows.append(f"{workflow_id}: {preview}")
    preview_text = "; ".join(unresolved_workflows[:4])
    if unresolved_workflows:
        pytest.skip(
            "Workflow observability contracts require contract-ready neutral workflows; "
            f"unresolved validation errors detected ({preview_text})"
        )
    workflow_resp = _api(
        "PUT",
        f"/api/projects/{project_id}/workflows",
        json={
            "workflow_ids": list(CANONICAL_WORKFLOW_IDS),
            "operator_default_workflow_id": source_default,
        },
        timeout=30,
    )
    assert workflow_resp.status_code == 200, (
        f"Failed to copy workflows onto isolated project {project_id}: "
        f"{workflow_resp.status_code} {workflow_resp.text[:300]}"
    )

    policy_resp = _api(
        "PUT",
        f"/api/projects/{project_id}/policy",
        json=_project_policy_update_payload(source_policy),
        timeout=30,
    )
    assert policy_resp.status_code == 200, (
        f"Failed to copy policy onto isolated project {project_id}: "
        f"{policy_resp.status_code} {policy_resp.text[:300]}"
    )
    return project_id


def _cleanup_project(project_id: str) -> None:
    project_id = str(project_id or "").strip()
    if not project_id:
        return
    for job_id in _project_job_ids(project_id, page=1, per_page=200):
        with contextlib.suppress(Exception):
            _cancel_job(job_id)
        with contextlib.suppress(Exception):
            _api("DELETE", f"/api/jobs/{job_id}", timeout=20)
        if BACKEND_ROLE == "admin":
            with contextlib.suppress(Exception):
                _hard_delete_job(job_id)
    with contextlib.suppress(Exception):
        _api("DELETE", f"/api/projects/{project_id}", timeout=20)


def _cancel_active_jobs(project_id: str, *, timeout: int = 60) -> None:
    project_id = str(project_id or "").strip()
    if not project_id:
        return
    deadline = time.monotonic() + max(1, int(timeout))
    while time.monotonic() < deadline:
        active_ids: list[str] = []
        for row in _project_jobs(project_id, page=1, per_page=200):
            job_id = _extract_row_id(row)
            status = str(row.get("status") or "").strip().lower()
            if job_id and status in ACTIVE_STATUSES:
                active_ids.append(job_id)
        if not active_ids:
            return
        for job_id in active_ids:
            with contextlib.suppress(Exception):
                _cancel_job(job_id)
        time.sleep(2)


def _submit_test_mode_job(
    project_id: str,
    *,
    summary: str,
    workflow_id: str = NEUTRAL_WORKFLOW_ID,
) -> str:
    idempotency_key = f"e2e-observability-{uuid.uuid4().hex[:10]}"
    payload = {
        "inputs": {"topic": summary},
        "project_id": str(project_id),
        "test_mode": True,
    }
    if str(workflow_id or "").strip():
        payload["workflow_id"] = str(workflow_id).strip()
        _ensure_project_workflow_linked(str(project_id), str(workflow_id).strip())

    last_response = None
    for attempt in range(1, 4):
        try:
            resp = _api(
                "POST",
                "/api/workflow-runs",
                json=payload,
                headers={"Idempotency-Key": idempotency_key},
                timeout=30,
            )
            last_response = resp
        except requests.exceptions.ReadTimeout:
            if attempt < 3:
                time.sleep(attempt)
                continue
            raise

        if resp.status_code == 200:
            body = resp.json() or {}
            job_id = str(body.get("job_id") or body.get("id") or 0).strip()
            assert job_id, f"Task submit response missing job_id: {body}"
            return job_id
        if resp.status_code in TRANSIENT_GATEWAY_STATUSES and attempt < 3:
            time.sleep(attempt)
            continue
        break

    assert last_response is not None
    assert last_response.status_code == 200, (
        f"Task submit failed: {last_response.status_code} {last_response.text[:350]}"
    )
    body = last_response.json() or {}
    job_id = str(body.get("job_id") or body.get("id") or 0).strip()
    assert job_id, f"Task submit response missing job_id: {body}"
    return job_id


def _approve_job(job_id: str | int, thread_id: str) -> None:
    approval_id = str(_approval_gate(job_id).get("approval_id") or "").strip()
    assert approval_id, f"Approval gate for job {job_id} is missing approval_id"
    resp = _api(
        "POST",
        f"/api/approvals/{thread_id}",
        json={"approval_id": approval_id, "approved": True},
        headers={"Idempotency-Key": f"observability-approval:{approval_id}:approve"},
        timeout=30,
        allow_redirects=False,
    )
    assert resp.status_code in (200, 202), (
        f"Approval failed for thread {thread_id}: {resp.status_code} {resp.text[:250]}"
    )


def _submit_approval_decision(
    job_id: str | int,
    thread_id: str,
    *,
    approved: bool | None = None,
    comment: str = "",
    action: str | None = None,
    override_content: str = "",
) -> dict[str, Any]:
    approval_id = str(_approval_gate(job_id).get("approval_id") or "").strip()
    assert approval_id, f"Approval gate for job {job_id} is missing approval_id"
    payload: dict[str, Any] = {"approval_id": approval_id}
    if approved is not None:
        payload["approved"] = bool(approved)
    if comment:
        payload["comment"] = comment
    if action is not None:
        payload["action"] = str(action)
    if override_content:
        payload["override_content"] = override_content

    resp = _api(
        "POST",
        f"/api/approvals/{thread_id}",
        json=payload,
        headers={"Idempotency-Key": f"observability-approval:{approval_id}:{action or approved}"},
        timeout=30,
        allow_redirects=False,
    )
    assert resp.status_code in (200, 202), (
        f"Approval decision failed for thread {thread_id}: {resp.status_code} {resp.text[:250]}"
    )
    if not resp.content:
        return {}
    body = resp.json() or {}
    assert isinstance(body, dict), f"Unexpected approval decision payload: {body!r}"
    return body


def _approval_gate(job_id: str | int) -> dict[str, Any]:
    body = _get_json_with_retry(
        f"/api/jobs/{job_id}/approval",
        timeout=20,
        label=f"Could not read approval gate for job {job_id}",
    )
    payload = body.get("payload") if isinstance(body, dict) else {}
    assert isinstance(payload, dict), f"Unexpected approval gate payload for job {job_id}: {body!r}"
    merged = dict(payload)
    merged.setdefault("approval_id", body.get("approval_id"))
    merged.setdefault("thread_id", body.get("thread_id"))
    return merged


def _approval_gate_payload_ready(payload: Mapping[str, Any]) -> bool:
    if not isinstance(payload, Mapping) or not payload:
        return False
    content = str(payload.get("content") or "").strip()
    message = str(payload.get("message") or "").strip()
    if content == "Approval details are loading. If this persists, refresh the page.":
        return False
    return message != "Awaiting your approval." or any(
        payload.get(key) for key in ("gate_kind", "technical_details")
    )


def _approval_gate_signature_or_none(job_id: str | int) -> tuple[str, str, str] | None:
    resp = _api("GET", f"/api/jobs/{job_id}/approval", timeout=20)
    if resp.status_code == 409:
        body = resp.json() if resp.content else {}
        message = str((body or {}).get("message") or "").strip().lower()
        if "not awaiting approval" in message:
            return None
    assert resp.status_code == 200, (
        f"Could not read approval gate for job {job_id}: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    payload = body.get("payload") if isinstance(body, dict) else {}
    assert isinstance(payload, dict), f"Unexpected approval gate payload for job {job_id}: {body!r}"
    return (
        _normalize_approval_gate_text(payload.get("stage")),
        _normalize_approval_gate_text(payload.get("question") or payload.get("message")),
        _normalize_approval_gate_text(payload.get("content")),
    )


def _approval_history(job_id: str | int) -> list[dict[str, Any]]:
    payload = _get_json_with_retry(
        f"/api/jobs/{job_id}/approvals",
        timeout=20,
        label=f"Could not read approval history for job {job_id}",
    )
    return _extract_rows(payload, "approvals")


def _normalize_approval_gate_text(value: Any) -> str:
    text = str(value or "").replace("\\r\\n", "\n").replace("\\n", "\n")
    return " ".join(part for part in text.split())


def _approval_gate_signature(job_id: str | int) -> tuple[str, str, str]:
    signature = _approval_gate_signature_or_none(job_id)
    assert signature is not None, f"Job {job_id} is not awaiting approval"
    return signature


def _wait_for_approval_gate(
    job_id: str | int,
    *,
    timeout: int = APPROVAL_GATE_TIMEOUT,
    interval: int = 2,
) -> dict[str, Any]:
    queue_deadline = time.monotonic() + max(timeout, QUEUE_DISPATCH_TIMEOUT)
    gate_deadline: float | None = None
    last_status = "unknown"
    last_row: dict[str, Any] | None = None
    while True:
        now = time.monotonic()
        if gate_deadline is not None and now >= gate_deadline:
            break
        row = _job_row(job_id)
        last_row = row
        last_status = _job_status(row) or "unknown"
        if _approval_needed(row):
            with contextlib.suppress(Exception):
                payload = _approval_gate(job_id)
                if _approval_gate_payload_ready(payload):
                    return row
        if last_status in TERMINAL_STATUSES:
            if _job_has_contract_readiness_failure(row):
                pytest.skip(
                    "Approval observability test skipped: selected workflow failed immediately "
                    "due to contract readiness errors in this environment"
                )
            pytest.fail(f"Job {job_id} reached terminal status before approval gate: {last_status}")
        if last_status in QUEUE_WAIT_STATUSES:
            if now >= queue_deadline:
                break
            time.sleep(interval)
            continue
        if gate_deadline is None:
            gate_deadline = now + timeout
        time.sleep(interval)
    raise TimeoutError(
        f"Job {job_id} did not reach approval gate; last_status={last_status} last_row={last_row!r}"
    )


def _wait_for_approval_history_entry(
    job_id: str | int,
    *,
    predicate,
    timeout: int = 60,
    interval: int = 2,
    label: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_history: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        last_history = _approval_history(job_id)
        for entry in reversed(last_history):
            if predicate(entry):
                return entry
        time.sleep(interval)
    raise TimeoutError(
        f"Job {job_id} did not record approval history entry for {label}; history={last_history!r}"
    )


def _wait_for_status_not_in(
    job_id: str | int,
    *,
    disallowed: set[str],
    timeout: int = 90,
    interval: int = 2,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_row: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_row = _job_row(job_id)
        status = _job_status(last_row)
        if status not in disallowed:
            return last_row
        time.sleep(interval)
    raise TimeoutError(
        f"Job {job_id} did not leave statuses {sorted(disallowed)}; last_row={last_row!r}"
    )


def _wait_for_approval_gate_transition(
    job_id: str | int,
    *,
    previous_signature: tuple[str, str, str],
    timeout: int = 45,
    interval: int = 2,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_row: dict[str, Any] | None = None
    last_signature = previous_signature
    while time.monotonic() < deadline:
        last_row = _job_row(job_id)
        status = _job_status(last_row)
        if status in TERMINAL_STATUSES:
            return last_row
        if not _approval_needed(last_row):
            return last_row
        last_signature = _approval_gate_signature_or_none(job_id)
        if last_signature is None:
            return last_row
        if last_signature != previous_signature:
            return last_row
        time.sleep(interval)
    raise TimeoutError(
        f"Job {job_id} stayed on the same approval gate after approval: "
        f"signature={last_signature!r} row={last_row!r}"
    )


def _drive_job_to_terminal(
    job_id: str | int,
    *,
    timeout: int = 360,
    interval: int = 2,
    max_approvals: int = 12,
    cancel_on_timeout: bool = True,
) -> dict[str, Any]:
    soft_deadline = time.monotonic() + timeout
    hard_deadline = soft_deadline + WORKFLOW_ACTIVITY_TIMEOUT
    activity_deadline: float | None = None
    approvals = 0
    last_status = "unknown"
    last_marker: tuple[str, str, str, str, str] | None = None
    last_row: dict[str, Any] | None = None
    while True:
        now = time.monotonic()
        if now >= hard_deadline:
            break
        if now >= soft_deadline and activity_deadline is not None and now >= activity_deadline:
            break
        row = _job_row(job_id)
        last_row = row
        status = _job_status(row)
        last_status = status or "unknown"
        if status in TERMINAL_STATUSES:
            if _job_has_contract_readiness_failure(row):
                pytest.skip(
                    "Approval observability test skipped: runtime failed with workflow "
                    "contract readiness errors in this environment"
                )
            return row
        marker = (
            *_job_progress_marker(row),
            _descendant_job_progress_marker(row),
        )
        if marker != last_marker:
            last_marker = marker
            if status not in QUEUE_WAIT_STATUSES:
                activity_deadline = time.monotonic() + WORKFLOW_ACTIVITY_TIMEOUT
        if _approval_needed(row):
            thread_id = str(row.get("thread_id") or "").strip()
            assert thread_id, f"Job {job_id} requires approval without thread_id"
            gate_signature = _approval_gate_signature_or_none(job_id)
            if gate_signature is None:
                time.sleep(interval)
                continue
            _approve_job(job_id, thread_id)
            approvals += 1
            activity_deadline = time.monotonic() + WORKFLOW_ACTIVITY_TIMEOUT
            if approvals > max_approvals:
                pytest.fail(
                    f"Job {job_id} exceeded approval budget ({max_approvals}) before terminal status"
                )
            _wait_for_approval_gate_transition(
                job_id,
                previous_signature=gate_signature,
                timeout=max(30, interval * 10),
                interval=interval,
            )
            continue
        time.sleep(interval)

    if cancel_on_timeout:
        _cancel_job(job_id)
        settle_deadline = time.monotonic() + 45
        while time.monotonic() < settle_deadline:
            row = _job_row(job_id)
            status = _job_status(row)
            if status in TERMINAL_STATUSES:
                return row
            time.sleep(1)
        raise TimeoutError(
            f"Job {job_id} timed out and did not settle after cancel; "
            f"last_status={last_status} last_row={last_row!r}"
        )

    raise TimeoutError(
        f"Job {job_id} did not reach terminal state; last_status={last_status} last_row={last_row!r}"
    )


def _cancel_job(job_id: str | int) -> None:
    with contextlib.suppress(Exception):
        _api(
            "POST",
            f"/api/jobs/{job_id}/cancel",
            headers={"accept": "application/json"},
            allow_redirects=False,
            timeout=20,
        )


def _list_events(job_id: str | int, *, since_id: str = "") -> list[dict[str, Any]]:
    resp = _api(
        "GET",
        f"/api/jobs/{job_id}/events",
        params={"since_id": str(since_id or "")},
        timeout=20,
    )
    assert resp.status_code == 200, (
        f"Could not read events for job {job_id}: {resp.status_code} {resp.text[:300]}"
    )
    payload = resp.json() or {}
    return _extract_rows(payload, "events")


def _list_messages(
    job_id: str | int,
    *,
    since_id: str = "",
    before_id: str = "",
    newest: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": max(1, int(limit))}
    if str(since_id or "").strip():
        params["since_id"] = str(since_id).strip()
    if str(before_id or "").strip():
        params["before_id"] = str(before_id).strip()
    if newest:
        params["newest"] = "true"
    resp = _api("GET", f"/api/jobs/{job_id}/messages", params=params, timeout=20)
    assert resp.status_code == 200, (
        f"Could not read messages for job {job_id}: {resp.status_code} {resp.text[:300]}"
    )
    payload = resp.json() or {}
    return _extract_rows(payload, "messages")


def _list_logs(
    job_id: str | int,
    *,
    since_id: str = "",
    before_id: str = "",
    newest: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": max(1, int(limit))}
    if str(since_id or "").strip():
        params["since_id"] = str(since_id).strip()
    if str(before_id or "").strip():
        params["before_id"] = str(before_id).strip()
    if newest:
        params["newest"] = "true"
    resp = _api("GET", f"/api/jobs/{job_id}/logs", params=params, timeout=20)
    assert resp.status_code == 200, (
        f"Could not read logs for job {job_id}: {resp.status_code} {resp.text[:300]}"
    )
    payload = resp.json() or {}
    return _extract_rows(payload, "logs")


def _assert_strictly_monotonic_ids(
    rows: list[dict[str, Any]], *, label: str, fallback_key: str = ""
) -> list[str]:
    ids = [_extract_row_id(row, fallback_key=fallback_key) for row in rows]
    ids = [value for value in ids if value]
    assert ids, f"{label}: no ids in payload"
    assert len(set(ids)) == len(ids), f"{label}: ids contain duplicates (got {ids})"
    return ids


def _hard_delete_job(job_id: str | int) -> None:
    resp = _api("POST", f"/api/trash/job/{job_id}/hard-delete", json={}, timeout=30)
    assert resp.status_code == 200, (
        f"Hard-delete failed for job {job_id}: {resp.status_code} {resp.text[:300]}"
    )


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


@pytest.fixture
def live_workflow_project_id(test_project_id: str) -> str:
    if not _explicit_mcp_bearer_configured():
        pytest.skip(
            "Live workflow observability tests require the worker to have an explicit MCP bearer "
            "(MCP_BEARER_TOKEN or FS_SESSION_TOKEN) for caller-scoped platform tools"
        )
    return test_project_id


@pytest.fixture
def approval_test_project_id(live_workflow_project_id: str):
    if BACKEND_ROLE != "admin":
        yield live_workflow_project_id
        return

    project_id = _create_isolated_project_clone(live_workflow_project_id)
    try:
        yield project_id
    finally:
        _cleanup_project(project_id)


def test_project_queue_contract_exposes_queue_health(server_available, test_project_id: str):
    resp = _api("GET", f"/api/projects/{test_project_id}/queue", timeout=25)
    assert resp.status_code == 200, (
        f"Could not load project queue contract: {resp.status_code} {resp.text[:300]}"
    )
    payload = resp.json() or {}
    queue_health = payload.get("queue_health") or {}

    assert str(payload.get("queue_transport_state") or "").strip() in {
        "broker_available",
        "broker_unavailable",
    }
    assert str(payload.get("dead_letter_queue_name") or "").strip().endswith(".dead_letter")
    assert str(queue_health.get("state") or "").strip() in {
        "idle",
        "queued_pending",
        "retrying_delivery",
        "dead_lettered",
        "execution_failed_after_delivery",
    }
    assert str(queue_health.get("state_label") or "").strip()
    assert "requires_intervention" in queue_health


def test_observability_streams_monotonic_and_incremental(server_available, test_project_id: str):
    terminal_ids: list[str] = []
    for row in _project_jobs(test_project_id, page=1, per_page=100):
        job_id = _extract_row_id(row)
        status = str(row.get("status") or "").strip().lower()
        if job_id and status in TERMINAL_STATUSES:
            terminal_ids.append(job_id)
    if not terminal_ids:
        pytest.skip("No terminal jobs available to validate stream ordering")

    selected_id = ""
    selected_streams: (
        tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]] | None
    ) = None
    for job_id in terminal_ids:
        events = _list_events(job_id)
        messages = _list_messages(job_id, limit=75)
        logs = _list_logs(job_id, limit=75)
        rich_stream_count = int(len(events) >= 2) + int(len(messages) >= 2) + int(len(logs) >= 2)
        if rich_stream_count >= 1:
            selected_id = job_id
            selected_streams = (events, messages, logs)
            break
    if not selected_id or selected_streams is None:
        pytest.skip("No terminal job has enough stream rows (>=2) for ordering assertions")

    events, messages, logs = selected_streams

    if len(events) >= 2:
        event_ids = _assert_strictly_monotonic_ids(events, label=f"job={selected_id} events")
        pivot = event_ids[max(0, len(event_ids) // 2 - 1)]
        incremental = _list_events(selected_id, since_id=pivot)
        incremental_ids = _assert_strictly_monotonic_ids(
            incremental,
            label=f"job={selected_id} events since_id={pivot}",
        )
        expected_incremental = event_ids[event_ids.index(pivot) + 1 :]
        assert incremental_ids == expected_incremental, (
            f"Events incremental page mismatch for job={selected_id}: "
            f"expected={expected_incremental} actual={incremental_ids}"
        )

    if len(messages) >= 2:
        message_ids = _assert_strictly_monotonic_ids(messages, label=f"job={selected_id} messages")
        last_message_id = message_ids[-1]
        older = _list_messages(selected_id, before_id=last_message_id, newest=True, limit=10)
        older_ids = _assert_strictly_monotonic_ids(
            older,
            label=f"job={selected_id} messages before_id={last_message_id} newest=true",
        )
        expected_window = message_ids[:-1]
        assert older_ids == expected_window[-len(older_ids) :], (
            f"Messages newest window mismatch for job={selected_id}: "
            f"expected suffix={expected_window[-len(older_ids) :]} actual={older_ids}"
        )

    if len(logs) >= 2:
        log_ids = _assert_strictly_monotonic_ids(logs, label=f"job={selected_id} logs")
        last_log_id = log_ids[-1]
        older_logs = _list_logs(selected_id, before_id=last_log_id, newest=True, limit=10)
        older_log_ids = _assert_strictly_monotonic_ids(
            older_logs,
            label=f"job={selected_id} logs before_id={last_log_id} newest=true",
        )
        expected_window = log_ids[:-1]
        assert older_log_ids == expected_window[-len(older_log_ids) :], (
            f"Logs newest window mismatch for job={selected_id}: "
            f"expected suffix={expected_window[-len(older_log_ids) :]} actual={older_log_ids}"
        )


def test_job_trash_then_hard_delete_observability_lifecycle(
    server_available, live_workflow_project_id: str
):
    if BACKEND_ROLE != "admin":
        pytest.skip("Job hard-delete lifecycle test requires admin role")

    job_id = _submit_test_mode_job(
        live_workflow_project_id,
        summary="E2E observability lifecycle: verify soft-delete retention and hard-delete purge",
    )
    hard_deleted = False
    try:
        _drive_job_to_terminal(job_id, timeout=240, interval=2)

        before_events = _list_events(job_id)
        before_messages = _list_messages(job_id, limit=100)
        before_logs = _list_logs(job_id, limit=100)

        before_event_ids = {
            value for value in (_extract_row_id(row) for row in before_events) if value
        }
        before_message_ids = {
            value for value in (_extract_row_id(row) for row in before_messages) if value
        }
        before_log_ids = {value for value in (_extract_row_id(row) for row in before_logs) if value}

        delete_resp = _api("DELETE", f"/api/jobs/{job_id}", timeout=20)
        assert delete_resp.status_code in (200, 204), (
            f"Soft-delete failed for job {job_id}: {delete_resp.status_code} {delete_resp.text[:300]}"
        )

        deadline = time.monotonic() + 30
        hidden_from_project_list = False
        while time.monotonic() < deadline:
            if job_id not in set(_project_job_ids(live_workflow_project_id, per_page=200)):
                hidden_from_project_list = True
                break
            time.sleep(1)
        assert hidden_from_project_list, (
            f"Trashed job {job_id} is still visible in project jobs list"
        )

        after_events = _list_events(job_id)
        after_messages = _list_messages(job_id, limit=100)
        after_logs = _list_logs(job_id, limit=100)

        after_event_ids = {
            value for value in (_extract_row_id(row) for row in after_events) if value
        }
        after_message_ids = {
            value for value in (_extract_row_id(row) for row in after_messages) if value
        }
        after_log_ids = {value for value in (_extract_row_id(row) for row in after_logs) if value}

        assert before_event_ids.issubset(after_event_ids), (
            f"Soft-delete dropped event rows for job {job_id}: before={sorted(before_event_ids)} "
            f"after={sorted(after_event_ids)}"
        )
        assert before_message_ids.issubset(after_message_ids), (
            f"Soft-delete dropped message rows for job {job_id}: before={sorted(before_message_ids)} "
            f"after={sorted(after_message_ids)}"
        )
        assert before_log_ids.issubset(after_log_ids), (
            f"Soft-delete dropped log rows for job {job_id}: before={sorted(before_log_ids)} "
            f"after={sorted(after_log_ids)}"
        )

        _hard_delete_job(job_id)
        hard_deleted = True

        status_resp = _api("GET", f"/api/jobs/{job_id}", timeout=20)
        assert status_resp.status_code == 404, (
            f"Hard-deleted job {job_id} is still readable: {status_resp.status_code} {status_resp.text[:300]}"
        )

        for endpoint in ("events", "messages", "logs"):
            resp = _api("GET", f"/api/jobs/{job_id}/{endpoint}", timeout=20)
            assert resp.status_code == 404, (
                f"Hard-deleted job {job_id} still exposes {endpoint}: "
                f"{resp.status_code} {resp.text[:300]}"
            )
    finally:
        if not hard_deleted:
            with contextlib.suppress(Exception):
                _api("DELETE", f"/api/jobs/{job_id}", timeout=20)
            with contextlib.suppress(Exception):
                _hard_delete_job(job_id)
        with contextlib.suppress(Exception):
            _cancel_active_jobs(live_workflow_project_id, timeout=90)


def test_approval_override_persists_history_and_job_completes(
    server_available,
    approval_test_project_id: str,
):
    if BACKEND_ROLE == "viewer":
        pytest.skip("Approval override test requires write-capable backend role")

    _cancel_active_jobs(approval_test_project_id, timeout=90)

    original_policy = _set_project_policy(
        approval_test_project_id,
        autonomy_mode="manual",
        autonomy_confidence_threshold=1.0,
    )
    job_id = _submit_test_mode_job(
        approval_test_project_id,
        summary=(
            "Add an admin release approvals panel that shows recent approval history, validates null "
            "and malformed values in the backing API, and includes focused API and UI coverage."
        ),
        workflow_id=NEUTRAL_WORKFLOW_ID,
    )
    override_content = (
        "# Operator Requirements\n\n"
        "Keep a durable audit trail for approval decisions, validate null and string inputs "
        "server-side, and use a compact admin-facing layout."
    )
    try:
        gate_row = _wait_for_approval_gate(job_id, timeout=APPROVAL_GATE_TIMEOUT, interval=2)
        thread_id = str(gate_row.get("thread_id") or "").strip()
        assert thread_id, f"Job {job_id} reached approval without thread_id"
        gate_payload = _approval_gate(job_id)
        expected_stage = str(gate_payload.get("stage") or "").strip()

        body = _submit_approval_decision(
            job_id,
            thread_id,
            action="override",
            comment="Use the operator-authored requirements artifact.",
            override_content=override_content,
        )
        assert str(body.get("action") or "").strip().lower() == "override"
        assert body.get("override_content") == override_content

        override_entry = _wait_for_approval_history_entry(
            job_id,
            label="override approval",
            predicate=lambda entry: (
                str(entry.get("action") or "").strip().lower() == "override"
                and entry.get("approved") is True
                and str(entry.get("comment") or "").strip()
                == "Use the operator-authored requirements artifact."
            ),
        )
        recorded_stage = str(override_entry.get("stage") or "").strip()
        if expected_stage and recorded_stage:
            assert recorded_stage == expected_stage
        assert (
            str(override_entry.get("override_content_preview") or "").strip()
            == override_content[:200]
        )

        if str(os.environ.get("CONTROL_LOOPS_ENABLED") or "").strip() == "0":
            pytest.skip("Approval history was recorded, but job resume requires control loops.")
        resumed_row = _wait_for_status_not_in(
            job_id,
            disallowed={"awaiting_approval"},
            timeout=90,
            interval=2,
        )
        assert _job_status(resumed_row) in ACTIVE_STATUSES | TERMINAL_STATUSES
        latest_row = _job_row(job_id)
        assert _job_status(latest_row) not in {"awaiting_approval", "failed"}
    finally:
        _cancel_job(job_id)
        with contextlib.suppress(Exception):
            _api("DELETE", f"/api/jobs/{job_id}", timeout=20)
        if BACKEND_ROLE == "admin":
            with contextlib.suppress(Exception):
                _hard_delete_job(job_id)
        _restore_project_policy(approval_test_project_id, original_policy)


def test_reject_then_approve_records_history_and_resumes(
    server_available,
    approval_test_project_id: str,
):
    if BACKEND_ROLE == "viewer":
        pytest.skip("Reject/approve history test requires write-capable backend role")

    _cancel_active_jobs(approval_test_project_id, timeout=90)

    original_policy = _set_project_policy(
        approval_test_project_id,
        autonomy_mode="manual",
        autonomy_confidence_threshold=1.0,
    )
    job_id = _submit_test_mode_job(
        approval_test_project_id,
        summary=(
            "Build a release readiness panel for admins that aggregates deployment status, handles "
            "null and malformed upstream values safely, and includes focused API and UI coverage."
        ),
        workflow_id=NEUTRAL_WORKFLOW_ID,
    )
    reject_comment = (
        "Please expand edge cases for null, empty, and malformed inputs before continuing."
    )
    approve_comment = "Revision addresses the requested edge cases; continue."
    try:
        first_gate = _wait_for_approval_gate(job_id, timeout=APPROVAL_GATE_TIMEOUT, interval=2)
        thread_id = str(first_gate.get("thread_id") or "").strip()
        assert thread_id, f"Job {job_id} reached approval without thread_id"

        reject_body = _submit_approval_decision(
            job_id,
            thread_id,
            approved=False,
            comment=reject_comment,
        )
        assert reject_body.get("approved") is False

        reject_entry = _wait_for_approval_history_entry(
            job_id,
            label="rejection approval",
            predicate=lambda entry: (
                entry.get("approved") is False
                and str(entry.get("comment") or "").strip() == reject_comment
            ),
        )
        assert str(reject_entry.get("action") or "").strip() in {"", "none"}

        try:
            resumed_after_reject = _wait_for_status_not_in(
                job_id,
                disallowed={"awaiting_approval"},
                timeout=30,
                interval=2,
            )
            assert _job_status(resumed_after_reject) in ACTIVE_STATUSES | TERMINAL_STATUSES
            follow_up_gate = _wait_for_approval_gate(
                job_id,
                timeout=APPROVAL_GATE_TIMEOUT,
                interval=2,
            )
            approve_thread_id = str(follow_up_gate.get("thread_id") or "").strip()
            assert approve_thread_id, f"Job {job_id} returned to approval without thread_id"
        except TimeoutError:
            rejected_row = _job_row(job_id)
            assert _job_status(rejected_row) == "awaiting_approval"
            failure_summary = rejected_row.get("failure_summary") or {}
            assert str(failure_summary.get("kind") or "") == "approval_rejected"
            approve_thread_id = thread_id

        approve_body = _submit_approval_decision(
            job_id,
            approve_thread_id,
            approved=True,
            comment=approve_comment,
        )
        assert approve_body.get("approved") is True

        approve_entry = _wait_for_approval_history_entry(
            job_id,
            label="follow-up approval",
            predicate=lambda entry: (
                entry.get("approved") is True
                and str(entry.get("comment") or "").strip() == approve_comment
            ),
        )
        approvals = _approval_history(job_id)
        reject_index = approvals.index(reject_entry)
        approve_index = approvals.index(approve_entry)
        assert reject_index < approve_index, (
            f"Approval history order is wrong for job {job_id}: "
            f"reject_index={reject_index}, approve_index={approve_index}, approvals={approvals!r}"
        )

        resumed_after_approve = _wait_for_status_not_in(
            job_id,
            disallowed={"awaiting_approval"},
            timeout=90,
            interval=2,
        )
        final_status = _job_status(resumed_after_approve)
        assert final_status in ACTIVE_STATUSES | TERMINAL_STATUSES, (
            f"Reject/re-approve path must resume successfully; job={job_id} status={final_status!r}"
        )
    finally:
        _cancel_job(job_id)
        with contextlib.suppress(Exception):
            _api("DELETE", f"/api/jobs/{job_id}", timeout=20)
        if BACKEND_ROLE == "admin":
            with contextlib.suppress(Exception):
                _hard_delete_job(job_id)
        _restore_project_policy(approval_test_project_id, original_policy)


def test_restart_terminal_job_reaches_terminal_again(
    server_available, live_workflow_project_id: str
):
    source_job_id = _submit_test_mode_job(
        live_workflow_project_id,
        summary="E2E restart lifecycle: source terminal job for restart endpoint",
    )
    restarted_job_id = 0
    try:
        source_row = _drive_job_to_terminal(source_job_id, timeout=240, interval=2)
        assert _job_status(source_row) in TERMINAL_STATUSES, (
            f"Source restart job {source_job_id} did not reach terminal status: "
            f"status={_job_status(source_row)!r}"
        )

        resp = _api(
            "POST",
            f"/api/jobs/{source_job_id}/restart",
            headers={"accept": "application/json"},
            json={},
            timeout=30,
            allow_redirects=False,
        )
        assert resp.status_code == 200, (
            f"Restart endpoint failed for job {source_job_id}: {resp.status_code} {resp.text[:300]}"
        )
        body = resp.json() or {}
        restarted_job_id = str(body.get("new_job_id") or 0).strip()
        assert restarted_job_id, f"Restart response missing new_job_id: {body}"
        assert restarted_job_id != source_job_id

        restarted_row = _drive_job_to_terminal(restarted_job_id, timeout=300, interval=2)
        restarted_status = _job_status(restarted_row)
        assert restarted_status in TERMINAL_STATUSES, (
            f"Restarted job {restarted_job_id} did not reach terminal status: {restarted_status!r}"
        )
    finally:
        with contextlib.suppress(Exception):
            _api("DELETE", f"/api/jobs/{source_job_id}", timeout=20)
        with contextlib.suppress(Exception):
            _hard_delete_job(source_job_id)
        if restarted_job_id:
            with contextlib.suppress(Exception):
                _api("DELETE", f"/api/jobs/{restarted_job_id}", timeout=20)
            with contextlib.suppress(Exception):
                _hard_delete_job(restarted_job_id)


def test_retry_step_resume_from_step_when_retryable_job_exists(
    server_available, test_project_id: str
):
    if BACKEND_ROLE == "viewer":
        pytest.skip("retry-step resume test requires write-capable backend role")

    retried_job_id = seed_retryable_interrupted_step_job(
        test_project_id,
        summary=(
            "Investigate and fix workflow retry-step so interrupted jobs resume from the captured "
            "step without losing queue state, approvals, or observability logs."
        ),
    )
    try:
        resp = _api("POST", f"/api/jobs/{retried_job_id}/retry-step", json={}, timeout=30)
        assert resp.status_code == 200, (
            f"retry-step failed for deterministic observability probe job {retried_job_id}: "
            f"{resp.status_code} {resp.text[:300]}"
        )

        retry_response_body = resp.json() or {}
        assert str(retry_response_body.get("job_id") or 0).strip() == retried_job_id
        assert str(retry_response_body.get("status") or "").strip().lower() == "pending"
        assert bool(retry_response_body.get("enqueued")) is True
        assert str(retry_response_body.get("thread_id") or "").strip(), (
            f"retry-step response missing thread_id: {retry_response_body}"
        )

        detail_row = _job_row(retried_job_id)
        runtime = detail_row.get("runtime") or {}
        assert isinstance(runtime, dict), f"Unexpected job runtime after retry-step: {runtime!r}"
        assert runtime.get("retryable_interrupted_step") in (None, {}), (
            f"Retry-step snapshot should be cleared from runtime payload: {runtime!r}"
        )

        journal_rows = retry_runtime_journal(retried_job_id)
        retry_event = next(
            (
                row
                for row in journal_rows
                if str(row.get("event") or "").strip().lower() == "manual_retry_step"
            ),
            None,
        )
        assert retry_event is not None, (
            f"manual_retry_step audit event missing for job {retried_job_id}: {journal_rows!r}"
        )
        retry_detail = retry_event.get("detail") or {}
        assert str(retry_detail.get("requested_by") or "").strip()
        assert str(retry_detail.get("agent_id") or "").strip()
        assert str(retry_detail.get("skill") or "").strip()
        assert str(retry_detail.get("step_id") or "").strip()

        terminal_row = _drive_job_to_terminal(retried_job_id, timeout=360, interval=2)
        final_status = _job_status(terminal_row)
        assert final_status in TERMINAL_STATUSES, (
            f"Retried job {retried_job_id} did not return to terminal status: {final_status!r}"
        )
    finally:
        cleanup_retryable_job(retried_job_id)
