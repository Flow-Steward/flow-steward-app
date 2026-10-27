from __future__ import annotations

from tests.unit.test_external_agent_api import (
    _auth_headers,
    _client_for_principal,
    _FakeExternalAgentLogger,
    _principal_with_scopes,
)


def test_external_agent_full_lifecycle_updates_queue_jobs_and_messages():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes(
        "agent:register",
        "agent:heartbeat",
        "agent:claim",
        "agent:report",
        # Reading back the queue/jobs/messages goes through the normal project
        # read paths, which require project/job read scopes after the
        # token-scope intersection hardening.
        "projects.read",
        "jobs.read",
    )

    with _client_for_principal(principal, logger_obj) as client:
        queue_before = client.get("/api/projects/7/queue", headers=_auth_headers())
        register = client.post(
            "/api/external-agents/register",
            json={
                "agent_name": "ext-worker",
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "workflow_id": "wf_demo",
                "skills": ["review"],
            },
            headers=_auth_headers(),
        )
        heartbeat = client.post(
            "/api/external-agents/heartbeat",
            json={"worker_instance_id": "worker-1", "project_id": 7},
            headers=_auth_headers(),
        )
        queue_during = client.get("/api/projects/7/queue", headers=_auth_headers())
        claim = client.post(
            "/api/external-agents/claim",
            json={
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "workflow_id": "wf_demo",
                "lease_token": heartbeat.json()["lease_token"],
                "agent_name": "ext-worker",
            },
            headers=_auth_headers(),
        )
        report = client.post(
            "/api/external-agents/report",
            json={
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "job_id": 41,
                "lease_token": claim.json()["lease_token"],
                "claim_token": claim.json()["job"]["claim_token"],
                "agent_name": "ext-worker",
                "step_id": "final_review",
                "skill": "review",
                "status": "pass",
                "summary": "Review finished cleanly",
                "artifacts": {"checks": ["lint", "tests"]},
                "completed": True,
            },
            headers=_auth_headers(),
        )
        queue_after = client.get("/api/projects/7/queue", headers=_auth_headers())
        jobs = client.get("/api/projects/7/jobs", headers=_auth_headers())
        messages = client.get("/api/jobs/41/messages", headers=_auth_headers())

    assert queue_before.status_code == 200
    assert queue_before.json()["pending_count"] == 1
    assert queue_before.json()["lease_holder"] is None

    assert register.status_code == 200
    assert heartbeat.status_code == 200
    assert queue_during.status_code == 200
    assert queue_during.json()["lease_holder"] == "worker-1"
    assert queue_during.json()["project_lease"]["worker_instance_id"] == "worker-1"

    assert claim.status_code == 200
    assert claim.json()["claimed"] is True

    assert report.status_code == 200
    assert report.json()["job_status"] == "completed"
    assert report.json()["lease_released"] is True

    assert queue_after.status_code == 200
    assert queue_after.json()["pending_count"] == 0
    assert queue_after.json()["lease_holder"] is None

    assert jobs.status_code == 200
    jobs_body = jobs.json()
    assert jobs_body["total"] == 1
    assert jobs_body["jobs"][0]["id"] == "41"
    assert jobs_body["jobs"][0]["status"] == "completed"
    assert jobs_body["jobs"][0]["task_summary"] == "Review this task"

    assert messages.status_code == 200
    messages_body = messages.json()
    assert messages_body["has_more"] is False
    assert messages_body["messages"][0]["status"] == "pass"
    assert messages_body["messages"][0]["summary"] == "Review finished cleanly"


def test_external_agent_failure_report_is_visible_through_normal_read_paths():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes(
        "agent:claim",
        "agent:report",
        # Failure report is read back via the normal project/job read paths,
        # which require project/job read scopes after the token-scope
        # intersection hardening.
        "projects.read",
        "jobs.read",
    )

    with _client_for_principal(principal, logger_obj) as client:
        claim = client.post(
            "/api/external-agents/claim",
            json={
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "workflow_id": "wf_demo",
                "agent_name": "ext-worker",
            },
            headers=_auth_headers(),
        )
        report = client.post(
            "/api/external-agents/report",
            json={
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "job_id": 41,
                "lease_token": claim.json()["lease_token"],
                "claim_token": claim.json()["job"]["claim_token"],
                "agent_name": "ext-worker",
                "step_id": "final_review",
                "skill": "review",
                "status": "fail",
                "summary": "Policy gate failed",
                "errors": ["missing-evidence"],
                "completed": True,
            },
            headers=_auth_headers(),
        )
        jobs = client.get("/api/projects/7/jobs", headers=_auth_headers())
        messages = client.get(
            "/api/jobs/41/messages",
            params={"status": "fail"},
            headers=_auth_headers(),
        )

    assert claim.status_code == 200
    assert report.status_code == 200
    assert report.json()["job_status"] == "failed"
    assert logger_obj.jobs[41]["status"] == "failed"
    assert logger_obj.jobs[41]["error"] == "Policy gate failed"

    assert jobs.status_code == 200
    assert jobs.json()["jobs"][0]["status"] == "failed"

    assert messages.status_code == 200
    assert messages.json()["messages"][0]["status"] == "fail"
    assert messages.json()["messages"][0]["errors"] == ["missing-evidence"]
