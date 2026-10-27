"""API-only contract test for the four public workflow integration stages.

The test is opt-in because it needs a provisioned project, a machine token, a
workflow that reaches a human gate, and a running Docker stack.  It deliberately
uses only the public launch, job, SSE, and approval resources.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


def _enabled() -> bool:
    return str(os.getenv("FS_PUBLIC_WORKFLOW_INTEGRATION_E2E", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _read_job_state(lines, *, deadline: float) -> tuple[str, dict]:
    """Read one durable ``job.state`` frame from an already-open SSE stream."""
    event_name = ""
    event_id = ""
    data_lines: list[str] = []
    for raw_line in lines:
        if time.monotonic() >= deadline:
            raise AssertionError("job-scoped SSE did not reach terminal state before deadline")
        line = str(raw_line or "")
        if line.startswith("event:"):
            event_name = line.partition(":")[2].strip()
        elif line.startswith("id:"):
            event_id = line.partition(":")[2].strip()
        elif line.startswith("data:"):
            data_lines.append(line.partition(":")[2].strip())
        elif not line and event_name == "job.state" and data_lines:
            return event_id, json.loads("\n".join(data_lines))
    raise AssertionError("job-scoped SSE did not emit a job.state frame")


def _result_contains_value(value: object, expected: str) -> bool:
    """Search structured workflow output without coupling the E2E to one result shape."""
    if isinstance(value, dict):
        return any(_result_contains_value(item, expected) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_result_contains_value(item, expected) for item in value)
    return str(value or "").strip() == expected


@pytest.mark.skipif(not _enabled(), reason="opt in with FS_PUBLIC_WORKFLOW_INTEGRATION_E2E=1")
def test_public_workflow_api_launch_sse_and_external_hitl() -> None:
    token = str(os.getenv("E2E_MACHINE_TOKEN") or "").strip()
    project_id = str(os.getenv("E2E_PUBLIC_WORKFLOW_PROJECT_ID") or "").strip()
    workflow_id = str(os.getenv("E2E_PUBLIC_WORKFLOW_ID") or "").strip()
    expected_step_id = str(os.getenv("E2E_PUBLIC_WORKFLOW_STEP_ID") or "").strip()
    if not token or not project_id or not workflow_id or not expected_step_id:
        pytest.skip(
            "E2E_MACHINE_TOKEN, E2E_PUBLIC_WORKFLOW_PROJECT_ID, E2E_PUBLIC_WORKFLOW_ID and "
            "E2E_PUBLIC_WORKFLOW_STEP_ID are required"
        )

    base_url = resolve_base_url(os.environ, include_dotenv=True).rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    expected_build_sha = str(os.getenv("E2E_PUBLIC_WORKFLOW_BUILD_SHA") or "").strip()
    expected_image_digest = str(os.getenv("E2E_PUBLIC_WORKFLOW_IMAGE_DIGEST") or "").strip()
    if expected_build_sha:
        health = requests.get(f"{base_url}/health", timeout=20)
        assert health.status_code == 200, health.text[:500]
        assert health.json().get("build_sha") == expected_build_sha
        assert health.json().get("image_digest") == expected_image_digest
    idem = f"public-workflow-{uuid.uuid4().hex}"
    fixture_invoice_id = "INV-E2E-WORKFLOW-7429"
    fixture_body = f"invoice_id={fixture_invoice_id}\namount=42.00\ncurrency=EUR\n".encode()
    payload = {
        "project_id": project_id,
        "workflow_id": workflow_id,
        "inputs": {
            "title": "Invoice workflow contract",
            "description": fixture_body.decode("utf-8"),
        },
    }
    launch = requests.post(
        f"{base_url}/api/workflow-runs",
        headers={**headers, "Idempotency-Key": idem},
        json=payload,
        timeout=30,
    )
    assert launch.status_code == 200, launch.text[:500]
    first = launch.json()
    job_id = str(first.get("job_id") or "").strip()
    assert job_id
    assert first.get("deduplicated") is False

    replay = requests.post(
        f"{base_url}/api/workflow-runs",
        headers={**headers, "Idempotency-Key": idem},
        json=payload,
        timeout=30,
    )
    assert replay.status_code == 200, replay.text[:500]
    assert replay.json().get("job_id") == job_id
    assert replay.json().get("deduplicated") is True

    deadline = time.monotonic() + 180
    observed_state = False
    observed_step = False
    approval_submitted = False
    approval_replayed = False
    terminal_stream_state: dict | None = None
    terminal_marker = ""
    run: dict = {}
    # Keep one live SSE connection from launch through approval and terminal
    # state.  Closing it after each frame can hide a stale marker regression.
    with requests.get(
        f"{base_url}/api/events/stream",
        params={"job_id": job_id},
        headers=headers,
        stream=True,
        timeout=(10, 30),
    ) as stream:
        assert stream.status_code == 200, stream.text[:500]
        stream_lines = stream.iter_lines(decode_unicode=True)
        while time.monotonic() < deadline:
            marker, stream_state = _read_job_state(stream_lines, deadline=deadline)
            observed_state = True
            observed_step = observed_step or any(
                str(step.get("step_id") or "") == expected_step_id
                for step in stream_state.get("steps") or []
                if isinstance(step, dict)
            )
            if stream_state.get("terminal"):
                terminal_stream_state = stream_state
                terminal_marker = marker

            # A newer transition may commit between reading the stream marker and
            # requesting the REST projection. Both views are durable revisioned
            # projections of the same job.
            response = requests.get(f"{base_url}/api/jobs/{job_id}", headers=headers, timeout=20)
            assert response.status_code == 200, response.text[:500]
            body = response.json()
            run = body.get("workflow_run") or {}
            assert run.get("job_id") == job_id
            assert run.get("project_id") == project_id
            assert run.get("workflow_id") == workflow_id
            # The public projection has no revision field. Compare a stable
            # snapshot only at an approval gate or terminal state; while a
            # graph is running, the GET projection may legitimately advance.
            if stream_state.get("terminal") or stream_state.get("status") == "awaiting_approval":
                assert run == stream_state

            if run.get("status") == "awaiting_approval" and not approval_submitted:
                approval = requests.get(
                    f"{base_url}/api/jobs/{job_id}/approval", headers=headers, timeout=20
                )
                assert approval.status_code == 200, approval.text[:500]
                gate = approval.json()
                thread_id = str((body.get("job") or {}).get("thread_id") or "").strip()
                assert thread_id
                approval_key = f"{idem}-approval"
                decision_payload = {
                    "approval_id": gate.get("approval_id"),
                    "approved": True,
                    "comment": "Approved by public integration fixture",
                    "external_actor": {
                        "id": "external-manager-e2e",
                        "display_name": "E2E manager",
                    },
                }
                decision = requests.post(
                    f"{base_url}/api/approvals/{thread_id}",
                    headers={**headers, "Idempotency-Key": approval_key},
                    json=decision_payload,
                    timeout=20,
                )
                assert decision.status_code == 202, decision.text[:500]
                approval_submitted = True
                replay_decision = requests.post(
                    f"{base_url}/api/approvals/{thread_id}",
                    headers={**headers, "Idempotency-Key": approval_key},
                    json=decision_payload,
                    timeout=20,
                )
                assert replay_decision.status_code == 202, replay_decision.text[:500]
                assert replay_decision.json().get("deduplicated") is True
                approval_replayed = True
            # REST may observe the committed terminal snapshot before this
            # streaming connection consumes the matching SSE frame.
            if terminal_stream_state is not None:
                break

    assert observed_state, "job-scoped SSE did not emit a durable job.state event"
    assert observed_step, f"job-scoped SSE did not expose configured step {expected_step_id!r}"
    assert approval_submitted, "workflow did not complete delegated approval replay"
    assert approval_replayed, "workflow did not complete delegated approval replay"
    assert run.get("terminal") is True, f"job did not reach terminal state: {run}"
    assert run.get("status") == "completed", f"workflow did not complete successfully: {run}"
    assert isinstance(run.get("result"), dict), f"completed workflow has no public result: {run}"
    terminal_artifacts = run["result"].get("artifacts")
    assert isinstance(terminal_artifacts, dict), (
        "protected public-workflow fixture must complete with at least one output artifact"
    )
    assert terminal_artifacts, (
        "protected public-workflow fixture must complete with at least one output artifact"
    )
    assert _result_contains_value(run["result"], fixture_invoice_id), (
        "workflow completed without proving that it read the uploaded invoice; "
        f"expected invoice id {fixture_invoice_id!r} from declared workflow inputs "
        f"in result: {run['result']}"
    )
    assert terminal_stream_state is not None, "SSE did not expose the terminal workflow state"
    assert terminal_stream_state.get("status") == run.get("status")
    assert terminal_marker, "terminal SSE state did not carry a reconnect cursor"

    # Repeated REST reads must be pure reads: they return the exact terminal
    # artifact projection and cannot create an artifact-free transition.
    for _ in range(2):
        terminal_get = requests.get(f"{base_url}/api/jobs/{job_id}", headers=headers, timeout=20)
        assert terminal_get.status_code == 200, terminal_get.text[:500]
        assert (terminal_get.json().get("workflow_run") or {}) == run

    # Terminal projections are replayed exactly once on reconnect, even when
    # the cursor already equals the terminal marker.
    reconnect_deadline = time.monotonic() + 30
    with requests.get(
        f"{base_url}/api/events/stream",
        params={"job_id": job_id},
        headers={**headers, "Last-Event-ID": terminal_marker},
        stream=True,
        timeout=(10, 30),
    ) as reconnect:
        assert reconnect.status_code == 200, reconnect.text[:500]
        reconnect_lines = reconnect.iter_lines(decode_unicode=True)
        replay_marker, replay_state = _read_job_state(reconnect_lines, deadline=reconnect_deadline)
        with pytest.raises(StopIteration):
            next(reconnect_lines)
    assert replay_marker == terminal_marker
    assert replay_state == run
