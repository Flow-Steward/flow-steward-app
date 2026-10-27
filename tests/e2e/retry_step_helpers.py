"""Shared helpers for deterministic interrupted-step retry E2E contracts."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.application.tooling.testing.runtime_env import resolve_base_url
from tests.e2e.conftest import e2e_request
from tests.e2e.neutral_registry import (
    NEUTRAL_AGENT_ID,
    NEUTRAL_DRAFT_STEP_ID,
    NEUTRAL_SKILL_ID,
    NEUTRAL_WORKFLOW_ID,
)

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
VERIFY_SSL = str(os.getenv("E2E_VERIFY_SSL", "0") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

ACTIVE_STATUSES = {"pending", "queued", "in_progress", "awaiting_approval"}
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "max_iterations",
    "retryable_interrupted_step",
    "stuck",
}


def _approval_needed(row: dict[str, Any]) -> bool:
    status = job_status(row)
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


def _api(method: str, path: str, *, timeout: int = 20, **kwargs):
    return e2e_request(
        method.upper(),
        f"{BASE_URL}{path}",
        verify=VERIFY_SSL,
        timeout=timeout,
        **kwargs,
    )


def _connect_db():
    dsn = str(os.getenv("FS_DB_DSN") or os.getenv("E2E_AUTH_DSN") or "").strip()
    if not dsn:
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if not line.startswith("FS_DB_DSN="):
                    continue
                dsn = line.split("=", 1)[1].strip()
                break
    # Keep service DNS names when running inside containers (fs-test-runner can resolve pg-fs).
    # Host-local runs still need loopback rewrite to reach published DB port.
    if not Path("/.dockerenv").exists():
        if "host=pg-fs" in dsn:
            dsn = dsn.replace("host=pg-fs", "host=127.0.0.1")
        if "@pg-fs:" in dsn:
            dsn = dsn.replace("@pg-fs:", "@127.0.0.1:")
    if not dsn:
        return None
    try:
        import psycopg  # type: ignore

        conn = psycopg.connect(dsn)
        conn.autocommit = True
        return conn
    except Exception:
        try:
            from core.infrastructure.db import psycopg_runtime as psycopg_runtime  # type: ignore

            conn = psycopg_runtime.connect(dsn)
            conn.autocommit = True
            return conn
        except Exception:
            return None


def _exec_sql_via_docker(sql: str, *, timeout: int = 30) -> str | None:
    repo_root = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                "flow-steward",
                "-f",
                "infra/docker/docker-compose.yaml",
                "exec",
                "-T",
                "pg-fs",
                "psql",
                "-U",
                "flow_steward",
                "-d",
                "flow_steward",
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
                "-c",
                sql,
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return None


def _project_queue(project_id: str) -> dict[str, Any]:
    resp = _api("GET", f"/api/projects/{project_id}/queue", timeout=20)
    assert resp.status_code == 200, (
        f"Could not inspect project queue for project {project_id}: "
        f"{resp.status_code} {resp.text[:300]}"
    )
    payload = resp.json() or {}
    assert isinstance(payload, dict), f"Unexpected project queue payload: {payload!r}"
    return payload


def _collect_active_project_job_ids(project_id: str) -> list[str]:
    stale_ids: set[str] = set()
    project_id_str = str(project_id or "").strip()

    try:
        resp = _api("GET", "/api/jobs/awaiting-approval", timeout=20)
        if resp.ok:
            for row in resp.json() or []:
                if str(row.get("project_id") or "").strip() != project_id_str:
                    continue
                raw_id = row.get("id") or row.get("job_id")
                if raw_id:
                    stale_ids.add(str(raw_id).strip())
    except Exception:
        pass

    try:
        payload = _project_queue(project_id_str)
        for row in payload.get("pending_jobs") or []:
            raw_id = row.get("job_id") or row.get("id")
            if raw_id:
                stale_ids.add(str(raw_id).strip())
        active_job_id = str(
            ((payload.get("project_lease") or {}).get("active_job_id")) or ""
        ).strip()
        if active_job_id:
            stale_ids.add(active_job_id)
    except Exception:
        pass

    try:
        resp = _api("GET", "/api/processes", timeout=20)
        if resp.ok:
            payload = resp.json() or {}
            for row in payload.get("jobs") or []:
                if str(row.get("project_id") or "").strip() != project_id_str:
                    continue
                if str(row.get("status") or "").strip().lower() not in ACTIVE_STATUSES | {"stuck"}:
                    continue
                raw_id = row.get("id") or row.get("job_id")
                if raw_id:
                    stale_ids.add(str(raw_id).strip())
    except Exception:
        pass

    return sorted(job_id for job_id in stale_ids if job_id)


def submit_test_mode_job(project_id: str, *, summary: str) -> str:
    payload = {
        "inputs": {"topic": f"Investigate interrupted-step retry-step seed path: {summary}"},
        "project_id": str(project_id),
        "workflow_id": NEUTRAL_WORKFLOW_ID,
        "test_mode": True,
    }
    resp = _api("POST", "/api/workflow-runs", json=payload, timeout=30)
    if resp.status_code in (401, 403):
        pytest.skip("Task submit requires elevated permissions for retry-step e2e")
    assert resp.status_code == 200, (
        f"Task submit failed for retry-step seed: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    job_id = str(body.get("job_id") or body.get("id") or "").strip()
    assert job_id, f"Task submit response missing job_id: {body}"
    return job_id


def _sql_literal(value: str) -> str:
    return str(value or "").replace("'", "''")


def job_row(job_id: str | int) -> dict[str, Any]:
    resp = _api("GET", f"/api/jobs/{job_id}", timeout=20)
    assert resp.status_code == 200, (
        f"Could not read job {job_id}: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    if (
        isinstance(body, dict)
        and str(body.get("status") or "").strip().lower() == "error"
        and "not found" in str(body.get("error") or "").strip().lower()
    ):
        raise AssertionError(f"Job {job_id} no longer exists: {body!r}")
    if isinstance(body, dict) and isinstance(body.get("job"), dict):
        return dict(body["job"])
    assert isinstance(body, dict), f"Unexpected job payload for {job_id}: {body!r}"
    return body


def job_status(row: dict[str, Any]) -> str:
    return str(row.get("status") or "").strip().lower()


def approve_job(job_id: str | int, thread_id: str) -> None:
    gate = _api("GET", f"/api/jobs/{job_id}/approval", timeout=20)
    gate_body = gate.json() if gate.status_code == 200 else {}
    approval_id = str(gate_body.get("approval_id") or "").strip()
    assert approval_id, f"Approval gate for job {job_id} is missing approval_id"
    resp = _api(
        "POST",
        f"/api/approvals/{thread_id}",
        json={"approval_id": approval_id, "approved": True},
        headers={"Idempotency-Key": f"retry-step-approval:{approval_id}:approve"},
        timeout=30,
        allow_redirects=False,
    )
    assert resp.status_code in (200, 202), (
        f"Approval failed for thread {thread_id}: {resp.status_code} {resp.text[:250]}"
    )


def cancel_job(job_id: str | int) -> None:
    _api(
        "POST",
        f"/api/jobs/{job_id}/cancel",
        headers={"accept": "application/json"},
        timeout=20,
        allow_redirects=False,
    )


def clear_project_backlog(project_id: str, *, timeout: int = 90, interval: float = 2.0) -> None:
    active_ids = _collect_active_project_job_ids(project_id)
    for job_id in active_ids:
        with contextlib.suppress(Exception):
            cancel_job(job_id)
        time.sleep(0.1)

    if not active_ids:
        return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        queue_payload = _project_queue(project_id)
        lease_payload = queue_payload.get("project_lease") or {}
        active_job_id = str(lease_payload.get("active_job_id") or "").strip()
        pending_jobs = queue_payload.get("pending_jobs") or []
        has_blocking_pending = any(
            str(row.get("job_id") or row.get("id") or "").strip() in active_ids
            for row in pending_jobs
        )
        if not has_blocking_pending and active_job_id not in active_ids:
            return
        time.sleep(interval)


def drive_job_to_terminal(
    job_id: str | int,
    *,
    timeout: int = 360,
    interval: float = 2.0,
    max_approvals: int = 12,
    cancel_on_timeout: bool = True,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    approvals = 0
    last_status = "unknown"
    while time.monotonic() < deadline:
        row = job_row(job_id)
        status = job_status(row)
        last_status = status or "unknown"
        if status in TERMINAL_STATUSES:
            return row
        if _approval_needed(row):
            thread_id = str(row.get("thread_id") or "").strip()
            assert thread_id, f"Job {job_id} requires approval without thread_id"
            approve_job(job_id, thread_id)
            approvals += 1
            if approvals > max_approvals:
                pytest.fail(
                    f"Job {job_id} exceeded approval budget ({max_approvals}) before terminal status"
                )
        time.sleep(interval)

    if cancel_on_timeout:
        cancel_job(job_id)
        settle_deadline = time.monotonic() + 45
        while time.monotonic() < settle_deadline:
            row = job_row(job_id)
            status = job_status(row)
            if status in TERMINAL_STATUSES:
                return row
            time.sleep(1)
    raise TimeoutError(f"Job {job_id} did not reach terminal status; last_status={last_status!r}")


def runtime_journal(job_id: str | int) -> list[dict[str, Any]]:
    resp = _api("GET", f"/api/jobs/{job_id}/runtime-journal", timeout=20)
    assert resp.status_code == 200, (
        f"Could not read runtime journal for job {job_id}: {resp.status_code} {resp.text[:300]}"
    )
    payload = resp.json() or {}
    if isinstance(payload, dict):
        rows = payload.get("runtime_journal") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _build_retryable_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    job_id = str(row.get("id") or row.get("job_id") or "").strip()
    thread_id = str(row.get("thread_id") or "").strip()
    step_id = str(row.get("current_step_id") or "").strip() or NEUTRAL_DRAFT_STEP_ID
    agent_id = NEUTRAL_AGENT_ID
    skill = NEUTRAL_SKILL_ID
    return {
        "detected_at": datetime.now(UTC).isoformat(),
        "detection_source": "e2e_seed",
        "job_id": job_id,
        "thread_id": thread_id,
        "step_id": step_id,
        "agent_id": agent_id,
        "skill": skill,
        "correlation_id": f"e2e-retry-step-{job_id}",
        "provider": "openai",
        "model_used": "e2e-seed",
        "prompt_version": "e2e",
        "system_prompt": "E2E retry-step seed snapshot",
        "user_prompt": "Resume from deterministic interrupted-step seed",
    }


def create_retryable_interrupted_step_job(project_id: str, *, summary: str) -> str:
    project_id_str = str(project_id or "").strip()
    assert project_id_str, f"Retry-step seed requires valid project_id, got {project_id!r}"

    task_id = f"e2e-retry-step-{uuid.uuid4().hex[:10]}"
    thread_id = uuid.uuid4().hex
    task_summary = f"Investigate interrupted-step retry-step seed path: {summary}"
    queue_name = f"platform.project.{project_id_str}"
    seed_meta = {
        "test_mode": True,
        "requested_workflow_id": NEUTRAL_WORKFLOW_ID,
        "selected_workflow_id": NEUTRAL_WORKFLOW_ID,
        "workflow_selection_source": "explicit_request",
        "workflow_selection_pinned": True,
    }

    conn = _connect_db()
    if conn is None:
        task_id_sql = _sql_literal(task_id)
        thread_id_sql = _sql_literal(thread_id)
        task_summary_sql = _sql_literal(task_summary)
        queue_name_sql = _sql_literal(queue_name)
        returned_id = _exec_sql_via_docker(
            f"""
            INSERT INTO jobs (
                project_id,
                task_id,
                task_summary,
                task_source,
                thread_id,
                mode,
                status,
                queue_name,
                heartbeat_at,
                current_step_id,
                current_agent,
                meta
            )
            VALUES (
                '{_sql_literal(project_id_str)}',
                '{task_id_sql}',
                '{task_summary_sql}',
                'e2e_test',
                '{thread_id_sql}',
                'standard',
                'retryable_interrupted_step',
                '{queue_name_sql}',
                NOW(),
                '{NEUTRAL_DRAFT_STEP_ID}',
                '{NEUTRAL_AGENT_ID}',
                '{_sql_literal(json.dumps(seed_meta))}'::jsonb
            )
            RETURNING id;
            """
        )
        assert returned_id, (
            f"Could not create deterministic retryable seed job via docker psql: {returned_id!r}"
        )
        job_id = str(returned_id).strip()
        job_id_sql = _sql_literal(job_id)
        update_result = _exec_sql_via_docker(
            f"""
            UPDATE jobs
               SET meta = COALESCE(meta, '{{}}'::jsonb)
                    || jsonb_build_object(
                        'retryable_interrupted_step',
                        jsonb_build_object(
                            'detected_at', NOW()::text,
                            'detection_source', 'e2e_seed',
                            'job_id', '{job_id_sql}',
                            'thread_id', '{thread_id_sql}',
                            'step_id', '{NEUTRAL_DRAFT_STEP_ID}',
                            'agent_id', '{NEUTRAL_AGENT_ID}',
                            'skill', '{NEUTRAL_SKILL_ID}',
                            'correlation_id', 'e2e-retry-step-{job_id}',
                            'provider', 'openai',
                            'model_used', 'e2e-seed',
                            'prompt_version', 'e2e',
                            'system_prompt', 'E2E retry-step seed snapshot',
                            'user_prompt', 'Resume from deterministic interrupted-step seed'
                        )
                    )
             WHERE id = '{job_id_sql}'
             RETURNING id;
            """
        )
        assert update_result == str(job_id), (
            f"Could not persist retryable snapshot for deterministic seed job {job_id}: "
            f"{update_result!r}"
        )
    else:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jobs (
                        project_id,
                        task_id,
                        task_summary,
                        task_source,
                        thread_id,
                        mode,
                        status,
                        queue_name,
                        heartbeat_at,
                        current_step_id,
                        current_agent,
                        meta
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, 'retryable_interrupted_step', %s, NOW(), %s, %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        project_id_str,
                        task_id,
                        task_summary,
                        "e2e_test",
                        thread_id,
                        "standard",
                        queue_name,
                        NEUTRAL_DRAFT_STEP_ID,
                        NEUTRAL_AGENT_ID,
                        json.dumps(seed_meta),
                    ),
                )
                row = cur.fetchone()
                job_id = str(row[0] if isinstance(row, (list, tuple)) else row["id"]).strip()
                snapshot = {
                    "detected_at": datetime.now(UTC).isoformat(),
                    "detection_source": "e2e_seed",
                    "job_id": job_id,
                    "thread_id": thread_id,
                    "step_id": NEUTRAL_DRAFT_STEP_ID,
                    "agent_id": NEUTRAL_AGENT_ID,
                    "skill": NEUTRAL_SKILL_ID,
                    "correlation_id": f"e2e-retry-step-{job_id}",
                    "provider": "openai",
                    "model_used": "e2e-seed",
                    "prompt_version": "e2e",
                    "system_prompt": "E2E retry-step seed snapshot",
                    "user_prompt": "Resume from deterministic interrupted-step seed",
                }
                cur.execute(
                    """
                    UPDATE jobs
                       SET meta = COALESCE(meta, '{}'::jsonb)
                            || jsonb_build_object('retryable_interrupted_step', %s::jsonb)
                     WHERE id = %s
                    """,
                    (json.dumps(snapshot), job_id),
                )
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                close()

    seeded_row = job_row(job_id)
    assert job_status(seeded_row) == "retryable_interrupted_step", (
        f"Retry-step seed job {job_id} did not persist retryable status: {seeded_row!r}"
    )
    return job_id


def force_retryable_interrupted_step(job_id: str | int, *, project_id: str | int) -> None:
    row = job_row(job_id)
    project_id_str = str(project_id or "").strip()
    thread_id = str(row.get("thread_id") or "").strip()
    assert project_id_str, f"Retry-step seed job {job_id} is missing project_id context"
    assert thread_id, f"Retry-step seed job {job_id} is missing thread_id"

    conn = _connect_db()
    snapshot = _build_retryable_snapshot(row)
    if conn is None:
        snapshot_json = json.dumps(snapshot).replace("'", "''")
        job_id_sql = _sql_literal(str(job_id))
        returned_id = _exec_sql_via_docker(
            f"""
            UPDATE jobs
            SET
              status = 'retryable_interrupted_step',
              queue_name = COALESCE(NULLIF(queue_name, ''), 'platform.project.{_sql_literal(project_id_str)}'),
              worker_id = '',
              heartbeat_at = NOW(),
              meta = COALESCE(meta, '{{}}'::jsonb)
                || jsonb_build_object('retryable_interrupted_step', '{snapshot_json}'::jsonb)
            WHERE id = '{job_id_sql}'
            RETURNING id;
            """
        )
        assert returned_id == str(job_id), (
            f"Could not inject retryable interrupted-step snapshot for job {job_id} via docker psql"
        )
    else:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jobs
                    SET
                      status = 'retryable_interrupted_step',
                      queue_name = COALESCE(NULLIF(queue_name, ''), %s),
                      worker_id = '',
                      heartbeat_at = NOW(),
                      meta = COALESCE(meta, '{}'::jsonb)
                        || jsonb_build_object('retryable_interrupted_step', %s::jsonb)
                    WHERE id = %s
                    """,
                    (f"platform.project.{project_id_str}", json.dumps(snapshot), str(job_id)),
                )
                assert int(getattr(cur, "rowcount", 0) or 0) == 1, (
                    f"Could not inject retryable interrupted-step snapshot for job {job_id}"
                )
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                close()

    seeded_row = job_row(job_id)
    assert job_status(seeded_row) == "retryable_interrupted_step", (
        f"Retry-step seed job {job_id} did not persist retryable status: {seeded_row!r}"
    )


def seed_retryable_interrupted_step_job(project_id: str, *, summary: str) -> str:
    clear_project_backlog(project_id)
    job_id = create_retryable_interrupted_step_job(project_id, summary=summary)
    try:
        return job_id
    except Exception:
        cleanup_job(job_id)
        raise


def hard_delete_job(job_id: str | int) -> None:
    resp = _api("POST", f"/api/trash/job/{job_id}/hard-delete", json={}, timeout=30)
    assert resp.status_code in (200, 204, 404), (
        f"Hard delete failed for job {job_id}: {resp.status_code} {resp.text[:300]}"
    )


def cleanup_job(job_id: str | int) -> None:
    with contextlib.suppress(Exception):
        _api("DELETE", f"/api/jobs/{job_id}", timeout=20)
    with contextlib.suppress(Exception):
        hard_delete_job(job_id)


__all__ = [
    "cleanup_job",
    "drive_job_to_terminal",
    "job_row",
    "job_status",
    "runtime_journal",
    "seed_retryable_interrupted_step_job",
]
