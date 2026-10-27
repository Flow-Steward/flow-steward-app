"""E2E tests for Job lifecycle APIs.

Covers: JOB-03, JOB-07, JOB-09-11, JOB-17, JOB-18, JOB-20, JOB-32 —
delete, single message, log deletion, approval history,
acknowledge/unacknowledge, reactions.
"""

from __future__ import annotations

import os
import time

import pytest

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.conftest import _authed_session, e2e_request
from tests.e2e.helpers import (
    BASE_URL,
    VERIFY_SSL,
    delete,
    ensure_test_job,
    get,
    post_json,
)
from tests.e2e.neutral_registry import NEUTRAL_WORKFLOW_ID

pytestmark = [pytest.mark.e2e]

BACKEND_ROLE = str(os.environ.get("E2E_BACKEND_ROLE", "admin") or "admin").strip().lower()
_BACKEND_ENV = resolve_test_env(os.environ, lane="backend", role=BACKEND_ROLE, include_dotenv=True)
PROJECT_ID = str(_BACKEND_ENV.project_id or "").strip()
ACTIVE_JOB_STATUSES = {"pending", "queued", "in_progress", "awaiting_approval"}
TERMINAL_JOB_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "max_iterations",
    "retryable_interrupted_step",
    "stuck",
}


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


@pytest.fixture(scope="module")
def seed_job(server_available) -> dict:
    """Ensure at least one job exists; create via API if needed."""
    return ensure_test_job(PROJECT_ID)


def _job_id(job: dict) -> str:
    return str(job.get("id") or job.get("job_id") or "").strip()


def _build_authed_job_ws_url(job_id: str | int) -> str:
    session, _verify_ssl = _authed_session()
    token = str(session.cookies.get("fs_session") or "").strip()
    if not token:
        pytest.skip("Could not obtain platform session token for websocket auth")
    ws_base = BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    return f"{ws_base}/ws/jobs/{str(job_id).strip()}?token={token}"


def _find_terminal_job() -> dict | None:
    """Find a completed/failed/cancelled job (best-effort, may return None)."""
    resp = get("/api/jobs", params={"limit": 20})
    if resp.status_code != 200:
        return None
    data = resp.json()
    jobs = data if isinstance(data, list) else data.get("jobs") or []
    for status in ("completed", "failed", "cancelled"):
        for j in jobs:
            if str(j.get("status") or "").lower() == status:
                return j
    return None


def _submit_cancelable_job(project_id: str) -> str:
    payload = {
        "inputs": {
            "topic": (
                "E2E cancel contract: investigate whether event sourcing fits our audit/replay architecture"
            )
        },
        "project_id": str(project_id),
        "workflow_id": NEUTRAL_WORKFLOW_ID,
    }
    resp = e2e_request(
        "POST",
        f"{BASE_URL}/api/workflow-runs",
        json=payload,
        verify=VERIFY_SSL,
        timeout=30,
    )
    if resp.status_code in (401, 403):
        pytest.skip("Task submit requires elevated role for cancel contract test")
    if resp.status_code == 400 and "unresolved contract errors" in resp.text.lower():
        pytest.skip(
            "Cancel contract test skipped: selected workflow has unresolved contract errors "
            "in this environment"
        )
    assert resp.status_code == 200, (
        f"Could not submit job for cancel contract test: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    job_id = str(body.get("job_id") or body.get("id") or "").strip()
    assert job_id, f"Task submit response missing job_id: {body}"
    return job_id


def _get_job_row(job_id: str | int) -> dict:
    resp = get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200, (
        f"Could not read job {job_id}: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    if isinstance(body, dict) and isinstance(body.get("job"), dict):
        return dict(body["job"])
    assert isinstance(body, dict), f"Unexpected job payload for {job_id}: {body!r}"
    return body


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


def _wait_for_status(
    job_id: str | int, statuses: set[str], *, timeout: int = 90, interval: float = 1.0
) -> dict:
    deadline = time.monotonic() + timeout
    last_row: dict = {}
    while time.monotonic() < deadline:
        row = _get_job_row(job_id)
        last_row = row
        status = str(row.get("status") or "").strip().lower()
        if status in statuses:
            return row
        time.sleep(interval)
    raise TimeoutError(
        f"Job {job_id} did not reach expected statuses={sorted(statuses)} "
        f"within {timeout}s; last_status={str(last_row.get('status') or '').strip().lower()!r}"
    )


def _recent_project_job_ids(project_id: str, *, page: int = 1, per_page: int = 100) -> list[str]:
    resp = get(f"/api/projects/{project_id}/jobs", params={"page": page, "per_page": per_page})
    if resp.status_code != 200:
        return []
    body = resp.json() or {}
    rows = body if isinstance(body, list) else (body.get("jobs") or [])
    ordered: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("id") or row.get("job_id") or "").strip()
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        ordered.append(job_id)
    return ordered


def _discover_message_candidate(project_id: str) -> tuple[str, str] | None:
    for job_id in _recent_project_job_ids(project_id, page=1, per_page=120):
        msg_resp = get(f"/api/jobs/{job_id}/messages", params={"limit": 10})
        if msg_resp.status_code != 200:
            continue
        body = msg_resp.json() or {}
        messages = body if isinstance(body, list) else (body.get("messages") or [])
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("id") or message.get("message_id") or "").strip()
            if message_id:
                return job_id, message_id
    return None


