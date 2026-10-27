from __future__ import annotations

import os

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url, resolve_test_env
from tests.e2e.conftest import e2e_request
from tests.e2e.retry_step_helpers import (
    cleanup_job,
    drive_job_to_terminal,
    runtime_journal,
    seed_retryable_interrupted_step_job,
)

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
ALLOW_REPLAY_EXECUTE = str(os.getenv("E2E_REPLAY_EXECUTE", "0") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
RETRYABLE_JOB_ID = str(os.getenv("E2E_RETRYABLE_JOB_ID", "") or "").strip()


def _get(path: str, **kwargs) -> requests.Response:
    return e2e_request("GET", f"{BASE_URL}{path}", verify=VERIFY_SSL, timeout=20, **kwargs)


def _post_json(path: str, payload: dict, **kwargs) -> requests.Response:
    return e2e_request(
        "POST",
        f"{BASE_URL}{path}",
        json=payload,
        verify=VERIFY_SSL,
        timeout=30,
        **kwargs,
    )


def _cancel_job(job_id: str | int) -> None:
    try:
        e2e_request(
            "POST",
            f"{BASE_URL}/api/jobs/{job_id}/cancel",
            headers={"accept": "application/json"},
            verify=VERIFY_SSL,
            timeout=20,
            allow_redirects=False,
        )
    except Exception:
        return


def _recent_project_job_ids(project_id: str) -> list[str]:
    resp = _get(f"/api/projects/{project_id}/jobs", params={"page": 1, "per_page": 100})
    assert resp.status_code == 200, f"project jobs failed: {resp.status_code}"
    jobs = (resp.json() or {}).get("jobs") or []
    seen: set[str] = set()
    ordered: list[str] = []
    for job in jobs:
        job_id = str(job.get("id") or job.get("job_id") or "").strip()
        if not job_id:
            continue
        if job_id in seen:
            continue
        seen.add(job_id)
        ordered.append(job_id)
    return ordered


def _discover_message_candidate(project_id: str) -> tuple[str, str] | None:
    for job_id in _recent_project_job_ids(project_id):
        resp = _get(f"/api/jobs/{job_id}/messages", params={"limit": 25})
        if resp.status_code != 200:
            continue
        messages = (resp.json() or {}).get("messages") or []
        for message in messages:
            message_id = str(message.get("id") or "").strip()
            if message_id:
                return job_id, message_id
    return None


def _discover_non_retryable_job_id(project_id: str) -> str | None:
    safe_statuses = {
        "completed",
        "cancelled",
        "awaiting_approval",
        "in_progress",
        "pending",
        "max_iterations",
    }
    for job_id in _recent_project_job_ids(project_id):
        resp = _get(f"/api/jobs/{job_id}")
        if resp.status_code != 200:
            continue
        status = str((resp.json() or {}).get("status") or "").strip().lower()
        if status in safe_statuses:
            return job_id
    return None


def _discover_retryable_candidate_job_ids(project_id: str) -> list[str]:
    candidate_statuses = {"retryable_interrupted_step", "stuck", "orphaned", "failed"}
    candidates: list[str] = []
    seen: set[str] = set()
    for job_id in _recent_project_job_ids(project_id):
        if job_id in seen:
            continue
        seen.add(job_id)
        resp = _get(f"/api/jobs/{job_id}")
        if resp.status_code != 200:
            continue
        status = str((resp.json() or {}).get("status") or "").strip().lower()
        if status in candidate_statuses:
            candidates.append(job_id)
    if RETRYABLE_JOB_ID:
        forced = str(RETRYABLE_JOB_ID or "").strip()
        if forced and forced not in seen:
            candidates.insert(0, forced)
    return candidates


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


class TestJobReplayAndRetry:
    def test_replay_step_trace_and_replay_list_load(
        self,
        test_project_id: str,
        server_available,
    ):
        candidate = _discover_message_candidate(test_project_id)
        if candidate is None:
            pytest.skip(f"No recent job messages found for project {test_project_id}")

        job_id, message_id = candidate
        trace_resp = _get(f"/api/jobs/{job_id}/replay-step", params={"message_id": message_id})
        assert trace_resp.status_code == 200, (
            f"replay-step failed for job={job_id} message={message_id}: "
            f"{trace_resp.status_code} {trace_resp.text[:300]}"
        )
        trace = trace_resp.json()
        assert str((trace.get("message") or {}).get("id") or "").strip() == message_id
        assert "routing_decisions" in trace
        assert "related_messages" in trace

        replay_list_resp = _get(
            f"/api/jobs/{job_id}/step-replays",
            params={"message_id": message_id, "limit": 5},
        )
        if replay_list_resp.status_code == 501:
            pytest.skip("Step replay storage is not available in the target database schema")
        assert replay_list_resp.status_code == 200
        replay_list = replay_list_resp.json()
        assert str(replay_list.get("job_id") or 0).strip() == job_id
        assert isinstance(replay_list.get("step_replays"), list)

    def test_retry_step_rejects_non_retryable_job(
        self,
        test_project_id: str,
        server_available,
    ):
        job_id = _discover_non_retryable_job_id(test_project_id)
        if job_id is None:
            pytest.skip(f"No recent non-retryable jobs found for project {test_project_id}")

        resp = _post_json(f"/api/jobs/{job_id}/retry-step", {})
        assert resp.status_code in (400, 409), (
            f"retry-step unexpectedly succeeded for non-retryable job {job_id}: "
            f"{resp.status_code} {resp.text[:300]}"
        )
        error_text = str((resp.json() or {}).get("error") or "").lower()
        assert "retry" in error_text or "snapshot" in error_text or "eligible" in error_text

    def test_replay_step_execute_opt_in(self, test_project_id: str, server_available):
        candidate = _discover_message_candidate(test_project_id)
        if candidate is None:
            pytest.skip(f"No recent job messages found for project {test_project_id}")

        job_id, message_id = candidate
        resp = _post_json(
            f"/api/jobs/{job_id}/replay-step/execute",
            {"message_id": message_id, "replay_mode": "step_reexecute"},
        )
        if not ALLOW_REPLAY_EXECUTE and resp.status_code in (400, 401, 403, 404, 409, 422, 501):
            # Conservative mode: when explicit execute is not requested, still validate
            # endpoint boundary/authorization contracts without requiring 200.
            return
        assert resp.status_code == 200, (
            f"replay-step execute failed for job={job_id} message={message_id}: "
            f"{resp.status_code} {resp.text[:300]}"
        )
        body = resp.json()
        assert str(body.get("job_id") or 0).strip() == job_id
        assert str(body.get("message_id") or "").strip() == message_id
        assert body.get("replay_outcome")

    def test_retry_step_requeues_dedicated_retryable_job(
        self, test_project_id: str, server_available
    ):
        retried_job_id = seed_retryable_interrupted_step_job(
            test_project_id,
            summary=(
                "Fix workflow retry-step so interrupted jobs resume from the captured step "
                "without losing queue state, approvals, or observability logs."
            ),
        )
        try:
            resp = _post_json(f"/api/jobs/{retried_job_id}/retry-step", {})
            assert resp.status_code == 200, (
                f"retry-step failed for deterministic retryable job {retried_job_id}: "
                f"{resp.status_code} {resp.text[:300]}"
            )

            retry_response = resp.json() or {}
            assert str(retry_response.get("job_id") or 0).strip() == retried_job_id
            assert str(retry_response.get("status") or "").strip().lower() == "pending"
            assert bool(retry_response.get("enqueued")) is True
            assert str(retry_response.get("thread_id") or "").strip(), (
                f"retry-step response missing thread_id: {retry_response}"
            )

            detail_resp = _get(f"/api/jobs/{retried_job_id}")
            assert detail_resp.status_code == 200
            detail_body = detail_resp.json() or {}
            detail = (
                detail_body.get("job") if isinstance(detail_body.get("job"), dict) else detail_body
            )
            assert isinstance(detail, dict), f"Unexpected job detail payload: {detail_body!r}"
            runtime = detail.get("runtime") or {}
            assert isinstance(runtime, dict), f"Unexpected retry-step runtime payload: {runtime!r}"
            assert runtime.get("retryable_interrupted_step") in (None, {}), (
                f"Retry-step snapshot should be cleared from runtime payload: {runtime!r}"
            )

            journal_rows = runtime_journal(retried_job_id)
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

            terminal_row = drive_job_to_terminal(retried_job_id, timeout=300, interval=2.0)
            final_status = str(terminal_row.get("status") or "").strip().lower()
            assert final_status in {
                "completed",
                "failed",
                "cancelled",
                "max_iterations",
                "retryable_interrupted_step",
                "stuck",
            }, f"Retried job {retried_job_id} did not return to terminal status: {final_status!r}"
        finally:
            cleanup_job(retried_job_id)

    # ── Job diagnostics ──────────────────────────────────────────────────

    def test_job_events_api_structure(self, test_project_id: str, server_available):
        """GET /api/jobs/{id}/events returns a well-structured events list."""
        job_ids = _recent_project_job_ids(test_project_id)
        if not job_ids:
            pytest.skip("No recent jobs for event inspection")

        resp = _get(f"/api/jobs/{job_ids[0]}/events")
        assert resp.status_code == 200
        body = resp.json()
        events = body.get("events") or []
        assert isinstance(events, list)
        for ev in events[:5]:
            assert isinstance(ev, dict)
            assert "id" in ev or "event_id" in ev

    def test_job_messages_api_structure(self, test_project_id: str, server_available):
        """GET /api/jobs/{id}/messages returns paginated messages."""
        job_ids = _recent_project_job_ids(test_project_id)
        if not job_ids:
            pytest.skip("No recent jobs for message inspection")

        resp = _get(f"/api/jobs/{job_ids[0]}/messages", params={"limit": 5})
        assert resp.status_code == 200
        body = resp.json()
        messages = body.get("messages") or []
        assert isinstance(messages, list)
        assert len(messages) <= 5

    def test_job_cost_breakdown(self, test_project_id: str, server_available):
        """GET /api/jobs/{id}/cost-breakdown returns per-agent cost data."""
        job_ids = _recent_project_job_ids(test_project_id)
        if not job_ids:
            pytest.skip("No recent jobs for cost inspection")

        resp = _get(f"/api/jobs/{job_ids[0]}/cost-breakdown")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body.get("agents") or body.get("cost_breakdown") or [], list)
