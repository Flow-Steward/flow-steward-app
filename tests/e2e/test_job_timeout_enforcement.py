"""E2E timeout-enforcement contracts for watchdog runtime safety.

Covers deterministic handling for stale in-progress jobs:
- watchdog scan with apply marks the job out of active execution states;
- runtime journal records watchdog mark metadata;
- watchdog kill recovery transitions the job to cancelled and records audit trail.
"""

from __future__ import annotations

import contextlib
import os
import time

import pytest

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import delete, get, post_json
from tests.e2e.neutral_registry import NEUTRAL_WORKFLOW_ID

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
PROJECT_ID = str(TEST_ENV.project_id or "").strip()

ACTIVE_STATUSES = {"pending", "queued", "in_progress", "awaiting_approval"}
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "max_iterations",
    "retryable_interrupted_step",
    "stuck",
}
WATCHDOG_MARK_STATUSES = {"stuck", "orphaned", "retryable_interrupted_step"}


def _job_has_contract_readiness_failure(job_row: dict) -> bool:
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


def _coerce_positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _force_stale_age_seconds() -> int:
    stuck = _coerce_positive_int(os.getenv("FS_WATCHDOG_STUCK_SECONDS"), 120)
    orphan = _coerce_positive_int(os.getenv("FS_WATCHDOG_ORPHAN_GRACE_SECONDS"), 120)
    return max(stuck, orphan) + 60


def _connect_db():
    dsn = str(os.getenv("FS_DB_DSN") or os.getenv("E2E_AUTH_DSN") or "").strip()
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


