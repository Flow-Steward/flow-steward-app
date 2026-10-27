"""Queued regression for RR-001: unresolved step failure cannot false-pass."""

from __future__ import annotations

import json
import os
import time

import pytest
import requests

from core.application.tooling.testing.runtime_env import _read_dotenv, resolve_base_url

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.e2e_auth_public]

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
TRANSIENT_PROJECTION_STATUSES = {502, 503, 504}


def _read_job_state(lines, *, deadline: float) -> dict:
    """Read the durable ``job.state`` frame from a job-scoped SSE stream."""
    event_name = ""
    data_lines: list[str] = []
    for raw_line in lines:
        if time.monotonic() >= deadline:
            raise AssertionError("job-scoped SSE did not emit state before deadline")
        line = str(raw_line or "")
        if line.startswith("event:"):
            event_name = line.partition(":")[2].strip()
        elif line.startswith("data:"):
            data_lines.append(line.partition(":")[2].strip())
        elif not line:
            if event_name == "job.state" and data_lines:
                return json.loads("\n".join(data_lines))
            event_name = ""
            data_lines = []
    raise AssertionError("job-scoped SSE did not emit a job.state frame")


def _terminal_stream_state(
    session: requests.Session,
    base_url: str,
    *,
    job_id: str,
) -> dict:
    """Retry transient stream startup and return the durable terminal projection."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with session.get(
            f"{base_url}/api/events/stream",
            params={"job_id": job_id},
            stream=True,
            verify=False,
            timeout=(10, 30),
        ) as stream:
            if stream.status_code in TRANSIENT_PROJECTION_STATUSES:
                time.sleep(1)
                continue
            assert stream.status_code == 200, stream.text[:500]
            return _read_job_state(
                stream.iter_lines(decode_unicode=True),
                deadline=deadline,
            )
    raise AssertionError("job-scoped SSE remained unavailable before deadline")


def _enabled() -> bool:
    return str(os.getenv("FS_RR001_FAILURE_TRUTH_E2E", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _credentials() -> tuple[str, str]:
    dotenv = _read_dotenv()
    email = str(os.getenv("E2E_ADMIN_USERNAME") or dotenv.get("E2E_ADMIN_USERNAME") or "")
    password = str(os.getenv("E2E_ADMIN_PASSWORD") or dotenv.get("E2E_ADMIN_PASSWORD") or "")
    assert email, "E2E admin credentials are required"
    assert password, "E2E admin credentials are required"
    return email, password


def _admin_session(base_url: str) -> requests.Session:
    email, password = _credentials()
    session = requests.Session()
    payload: dict[str, str] = {"email": email, "password": password}
    response = session.post(f"{base_url}/api/auth/login", json=payload, verify=False, timeout=20)
    if response.status_code == 409:
        body = response.json() or {}
        options = body.get("account_options") if isinstance(body, dict) else []
        default = next(
            (
                option
                for option in options or []
                if isinstance(option, dict) and option.get("is_default") is True
            ),
            None,
        )
        assert isinstance(default, dict), f"No default account in login response: {body}"
        payload["account_id"] = str(default.get("account_id") or "")
        response = session.post(
            f"{base_url}/api/auth/login", json=payload, verify=False, timeout=20
        )
    assert response.status_code == 200, response.text[:500]
    csrf = str((response.json() or {}).get("csrf_token") or session.cookies.get("fs_csrf") or "")
    assert csrf, "Authenticated session did not receive CSRF token"
    session.headers["x-csrf-token"] = csrf
    return session


def _approve_current_gate(
    session: requests.Session,
    base_url: str,
    *,
    job_id: str,
    thread_id: str,
) -> None:
    gate = session.get(f"{base_url}/api/jobs/{job_id}/approval", verify=False, timeout=20)
    assert gate.status_code == 200, gate.text[:500]
    approval_id = str((gate.json() or {}).get("approval_id") or "")
    assert approval_id
    decision = session.post(
        f"{base_url}/api/approvals/{thread_id}",
        json={"approval_id": approval_id, "approved": True, "comment": "RR-001 regression"},
        headers={
            "x-csrf-token": session.headers["x-csrf-token"],
            "Idempotency-Key": f"rr001:{approval_id}:approve",
        },
        verify=False,
        timeout=30,
    )
    assert decision.status_code in {200, 202}, decision.text[:500]


@pytest.mark.skipif(not _enabled(), reason="opt in with FS_RR001_FAILURE_TRUTH_E2E=1")
def test_queued_unhandled_dataset_failure_is_terminal_across_public_projections() -> None:
    dotenv = _read_dotenv()
    base_url = resolve_base_url(os.environ, include_dotenv=True).rstrip("/")
    project_id = str(
        os.getenv("E2E_RR001_PROJECT_ID") or dotenv.get("TEST_PROJECT_BACKEND_ID") or ""
    ).strip()
    workflow_id = str(os.getenv("E2E_RR001_WORKFLOW_ID") or "").strip()
    assert project_id, "E2E_RR001_PROJECT_ID and E2E_RR001_WORKFLOW_ID are required"
    assert workflow_id, "E2E_RR001_PROJECT_ID and E2E_RR001_WORKFLOW_ID are required"
    session = _admin_session(base_url)
    job_id = ""
    try:
        submitted = session.post(
            f"{base_url}/api/workflow-runs",
            json={
                "project_id": project_id,
                "workflow_id": workflow_id,
                "test_mode": True,
            },
            verify=False,
            timeout=30,
        )
        assert submitted.status_code == 200, submitted.text[:500]
        job_id = str((submitted.json() or {}).get("job_id") or "")
        assert job_id

        deadline = time.monotonic() + 240
        approvals = 0
        body: dict = {}
        while time.monotonic() < deadline:
            response = session.get(f"{base_url}/api/jobs/{job_id}", verify=False, timeout=20)
            if response.status_code in TRANSIENT_PROJECTION_STATUSES:
                time.sleep(1)
                continue
            assert response.status_code == 200, response.text[:500]
            body = response.json() or {}
            job = body.get("job") or body
            status = str(job.get("status") or "").strip().lower()
            if status in TERMINAL_STATUSES:
                break
            if status == "awaiting_approval":
                thread_id = str(job.get("thread_id") or "")
                assert thread_id
                _approve_current_gate(
                    session,
                    base_url,
                    job_id=job_id,
                    thread_id=thread_id,
                )
                approvals += 1
                assert approvals <= 2, "workflow continued to downstream approval after failure"
            time.sleep(1)
        else:
            pytest.fail(f"Job {job_id} did not reach terminal state")

        job = body.get("job") or body
        workflow_run = body.get("workflow_run") or {}
        primary_error = str(job.get("error") or job.get("last_error") or "")
        assert job.get("status") == "failed"
        assert "Dataset is not assigned to this project" in primary_error
        assert workflow_run.get("status") == "failed"
        assert workflow_run.get("terminal") is True

        stream_state = _terminal_stream_state(
            session,
            base_url,
            job_id=job_id,
        )
        assert stream_state.get("status") == "failed"
        assert stream_state.get("terminal") is True
        stream_error = stream_state.get("error") or {}
        assert isinstance(stream_error, dict)
        assert str(stream_error.get("code") or "").strip()
        assert str(stream_error.get("message") or "").strip()
    finally:
        if job_id and str(os.getenv("FS_RR001_KEEP_FIXTURE", "0")).strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            session.delete(f"{base_url}/api/jobs/{job_id}", verify=False, timeout=20)
            session.post(
                f"{base_url}/api/trash/job/{job_id}/hard-delete",
                json={},
                verify=False,
                timeout=30,
            )