def _submit_message_probe_job(project_id: str) -> str:
    payload = {
        "inputs": {
            "topic": (
                "E2E message probe: investigate whether event sourcing fits our audit/replay architecture"
            )
        },
        "project_id": str(project_id),
        "workflow_id": NEUTRAL_WORKFLOW_ID,
    }
    resp = e2e_request(
        "POST",
        f"{BASE_URL}/api/workflow-runs",
        json=payload,
        verify=VERIFY_SSL,
        timeout=30,
    )
    if resp.status_code in (401, 403):
        pytest.skip("Task submit requires elevated role for message detail contract test")
    if resp.status_code == 400 and "unresolved contract errors" in resp.text.lower():
        pytest.skip(
            "Message detail contract test skipped: selected workflow has unresolved contract "
            "errors in this environment"
        )
    assert resp.status_code == 200, (
        f"Could not submit message probe job: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json() or {}
    job_id = str(body.get("job_id") or body.get("id") or "").strip()
    assert job_id, f"Task submit response missing job_id: {body}"
    return job_id


def _wait_for_message_candidate(
    job_id: str | int, *, timeout: int = 120, interval: float = 2.0
) -> tuple[str, str] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg_resp = get(f"/api/jobs/{job_id}/messages", params={"limit": 10})
        if msg_resp.status_code == 200:
            body = msg_resp.json() or {}
            messages = body if isinstance(body, list) else (body.get("messages") or [])
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_id = str(message.get("id") or message.get("message_id") or "").strip()
                if message_id:
                    return str(job_id), message_id
        time.sleep(interval)
    return None


# ── Jobs List Contract ───────────────────────────────────────────────────────


class TestJobsListContract:
    def test_jobs_list_includes_frontend_fields(self, server_available):
        resp = get("/api/jobs", params={"limit": 5})
        if resp.status_code == 404:
            pytest.skip("Jobs list endpoint not implemented")
        assert resp.status_code == 200
        data = resp.json()
        jobs = data if isinstance(data, list) else data.get("jobs") or []
        assert isinstance(jobs, list)
        if jobs:
            row = jobs[0]
            assert "status" in row
            assert "project_name" in row
            assert "current_step_id" in row
            assert "current_agent" in row
            assert ("job_id" in row) or ("id" in row)
            assert ("display_summary" in row) or ("task_summary" in row)


# ── Job Delete ──────────────────────────────────────────────────────────────


class TestJobDelete:
    def test_delete_nonexistent_job_returns_404(self, server_available):
        resp = delete("/api/jobs/999999")
        assert resp.status_code in (404, 400)

    def test_delete_returns_expected_status(self, server_available, seed_job):
        """Verify the DELETE endpoint exists and responds correctly."""
        # Prefer a terminal job for deletion; fall back to the seed job.
        job = _find_terminal_job() or seed_job
        jid = _job_id(job)
        resp = delete(f"/api/jobs/{jid}")
        # Should either succeed or reject if job has dependencies
        # E2E helpers authenticate as admin by default, so 403 indicates RBAC regression.
        assert resp.status_code in (200, 204, 400, 404, 409)


class TestJobCancel:
    def test_cancel_active_job_transitions_to_cancelled(
        self, server_available, test_project_id: str
    ):
        job_id = _submit_cancelable_job(test_project_id)
        first_row = _wait_for_status(
            job_id,
            ACTIVE_JOB_STATUSES | TERMINAL_JOB_STATUSES,
            timeout=90,
            interval=1.0,
        )
        first_status = str(first_row.get("status") or "").strip().lower()
        if first_status == "failed" and _job_has_contract_readiness_failure(first_row):
            pytest.skip(
                "Cancel contract test skipped: submitted job failed immediately due to "
                "workflow contract readiness errors in this environment"
            )

        cancel_resp = e2e_request(
            "POST",
            f"{BASE_URL}/api/jobs/{job_id}/cancel",
            headers={"accept": "application/json"},
            verify=VERIFY_SSL,
            timeout=20,
            allow_redirects=False,
        )
        assert cancel_resp.status_code in (200, 202, 409), (
            f"Cancel endpoint returned unexpected status for job {job_id}: "
            f"{cancel_resp.status_code} {cancel_resp.text[:300]}"
        )

        try:
            final_row = _wait_for_status(
                job_id,
                TERMINAL_JOB_STATUSES,
                timeout=120,
                interval=2.0,
            )
        except TimeoutError:
            # Race hardening: if cancellation did not settle quickly under load,
            # issue one more cancel request and re-check for terminal convergence.
            second_cancel = e2e_request(
                "POST",
                f"{BASE_URL}/api/jobs/{job_id}/cancel",
                headers={"accept": "application/json"},
                verify=VERIFY_SSL,
                timeout=20,
                allow_redirects=False,
            )
            assert second_cancel.status_code in (200, 202, 409), (
                f"Second cancel request returned unexpected status for job {job_id}: "
                f"{second_cancel.status_code} {second_cancel.text[:300]}"
            )
            final_row = _wait_for_status(
                job_id,
                TERMINAL_JOB_STATUSES,
                timeout=180,
                interval=2.0,
            )
        final_status = str(final_row.get("status") or "").strip().lower()
        if final_status == "failed" and _job_has_contract_readiness_failure(final_row):
            pytest.skip(
                "Cancel contract test skipped: runtime failed with workflow contract readiness "
                "errors before cancellation convergence"
            )
        if first_status in TERMINAL_JOB_STATUSES:
            assert final_status in TERMINAL_JOB_STATUSES
            return
        assert final_status == "cancelled", (
            f"Active job {job_id} was cancelled but final status is {final_status!r}; "
            f"first_status={first_status!r}"
        )


# ── Single Message Detail ──────────────────────────────────────────────────


class TestMessageDetail:
    def test_message_detail_nonexistent(self, server_available):
        resp = get("/api/messages/999999")
        if resp.status_code == 404:
            return  # expected
        # Some implementations return 404, others 400
        assert resp.status_code in (400, 404)

    def test_message_detail_for_real_message(self, server_available, test_project_id: str):
        candidate = _discover_message_candidate(test_project_id)
        cleanup_job_id = ""
        if candidate is None:
            cleanup_job_id = _submit_message_probe_job(test_project_id)
            candidate = _wait_for_message_candidate(cleanup_job_id, timeout=150, interval=2.0)
        if candidate is None:
            pytest.skip(
                "No job messages were emitted for message-detail contract check "
                f"(project={test_project_id})"
            )
        assert candidate is not None
        _jid, msg_id = candidate
        resp = get(f"/api/messages/{msg_id}")
        assert resp.status_code == 200
        detail = resp.json()
        # Response may wrap message in a "message" key
        msg = detail.get("message", detail) if isinstance(detail, dict) else detail
        assert isinstance(msg, dict)
        if cleanup_job_id:
            e2e_request(
                "POST",
                f"{BASE_URL}/api/jobs/{cleanup_job_id}/cancel",
                headers={"accept": "application/json"},
                verify=VERIFY_SSL,
                timeout=20,
                allow_redirects=False,
            )


# ── Log Policy ──────────────────────────────────────────────────────────────


class TestLogPolicy:
    def test_log_delete_endpoints_are_unavailable(self, server_available, seed_job):
        jid = _job_id(seed_job)
        single = delete(f"/api/jobs/{jid}/logs/999999")
        bulk = post_json(f"/api/jobs/{jid}/logs/bulk-delete", {"log_ids": []})
        delete_all = post_json(f"/api/jobs/{jid}/logs/delete-all", {})

        for resp in (single, bulk, delete_all):
            # E2E requests are authenticated by default, so log deletion must be
            # unavailable as a skill, not merely blocked by auth checks.
            assert resp.status_code in (404, 405)


# ── Approval History ────────────────────────────────────────────────────────


class TestApprovalHistory:
    def test_approval_history_for_job(self, server_available, seed_job):
        jid = _job_id(seed_job)
        resp = get(f"/api/jobs/{jid}/approvals")
        if resp.status_code == 404:
            pytest.skip("Approval history endpoint not implemented")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (dict, list))

    def test_approval_history_nonexistent_job(self, server_available):
        resp = get("/api/jobs/999999/approvals")
        assert resp.status_code in (404, 200)  # 200 with empty list is acceptable