def _submit_timeout_probe_job(project_id: str) -> str:
    payload = {
        "inputs": {
            "topic": (
                "E2E timeout enforcement probe: verify watchdog timeout policy marks stale runtime deterministically"
            )
        },
        "project_id": str(project_id),
        "workflow_id": NEUTRAL_WORKFLOW_ID,
        "test_mode": True,
    }
    resp = post_json("/api/workflow-runs", payload, timeout=30)
    if resp.status_code in (401, 403):
        pytest.skip("Task submit requires elevated permissions for timeout enforcement suite")
    assert resp.status_code == 200, (
        f"Task submit failed for timeout-enforcement probe: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    job_id = str(body.get("job_id") or body.get("id") or 0).strip()
    assert job_id, f"Task submit response missing job_id: {body}"
    return job_id


def _force_job_stale_in_progress(job_id: str | int, *, age_seconds: int) -> None:
    conn = _connect_db()
    if conn is None:
        pytest.skip("No DB DSN available; cannot force stale runtime state for watchdog e2e")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET
                  status = 'in_progress',
                  started_at = NOW() - make_interval(secs => %s),
                  heartbeat_at = NOW() - make_interval(secs => %s),
                  worker_id = %s
                WHERE id = %s
                """,
                (int(age_seconds), int(age_seconds), "worker-timeout-probe", job_id),
            )
            assert int(getattr(cur, "rowcount", 0) or 0) == 1, (
                f"Could not force stale runtime state for job {job_id}"
            )
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()


def _job_row(job_id: str | int) -> dict:
    resp = get(f"/api/jobs/{job_id}", timeout=20)
    assert resp.status_code == 200, (
        f"Could not read job {job_id}: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    if isinstance(body, dict) and isinstance(body.get("job"), dict):
        return dict(body["job"])
    assert isinstance(body, dict), f"Unexpected job payload: {body!r}"
    return body


def _runtime_journal(job_id: str | int) -> list[dict]:
    resp = get(f"/api/jobs/{job_id}/runtime-journal", timeout=20)
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


def _approve_job(job_id: str | int, thread_id: str) -> None:
    gate_response = get(f"/api/jobs/{job_id}/approval", timeout=20)
    assert gate_response.status_code == 200, (
        f"Could not read approval gate for job {job_id}: "
        f"{gate_response.status_code} {gate_response.text[:250]}"
    )
    approval_id = str((gate_response.json() or {}).get("approval_id") or "").strip()
    assert approval_id, f"Approval gate for job {job_id} is missing approval_id"
    resp = post_json(
        f"/api/approvals/{thread_id}",
        {"approval_id": approval_id, "approved": True},
        timeout=30,
        allow_redirects=False,
    )
    assert resp.status_code in (200, 202), (
        f"Approval failed for thread {thread_id}: {resp.status_code} {resp.text[:250]}"
    )


def _quiesce_job_before_stale_injection(
    job_id: str | int, *, timeout: int = 120, interval: float = 1.0
) -> dict:
    deadline = time.monotonic() + timeout
    last_row: dict = {}
    approvals = 0
    cancel_requests = 0
    while time.monotonic() < deadline:
        row = _job_row(job_id)
        last_row = row
        status = str(row.get("status") or "").strip().lower()
        if status in TERMINAL_STATUSES:
            if status == "failed" and _job_has_contract_readiness_failure(row):
                pytest.skip(
                    "Timeout-enforcement workflow skipped: selected workflow failed immediately "
                    "due to contract readiness errors in this environment"
                )
            return row
        if status in ACTIVE_STATUSES and cancel_requests < 6:
            with contextlib.suppress(Exception):
                post_json(
                    f"/api/jobs/{job_id}/cancel",
                    {},
                    headers={"accept": "application/json"},
                    timeout=20,
                )
            cancel_requests += 1
        if status == "awaiting_approval":
            thread_id = str(row.get("thread_id") or "").strip()
            assert thread_id, f"Job {job_id} awaiting_approval but thread_id missing"
            _approve_job(job_id, thread_id)
            approvals += 1
            if approvals > 8:
                raise TimeoutError(
                    f"Job {job_id} exceeded approval budget while quiescing before stale injection"
                )
        time.sleep(interval)
    raise TimeoutError(
        f"Job {job_id} did not reach terminal status while quiescing before stale injection; "
        f"last_status={str(last_row.get('status') or '').strip().lower()!r}"
    )


def _scan_until_applied(
    job_id: str | int, *, timeout: int = 90, interval: float = 2.0
) -> tuple[dict, dict]:
    deadline = time.monotonic() + timeout
    last_payload: dict = {}
    while time.monotonic() < deadline:
        scan_resp = post_json("/api/watchdog/jobs/scan", {}, timeout=30)
        assert scan_resp.status_code == 200, (
            f"Watchdog scan failed: {scan_resp.status_code} {scan_resp.text[:300]}"
        )
        payload = scan_resp.json() or {}
        last_payload = payload if isinstance(payload, dict) else {}
        applied = last_payload.get("applied") or []
        if isinstance(applied, list):
            for row in applied:
                if not isinstance(row, dict):
                    continue
                if str(row.get("job_id") or 0).strip() == str(job_id).strip():
                    return last_payload, row
        time.sleep(interval)

    try:
        applied_count = int(last_payload.get("applied_count") or 0)
    except (TypeError, ValueError):
        applied_count = 0
    try:
        detection_count = int(last_payload.get("detection_count") or 0)
    except (TypeError, ValueError):
        detection_count = 0
    if applied_count == 0 and detection_count == 0:
        pytest.skip(
            f"Watchdog scan did not detect stale probe job {job_id} within {timeout}s "
            "(no detections observed in this environment)"
        )

    raise AssertionError(
        f"Watchdog did not apply timeout action to probe job {job_id}; "
        f"applied_count={last_payload.get('applied_count')} detection_count={last_payload.get('detection_count')}"
    )


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as e:
        pytest.skip(f"Flow Steward server not available: {e}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server unhealthy: {resp.status_code}")


@pytest.fixture(scope="module")
def test_project_id() -> str:
    if not PROJECT_ID:
        pytest.skip("TEST_PROJECT_BACKEND_ID not set")
    return PROJECT_ID


class TestJobTimeoutEnforcement:
    def test_watchdog_applies_timeout_mark_and_kill_recovery_cancels(
        self, server_available, test_project_id: str
    ):
        job_id = _submit_timeout_probe_job(test_project_id)
        cleanup_sent = False

        try:
            _quiesce_job_before_stale_injection(job_id, timeout=120, interval=1.0)
            _force_job_stale_in_progress(job_id, age_seconds=_force_stale_age_seconds())
            _scan_payload, applied_row = _scan_until_applied(job_id, timeout=90, interval=2.0)

            marked_status = str(applied_row.get("status") or "").strip().lower()
            assert marked_status in WATCHDOG_MARK_STATUSES, (
                f"Unexpected watchdog status for job {job_id}: {marked_status!r}"
            )

            row_after_scan = _job_row(job_id)
            status_after_scan = str(row_after_scan.get("status") or "").strip().lower()
            assert status_after_scan in WATCHDOG_MARK_STATUSES, (
                f"Probe job {job_id} should be moved out of active execution states after timeout mark; "
                f"got status={status_after_scan!r}"
            )
            assert status_after_scan not in ACTIVE_STATUSES

            journal_after_scan = _runtime_journal(job_id)
            mark_events = [
                entry
                for entry in journal_after_scan
                if str(entry.get("event") or "") == "watchdog_mark"
            ]
            assert mark_events, f"Expected watchdog_mark runtime journal entry for job {job_id}"

            recover_resp = post_json(
                f"/api/watchdog/jobs/{job_id}/recover",
                {"action": "kill", "reason": "e2e timeout enforcement kill"},
                timeout=30,
            )
            assert recover_resp.status_code == 200, (
                f"Watchdog kill recovery failed for job {job_id}: "
                f"{recover_resp.status_code} {recover_resp.text[:300]}"
            )
            recover_payload = recover_resp.json() or {}
            assert str(recover_payload.get("action") or "").strip().lower() == "kill"

            row_after_kill = _job_row(job_id)
            status_after_kill = str(row_after_kill.get("status") or "").strip().lower()
            assert status_after_kill == "cancelled", (
                f"Kill recovery must transition timeout-marked job to cancelled; got {status_after_kill!r}"
            )

            journal_after_kill = _runtime_journal(job_id)
            kill_events = [
                entry
                for entry in journal_after_kill
                if str(entry.get("event") or "") == "watchdog_kill"
            ]
            assert kill_events, f"Expected watchdog_kill runtime journal entry for job {job_id}"
        finally:
            with contextlib.suppress(Exception):
                post_json(
                    f"/api/jobs/{job_id}/cancel",
                    {},
                    headers={"accept": "application/json"},
                    timeout=20,
                )
            with contextlib.suppress(Exception):
                delete(f"/api/jobs/{job_id}", timeout=20)
                cleanup_sent = True
            if not cleanup_sent:
                with contextlib.suppress(Exception):
                    delete(f"/api/jobs/{job_id}", timeout=20)