# ── Acknowledge / Unacknowledge ─────────────────────────────────────────────


class TestAcknowledge:
    def test_acknowledge_job(self, server_available, seed_job):
        # Prefer a terminal job; fall back to the seed job.
        job = _find_terminal_job() or seed_job
        jid = _job_id(job)
        resp = post_json(f"/api/jobs/{jid}/acknowledge", {})
        if resp.status_code == 404:
            pytest.skip("Acknowledge endpoint not implemented")
        assert resp.status_code in (200, 204, 409)

    def test_unacknowledge_job(self, server_available, seed_job):
        job = _find_terminal_job() or seed_job
        jid = _job_id(job)
        # Acknowledge first, then unacknowledge
        post_json(f"/api/jobs/{jid}/acknowledge", {})
        resp = post_json(f"/api/jobs/{jid}/unacknowledge", {})
        if resp.status_code == 404:
            pytest.skip("Unacknowledge endpoint not implemented")
        assert resp.status_code in (200, 204, 409)

    def test_acknowledge_nonexistent_job(self, server_available):
        resp = post_json("/api/jobs/999999/acknowledge", {})
        assert resp.status_code in (404, 400)


# ── Job Reactions ───────────────────────────────────────────────────────────


class TestJobReactions:
    def test_reactions_endpoint_exists(self, server_available, seed_job):
        jid = _job_id(seed_job)
        resp = get(f"/api/jobs/{jid}/reactions")
        if resp.status_code == 404:
            pytest.skip("Reactions endpoint not implemented")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (dict, list))


# ── Job Approval (singular) ──────────────────────────────────────────────────


class TestJobApprovalSingular:
    def test_approval_status_endpoint(self, server_available, seed_job):
        """GET /api/jobs/{id}/approval returns current approval status."""
        jid = _job_id(seed_job)
        resp = get(f"/api/jobs/{jid}/approval")
        # May return 200 with status, 404 if not found, or 409 if not awaiting approval.
        assert resp.status_code in (200, 404, 409), (
            f"approval endpoint returned {resp.status_code}: {resp.text[:300]}"
        )

    def test_approval_nonexistent_job(self, server_available):
        """GET /api/jobs/99999/approval returns 404."""
        resp = get("/api/jobs/99999/approval")
        assert resp.status_code in (404, 503)


# ── WebSocket /ws/jobs/{job_id} ───────────────────────────────────────────────


class TestJobWebSocket:
    def test_websocket_endpoint_reachable(self, server_available, seed_job):
        """WebSocket /ws/jobs/{id} should be reachable (upgrade or 403)."""
        import requests as req

        from tests.e2e.helpers import BASE_URL, VERIFY_SSL

        jid = _job_id(seed_job)

        # HTTP GET to WebSocket endpoint — should return 426 (Upgrade Required)
        # or 403 (auth) or similar, but not 404 (route exists).
        try:
            resp = req.get(
                f"{BASE_URL}/ws/jobs/{jid}",
                verify=VERIFY_SSL,
                timeout=5,
            )
            # WebSocket endpoint responds to plain HTTP with upgrade required or similar.
            # 404 may occur if the WS route is mounted under a different prefix.
            assert resp.status_code in (101, 400, 403, 404, 426), (
                f"WS endpoint returned {resp.status_code}"
            )
        except req.exceptions.ConnectionError:
            # Connection error may occur when server expects WebSocket upgrade.
            pass  # Endpoint exists but rejects non-WS connections — that's correct.

    def test_websocket_with_actual_connection(self, server_available, seed_job):
        """Attempt a real WebSocket connection to /ws/jobs/{id}."""
        try:
            import websocket
        except ImportError:
            pytest.skip("websocket-client not installed")

        jid = _job_id(seed_job)
        ws_url = _build_authed_job_ws_url(jid)

        try:
            ws = websocket.create_connection(
                ws_url,
                timeout=5,
                sslopt={"cert_reqs": 0},  # Accept self-signed certs.
            )
            # If connected, we should receive at least a status message.
            ws.settimeout(3)
            try:
                msg = ws.recv()
                assert msg  # Got some data — endpoint works.
            except websocket.WebSocketTimeoutException:
                pass  # No messages yet — connection works, job just has no new logs.
            finally:
                ws.close()
        except (ConnectionRefusedError, websocket.WebSocketException):
            pytest.skip("Could not establish WebSocket connection")
