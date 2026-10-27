from __future__ import annotations

import copy
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.external_agents.reference_worker import ReferenceExternalAgentWorker

_OPTIONAL_TEST_DEPS = {
    "fastapi",
    "starlette",
    "pydantic",
    "jinja2",
    "psycopg_runtime",
    "langgraph",
    "langchain_core",
}


def _skip_only_missing_optional_dep(exc: Exception, *, context: str) -> None:
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") in _OPTIONAL_TEST_DEPS:
        pytest.skip(f"{context}: missing optional dependency '{exc.name}'")
    raise exc


class _FakeExternalAgentLogger:
    _TERMINAL_STATUSES: ClassVar[set[str]] = {
        "completed",
        "failed",
        "cancelled",
        "max_iterations",
        "preflight_failed",
    }

    def __init__(self, *, job_rows: list[dict] | None = None) -> None:
        self.now = datetime(2026, 3, 24, 10, 0, tzinfo=UTC)
        self.project = {
            "id": 7,
            "name": "Demo",
            "account_id": "acc_ops",
            "project_root": "/workspace/demo",
            "container_root": "/target/demo",
        }
        base_jobs = job_rows or [
            {
                "id": 41,
                "project_id": 7,
                "project_name": "Demo",
                "task_id": "TASK-41",
                "task_summary": "Review this task",
                "task_source": "external_agent",
                "thread_id": "thread-41",
                "mode": "external",
                "status": "pending",
                "queue_name": "",
                "selected_workflow": "wf_demo",
                "queued_at": self.now,
                "started_at": None,
                "finished_at": None,
                "meta": {
                    "external_agent": {
                        "enabled": True,
                        "workflow_id": "wf_demo",
                        "step_id": "final_review",
                        "skill": "review",
                        "input": {"checklist": ["lint", "tests"]},
                    }
                },
            }
        ]
        self.jobs = {
            int(row["id"]): copy.deepcopy(row)
            for row in base_jobs
            if isinstance(row, dict) and row.get("id") is not None
        }
        self.lease: dict | None = None
        self.agent_messages: list[tuple[int, dict]] = []
        self.agent_message_rows: list[dict] = []
        self.actions: list[dict] = []
        self._message_id = 0
        self._action_id = 0

    def advance_time(self, *, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=int(seconds))

    def _now(self) -> datetime:
        return self.now

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def _is_lease_active(self, lease: dict | None) -> bool:
        if not isinstance(lease, dict):
            return False
        expires_at = lease.get("expires_at")
        if not isinstance(expires_at, datetime):
            return False
        return expires_at > self._now()

    def _prune_lease_if_stale(self) -> None:
        if not isinstance(self.lease, dict):
            return
        active_job_id = int(self.lease.get("active_job_id") or 0) or None
        if not self._is_lease_active(self.lease):
            self.lease = None
            return
        if active_job_id is None:
            return
        job = self.jobs.get(active_job_id)
        if not isinstance(job, dict):
            return
        if str(job.get("status") or "") in self._TERMINAL_STATUSES:
            self.lease = None

    def _lease_payload(
        self,
        *,
        project_id: int,
        worker_instance_id: str,
        lease_token: str,
        ttl_seconds: int,
        drain_status: str,
        active_job_id: int | None,
    ) -> dict:
        now = self._now()
        return {
            "project_id": int(project_id),
            "worker_instance_id": str(worker_instance_id),
            "lease_token": str(lease_token),
            "heartbeat_at": now,
            "expires_at": now + timedelta(seconds=max(15, int(ttl_seconds or 45))),
            "acquired_at": self.lease.get("acquired_at") if isinstance(self.lease, dict) else now,
            "active_job_id": int(active_job_id) if active_job_id is not None else None,
            "drain_status": str(drain_status),
            "created_at": self.lease.get("created_at") if isinstance(self.lease, dict) else now,
            "updated_at": now,
        }

    def get_project(self, project_id: int) -> dict | None:
        if int(project_id) != int(self.project["id"]):
            return None
        return copy.deepcopy(self.project)

    def get_project_run_config(self, project_id: int) -> dict:
        if int(project_id) != int(self.project["id"]):
            return {}
        # Mirrors the real serializer's shape: the full run config includes
        # sensitive/broad fields (agents_config, raw config blob, project rules,
        # provider/connection metadata, assigned data connections, DB schema).
        # FS-DEEP-CAND-004 requires the claim response to drop all of these.
        return {
            "project_id": 7,
            "project_name": "Demo",
            "project_type": "it",
            "project_root": "/workspace/demo",
            "container_root": "/target/demo",
            "git_root": "/target/demo",
            "default_branch": "main",
            "supported_extensions": [".py"],
            "exclude_dirs": [".git"],
            "exclude_files": [],
            # --- sensitive / broad fields that must NOT reach a claim worker ---
            "agents_config": {"agents": {"config": {"checker": {"api_key": "sk-secret"}}}},
            "config": {"github_token": "ghp_secret"},
            "project_rules": "internal escalation contacts: ...",
            "project_connections": [{"id": "conn1", "secret": "shh"}],
            "assigned_data_connections": [{"connection_id": "db1", "dsn": "postgres://u:p@h/db"}],
            "project_db_schema": {"tables": ["users", "secrets"]},
            "project_providers": [{"provider": "anthropic", "api_key": "sk-prov"}],
            "quality_policy": {"resolved_checks": ["lint"]},
        }

    def acquire_project_execution_lease(
        self,
        *,
        project_id: int,
        worker_instance_id: str,
        lease_token: str | None = None,
        ttl_seconds: int = 45,
        drain_status: str = "acquired",
        active_job_id: int | None = None,
    ) -> dict | None:
        if int(project_id) != int(self.project["id"]):
            return None
        self._prune_lease_if_stale()
        normalized_worker = str(worker_instance_id)
        normalized_token = str(lease_token or "").strip()
        if (
            isinstance(self.lease, dict)
            and self._is_lease_active(self.lease)
            and str(self.lease.get("worker_instance_id") or "") == normalized_worker
            and normalized_token
            and normalized_token == str(self.lease.get("lease_token") or "")
        ):
            self.lease = self._lease_payload(
                project_id=project_id,
                worker_instance_id=normalized_worker,
                lease_token=normalized_token,
                ttl_seconds=ttl_seconds,
                drain_status=drain_status,
                active_job_id=active_job_id,
            )
            return copy.deepcopy(self.lease)
        if isinstance(self.lease, dict) and self._is_lease_active(self.lease):
            return None
        token = normalized_token or f"lease-{normalized_worker}"
        self.lease = self._lease_payload(
            project_id=project_id,
            worker_instance_id=normalized_worker,
            lease_token=token,
            ttl_seconds=ttl_seconds,
            drain_status=drain_status,
            active_job_id=active_job_id,
        )
        return copy.deepcopy(self.lease)

    def heartbeat_project_execution_lease(
        self,
        *,
        project_id: int,
        worker_instance_id: str,
        lease_token: str,
        ttl_seconds: int = 45,
        drain_status: str = "draining",
        active_job_id: int | None = None,
    ) -> dict | None:
        self._prune_lease_if_stale()
        if not self.lease:
            return None
        if (
            int(project_id) != int(self.lease["project_id"])
            or str(worker_instance_id) != str(self.lease["worker_instance_id"])
            or str(lease_token) != str(self.lease["lease_token"])
        ):
            return None
        self.lease = self._lease_payload(
            project_id=project_id,
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
            ttl_seconds=ttl_seconds,
            drain_status=drain_status,
            active_job_id=active_job_id,
        )
        return copy.deepcopy(self.lease)

    def list_project_execution_leases(
        self,
        *,
        active_only: bool = True,
        project_id: int | None = None,
        worker_instance_id: str | None = None,
        lease_token: str | None = None,
    ) -> list[dict]:
        self._prune_lease_if_stale()
        if not self.lease:
            return []
        if active_only and not self._is_lease_active(self.lease):
            return []
        if project_id is not None and int(project_id) != int(self.lease["project_id"]):
            return []
        if worker_instance_id and str(worker_instance_id) != str(self.lease["worker_instance_id"]):
            return []
        if lease_token and str(lease_token) != str(self.lease["lease_token"]):
            return []
        return [copy.deepcopy(self.lease)]

    def get_project_execution_lease(
        self,
        project_id: int,
        *,
        active_only: bool = True,
        worker_instance_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict | None:
        self._prune_lease_if_stale()
        if not self.lease or int(project_id) != int(self.lease["project_id"]):
            return None
        if active_only and not self._is_lease_active(self.lease):
            return None
        if worker_instance_id and str(worker_instance_id) != str(self.lease["worker_instance_id"]):
            return None
        if lease_token and str(lease_token) != str(self.lease["lease_token"]):
            return None
        return copy.deepcopy(self.lease)

    def release_project_execution_lease(
        self, *, project_id: int, worker_instance_id: str, lease_token: str
    ) -> bool:
        if not self.lease:
            return False
        if (
            int(project_id) != int(self.lease["project_id"])
            or str(worker_instance_id) != str(self.lease["worker_instance_id"])
            or str(lease_token) != str(self.lease["lease_token"])
        ):
            return False
        self.lease = None
        return True

    def claim_external_agent_job(
        self,
        *,
        project_id: int,
        worker_instance_id: str,
        workflow_id: str | None = None,
        allowed_workflow_ids: list[str] | None = None,
        claim_token: str | None = None,
        agent_name: str | None = None,
    ) -> dict | None:
        self._prune_lease_if_stale()
        effective_allowed = {
            str(item).strip() for item in (allowed_workflow_ids or []) if str(item).strip()
        }
        for job_id in sorted(self.jobs):
            job = self.jobs[job_id]
            ext = dict(job.get("meta", {}).get("external_agent", {}))
            effective_workflow = str(
                job.get("selected_workflow") or ext.get("workflow_id") or ""
            ).strip()
            if int(job.get("project_id") or 0) != int(project_id):
                continue
            if str(job.get("status") or "") != "pending":
                continue
            if str(job.get("queue_name") or ""):
                continue
            if not bool(ext.get("enabled")):
                continue
            if workflow_id and effective_workflow != str(workflow_id):
                continue
            if effective_allowed and effective_workflow not in effective_allowed:
                continue
            ext["claim_token"] = str(claim_token or "claim-1")
            ext["worker_instance_id"] = str(worker_instance_id)
            ext["agent_name"] = str(agent_name or worker_instance_id)
            ext["claimed_at"] = self._now_iso()
            job["meta"]["external_agent"] = ext
            job["status"] = "in_progress"
            job["worker_id"] = str(worker_instance_id)
            job["current_agent"] = str(agent_name or worker_instance_id)
            job["started_at"] = self._now()
            job["finished_at"] = None
            return copy.deepcopy(job)
        return None

    def get_job(self, job_id: int) -> dict | None:
        job = self.jobs.get(int(job_id))
        return copy.deepcopy(job) if isinstance(job, dict) else None

    def get_jobs(self, project_id: int, *, limit: int = 25, offset: int = 0) -> list[dict]:
        rows = [
            copy.deepcopy(job)
            for job in sorted(self.jobs.values(), key=lambda item: int(item.get("id") or 0))
            if int(job.get("project_id") or 0) == int(project_id)
        ]
        start = max(0, int(offset or 0))
        stop = start + max(1, int(limit or 25))
        return rows[start:stop]

    def count_jobs(self, project_id: int) -> int:
        return len(
            [1 for job in self.jobs.values() if int(job.get("project_id") or 0) == int(project_id)]
        )

    def get_pending_jobs(self, project_id: int) -> list[dict]:
        return [
            copy.deepcopy(job)
            for job in sorted(self.jobs.values(), key=lambda item: int(item.get("id") or 0))
            if int(job.get("project_id") or 0) == int(project_id)
            and str(job.get("status") or "") == "pending"
        ]

    def get_job_cost_summary(self, job_id: int) -> dict:
        _ = job_id
        return {}

    def update_job_runtime(self, job_id: int, **kwargs) -> None:
        job = self.jobs[int(job_id)]
        runtime = dict(job.get("runtime") or {})
        if kwargs.pop("clear_last_error", False):
            runtime.pop("last_error", None)
        if kwargs.pop("heartbeat_now", False):
            now = self._now()
            job["heartbeat_at"] = now
            runtime["heartbeat_at"] = now.isoformat()
        for key, value in kwargs.items():
            if value is None:
                continue
            if key == "selected_workflow":
                job["selected_workflow"] = value
            elif key in {"current_step_id", "current_group_id"}:
                job[key] = value
                runtime[key] = value
            elif key == "current_agent":
                job["current_agent"] = value
                runtime["current_agent"] = value
            else:
                runtime[key] = value
        job["runtime"] = runtime

    def merge_job_meta(self, job_id: int, patch: dict | None = None) -> None:
        if not isinstance(patch, dict):
            return
        job = self.jobs[int(job_id)]
        meta = dict(job.get("meta") or {})
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(meta.get(key), dict):
                merged = dict(meta[key])
                merged.update(value)
                meta[key] = merged
            else:
                meta[key] = value
        job["meta"] = meta

    def log_agent_message(self, job_id: int, envelope: dict) -> bool:
        self._message_id += 1
        self.agent_messages.append((int(job_id), copy.deepcopy(envelope)))
        self.agent_message_rows.append(
            {
                "id": self._message_id,
                "timestamp": self._now(),
                "job_id": int(job_id),
                "correlation_id": str(envelope.get("correlation_id") or ""),
                "schema_version": str(envelope.get("schema_version") or "agent_message_v1"),
                "from_agent": str(envelope.get("from_agent") or ""),
                "to_agent": str(envelope.get("to_agent") or ""),
                "step_id": str(envelope.get("step_id") or ""),
                "skill": str(envelope.get("skill") or ""),
                "status": str(envelope.get("status") or ""),
                "summary": str(envelope.get("summary") or ""),
                "artifacts": copy.deepcopy(envelope.get("artifacts") or {}),
                "errors": list(envelope.get("errors") or []),
                "envelope": copy.deepcopy(envelope),
            }
        )
        return True

    def get_agent_messages(
        self,
        job_id: int,
        *,
        since_id: int = 0,
        before_id: int | None = None,
        newest: bool = False,
        limit: int = 25,
        step_id: str | None = None,
        from_agent: str | None = None,
        skill: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        rows = [
            row
            for row in self.agent_message_rows
            if int(row.get("job_id") or 0) == int(job_id)
            and int(row.get("id") or 0) > int(since_id or 0)
        ]
        if before_id is not None:
            rows = [row for row in rows if int(row.get("id") or 0) < int(before_id)]
        if step_id:
            rows = [row for row in rows if str(row.get("step_id") or "") == str(step_id)]
        if from_agent:
            rows = [row for row in rows if str(row.get("from_agent") or "") == str(from_agent)]
        if skill:
            rows = [row for row in rows if str(row.get("skill") or "") == str(skill)]
        if status:
            rows = [row for row in rows if str(row.get("status") or "") == str(status)]
        rows = sorted(rows, key=lambda row: int(row.get("id") or 0), reverse=bool(newest))
        return [copy.deepcopy(row) for row in rows[: max(1, int(limit or 25))]]

    def log_action(self, **kwargs):
        self._action_id += 1
        row = copy.deepcopy(kwargs)
        row["id"] = self._action_id
        row["created_at"] = self._now()
        self.actions.append(row)
        return kwargs

    def update_job(self, job_id: int, status: str | None = None, **kwargs) -> None:
        job = self.jobs[int(job_id)]
        if status is not None:
            job["status"] = str(status)
        if job.get("status") in self._TERMINAL_STATUSES:
            job["finished_at"] = self._now()

    def finish_job(
        self, job_id: int, status: str = "completed", final_report=None, error=None, **kwargs
    ) -> None:
        job = self.jobs[int(job_id)]
        job["status"] = str(status)
        job["final_report"] = final_report
        job["error"] = error
        job["finished_at"] = self._now()


class _FakeTerminalPersistence:
    """Exercise the API terminal mapping without requiring a live SQL transaction."""

    def __init__(self, logger: _FakeExternalAgentLogger) -> None:
        self._logger = logger

    def complete_success(
        self,
        *,
        job_id: int,
        workflow_outputs: dict,
        final_report: str | None = None,
    ) -> None:
        _ = workflow_outputs
        self._logger.finish_job(job_id, status="completed", final_report=final_report)

    def complete_failure(
        self,
        *,
        job_id: int,
        status: str,
        error_message: str,
        error_code: str,
        category: str,
    ) -> None:
        _ = error_code, category
        self._logger.finish_job(job_id, status=status, error=error_message)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer machine-token"}


def _principal_with_scopes(*scopes: str) -> dict:
    return _machine_principal(
        scopes=list(scopes),
        allowed_project_ids=[7],
        allowed_workflow_ids=["wf_demo"],
    )


def _machine_principal(
    *, scopes: list[str], allowed_project_ids: list[int], allowed_workflow_ids: list[str]
) -> dict:
    return {
        "principal_type": "service_account",
        "service_account_id": 12,
        "service_account_key": "svc-ext",
        "display_name": "External Worker",
        "roles": ["operator"],
        "scopes": list(scopes),
        "auth_source": "machine",
        "account_id": "acc_ops",
        "allowed_project_ids": list(allowed_project_ids),
        "allowed_workflow_ids": list(allowed_workflow_ids),
        "is_active": True,
    }


@contextmanager
def _client_for_principal(principal: dict, logger_obj: _FakeExternalAgentLogger):
    try:
        from fastapi.testclient import TestClient

        from core.application.management.command_services_external_agent import (
            ExternalAgentCommandService,
        )
        from core.entrypoints.api.app_factory import create_app
    except Exception as exc:
        _skip_only_missing_optional_dep(exc, context="Could not create external agent TestClient")

    with patch(
        "core.entrypoints.api.runtime_port_manifest.RUNTIME_SERVICE_PORT_CONTRACTS",
        (),
    ):
        app = create_app()
    app.state.local_auth_enabled_override = lambda: True
    app.state.external_auth_enabled_override = lambda: False
    with (
        patch.dict(
            app.state.runtime_container.service_factories,
            {
                "db": lambda: logger_obj,
                "job_store": lambda: logger_obj,
                "external_agent_command_service": lambda: ExternalAgentCommandService(
                    get_logger=lambda: logger_obj,
                    terminal_persistence_factory=lambda logger: _FakeTerminalPersistence(logger),
                ),
            },
        ),
        patch(
            "core.entrypoints.api.route_registration._get_user_by_session_token",
            return_value=None,
        ),
        patch("core.application.auth.resolve.resolve_pat_principal", return_value=None),
        patch("core.application.auth.resolve.resolve_machine_principal", return_value=principal),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        try:
            yield client
        finally:
            client.close()


def _client_transport(client):
    def _transport(path: str, payload: dict, headers: dict[str, str]) -> tuple[int, dict]:
        resp = client.post(path, json=payload, headers=headers)
        return resp.status_code, resp.json()

    return _transport


def test_reference_external_worker_round_trips_ingress_lifecycle():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes(
        "agent:register",
        "agent:heartbeat",
        "agent:claim",
        "agent:report",
    )

    with _client_for_principal(principal, logger_obj) as client:
        worker = ReferenceExternalAgentWorker(
            bearer_token="machine-token",
            transport=_client_transport(client),
        )
        result = worker.run_once(
            project_id=7,
            worker_instance_id="worker-1",
            agent_name="ext-worker",
            workflow_id="wf_demo",
        )

    assert result is not None
    assert result["job_status"] == "completed"
    assert logger_obj.jobs[41]["status"] == "completed"
    assert logger_obj.lease is None
    assert logger_obj.agent_messages[0][1]["skill"] == "review"


def test_external_agent_register_route_returns_scope_contract():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:register")

    with _client_for_principal(principal, logger_obj) as client:
        resp = client.post(
            "/api/external-agents/register",
            json={
                "agent_name": "ext-worker",
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "workflow_id": "wf_demo",
                "skills": ["review"],
                "metadata": {"region": "eu"},
            },
            headers=_auth_headers(),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["registration_id"].startswith("reg_")
    assert body["agent_name"] == "ext-worker"
    assert body["worker_instance_id"] == "worker-1"
    assert body["principal_type"] == "service_account"
    assert body["allowed_project_ids"] == ["7"]
    assert body["allowed_workflow_ids"] == ["wf_demo"]
    assert body["project_id"] == "7"
    assert body["workflow_id"] == "wf_demo"
    assert body["scopes"] == ["agent:register"]
    assert logger_obj.lease is None
    assert logger_obj.jobs[41]["status"] == "pending"


def test_external_agent_heartbeat_route_acquires_and_refreshes_lease():
    logger_obj = _FakeExternalAgentLogger()
    # FS-DEEP-CAND-005: acquiring a new lease (no lease_token) requires
    # agent:claim; refreshing an existing lease only needs agent:heartbeat.
    principal = _principal_with_scopes("agent:heartbeat", "agent:claim")

    with _client_for_principal(principal, logger_obj) as client:
        first = client.post(
            "/api/external-agents/heartbeat",
            json={"worker_instance_id": "worker-1", "project_id": 7},
            headers=_auth_headers(),
        )
        first_body = first.json()
        assert first.status_code == 200
        assert first_body["status"] == "ok"
        assert first_body["lease_token"] == "lease-worker-1"
        first_expiry = logger_obj.lease["expires_at"]

        logger_obj.advance_time(seconds=5)
        second = client.post(
            "/api/external-agents/heartbeat",
            json={
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "lease_token": first_body["lease_token"],
                "active_job_id": 41,
            },
            headers=_auth_headers(),
        )

    assert second.status_code == 200
    second_body = second.json()
    assert second_body["lease_token"] == first_body["lease_token"]
    assert second_body["active_job_id"] == "41"
    assert logger_obj.lease["active_job_id"] == 41
    assert logger_obj.lease["drain_status"] == "external_active"
    assert logger_obj.lease["expires_at"] > first_expiry


def test_external_agent_claim_route_returns_job_contract_and_runtime_state():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:claim")

    with _client_for_principal(principal, logger_obj) as client:
        resp = client.post(
            "/api/external-agents/claim",
            json={
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "workflow_id": "wf_demo",
                "agent_name": "ext-worker",
            },
            headers=_auth_headers(),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["claimed"] is True
    assert body["lease_token"] == "lease-worker-1"
    assert body["job"]["job_id"] == "41"
    assert body["job"]["workflow_id"] == "wf_demo"
    assert body["job"]["step_id"] == "final_review"
    assert body["job"]["step_execution_id"] is None
    assert body["job"]["skill"] == "review"
    assert body["job"]["claim_token"]
    # FS-DEEP-CAND-004 positive: the legitimately-needed, non-sensitive runtime
    # fields are still present for the worker.
    project_config = body["job"]["input"]["project_config"]
    assert project_config["project_name"] == "Demo"
    assert project_config["project_id"] == 7
    assert logger_obj.jobs[41]["status"] == "in_progress"
    assert logger_obj.jobs[41]["worker_id"] == "worker-1"
    assert logger_obj.jobs[41]["current_agent"] == "ext-worker"
    assert logger_obj.jobs[41]["runtime"]["current_agent"] == "ext-worker"
    assert logger_obj.lease["active_job_id"] == 41
    assert logger_obj.lease["drain_status"] == "external_claimed"


def test_external_agent_claim_omits_sensitive_project_config_fields():
    """FS-DEEP-CAND-004 negative: the claim response must not leak broad/sensitive
    project configuration to a narrow agent:claim service-account credential."""
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:claim")

    with _client_for_principal(principal, logger_obj) as client:
        resp = client.post(
            "/api/external-agents/claim",
            json={
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "workflow_id": "wf_demo",
                "agent_name": "ext-worker",
            },
            headers=_auth_headers(),
        )

    assert resp.status_code == 200
    project_config = resp.json()["job"]["input"]["project_config"]
    for sensitive_field in (
        "agents_config",
        "config",
        "project_rules",
        "project_connections",
        "assigned_data_connections",
        "project_db_schema",
        "project_providers",
        "quality_policy",
        # project_root is a host-side path that the worker does not need.
        "project_root",
        # Repository fields were removed with the built-in code capabilities.
        "container_root",
        "default_branch",
        "supported_extensions",
    ):
        assert sensitive_field not in project_config, sensitive_field
    # No leaked value should appear anywhere in the serialized payload.
    serialized = resp.text
    for secret in ("sk-secret", "ghp_secret", "sk-prov", "postgres://u:p@h/db"):
        assert secret not in serialized, secret


def test_external_agent_heartbeat_only_principal_cannot_acquire_lease():
    """FS-DEEP-CAND-005 negative: a heartbeat-only credential (no agent:claim) is
    denied when it attempts to acquire a new execution lease, and the lease sink
    is never called."""
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:heartbeat")

    with _client_for_principal(principal, logger_obj) as client:
        resp = client.post(
            "/api/external-agents/heartbeat",
            json={"worker_instance_id": "worker-1", "project_id": 7},
            headers=_auth_headers(),
        )

    assert resp.status_code == 403
    # The lease sink must not have been reached.
    assert logger_obj.lease is None


def test_external_agent_heartbeat_only_principal_can_refresh_existing_lease():
    """FS-DEEP-CAND-005 positive: refreshing an existing lease (lease_token
    supplied) remains a pure heartbeat operation and does not require claim."""
    logger_obj = _FakeExternalAgentLogger()
    acquire_principal = _principal_with_scopes("agent:heartbeat", "agent:claim")

    with _client_for_principal(acquire_principal, logger_obj) as client:
        acquired = client.post(
            "/api/external-agents/heartbeat",
            json={"worker_instance_id": "worker-1", "project_id": 7},
            headers=_auth_headers(),
        )
    assert acquired.status_code == 200
    lease_token = acquired.json()["lease_token"]

    heartbeat_only = _principal_with_scopes("agent:heartbeat")
    with _client_for_principal(heartbeat_only, logger_obj) as client:
        refreshed = client.post(
            "/api/external-agents/heartbeat",
            json={
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "lease_token": lease_token,
            },
            headers=_auth_headers(),
        )

    assert refreshed.status_code == 200
    assert refreshed.json()["lease_token"] == lease_token


def test_external_agent_claim_principal_can_acquire_lease():
    """FS-DEEP-CAND-005 positive: a principal WITH agent:claim can acquire a lease
    via the heartbeat acquisition branch."""
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:heartbeat", "agent:claim")

    with _client_for_principal(principal, logger_obj) as client:
        resp = client.post(
            "/api/external-agents/heartbeat",
            json={"worker_instance_id": "worker-1", "project_id": 7},
            headers=_auth_headers(),
        )

    assert resp.status_code == 200
    assert resp.json()["lease_token"] == "lease-worker-1"
    assert logger_obj.lease is not None


def test_external_agent_report_route_publishes_message_and_releases_lease():
    logger_obj = _FakeExternalAgentLogger()
    # jobs.read is required to read back /api/jobs/{id}/messages over HTTP for a
    # machine principal (hardened scope gate); the worker reports and verifies.
    principal = _principal_with_scopes("agent:claim", "agent:report", "jobs.read")

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
        claim_body = claim.json()
        resp = client.post(
            "/api/external-agents/report",
            json={
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "job_id": 41,
                "lease_token": claim_body["lease_token"],
                "claim_token": claim_body["job"]["claim_token"],
                "agent_name": "ext-worker",
                "step_id": "final_review",
                "skill": "review",
                "status": "pass",
                "summary": "Looks good",
                "artifacts": {"score": 1},
                "completed": True,
            },
            headers=_auth_headers(),
        )
        messages = client.get("/api/jobs/41/messages", headers=_auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["job_status"] == "completed"
    assert body["message_published"] is True
    assert body["lease_released"] is True
    assert logger_obj.lease is None
    assert logger_obj.jobs[41]["status"] == "completed"
    assert logger_obj.jobs[41]["final_report"] == "Looks good"
    assert messages.status_code == 200
    messages_body = messages.json()
    assert messages_body["messages"][0]["summary"] == "Looks good"
    assert messages_body["messages"][0]["status"] == "pass"


def test_external_agent_claim_denies_project_outside_scope():
    logger_obj = _FakeExternalAgentLogger()
    principal = _machine_principal(
        scopes=["agent:claim"],
        allowed_project_ids=[99],
        allowed_workflow_ids=["wf_demo"],
    )

    with _client_for_principal(principal, logger_obj) as client:
        resp = client.post(
            "/api/external-agents/claim",
            json={"worker_instance_id": "worker-1", "project_id": 7, "workflow_id": "wf_demo"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 403


def test_external_agent_claim_denies_missing_agent_scope():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:heartbeat")

    with _client_for_principal(principal, logger_obj) as client:
        resp = client.post(
            "/api/external-agents/claim",
            json={"worker_instance_id": "worker-1", "project_id": 7, "workflow_id": "wf_demo"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 403


def test_external_agent_claim_denies_workflow_outside_scope():
    logger_obj = _FakeExternalAgentLogger()
    principal = _machine_principal(
        scopes=["agent:claim"],
        allowed_project_ids=[7],
        allowed_workflow_ids=["wf_other"],
    )

    with _client_for_principal(principal, logger_obj) as client:
        resp = client.post(
            "/api/external-agents/claim",
            json={"worker_instance_id": "worker-1", "project_id": 7, "workflow_id": "wf_demo"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 403


def _assert_request_validation_failed(resp) -> None:
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert body["message"] == "Request validation failed"
    assert body["correlation_id"]


def test_external_agent_claim_rejects_non_scalar_project_id_with_422():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:claim")

    with _client_for_principal(principal, logger_obj) as client:
        resp = client.post(
            "/api/external-agents/claim",
            json={"worker_instance_id": "worker-1", "project_id": ["7"], "workflow_id": "wf_demo"},
            headers=_auth_headers(),
        )

    _assert_request_validation_failed(resp)


def test_external_agent_register_rejects_non_scalar_project_id_with_422():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:register")

    with _client_for_principal(principal, logger_obj) as client:
        resp = client.post(
            "/api/external-agents/register",
            json={
                "agent_name": "ext-worker",
                "worker_instance_id": "worker-1",
                "project_id": ["7"],
                "workflow_id": "wf_demo",
            },
            headers=_auth_headers(),
        )

    _assert_request_validation_failed(resp)


def test_external_agent_heartbeat_rejects_non_scalar_project_id_with_422():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:heartbeat")

    with _client_for_principal(principal, logger_obj) as client:
        resp = client.post(
            "/api/external-agents/heartbeat",
            json={"worker_instance_id": "worker-1", "project_id": ["7"], "lease_token": "lease-1"},
            headers=_auth_headers(),
        )

    _assert_request_validation_failed(resp)


def test_external_agent_report_rejects_non_scalar_ids_with_422():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:report")

    with _client_for_principal(principal, logger_obj) as client:
        resp = client.post(
            "/api/external-agents/report",
            json={
                "worker_instance_id": "worker-1",
                "project_id": ["7"],
                "job_id": ["41"],
                "lease_token": "lease-1",
                "claim_token": "claim-1",
                "agent_name": "ext-worker",
                "step_id": "final_review",
                "skill": "review",
                "status": "pass",
            },
            headers=_auth_headers(),
        )

    _assert_request_validation_failed(resp)


def test_external_agent_double_claim_returns_409_while_first_lease_is_active():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:claim")

    with _client_for_principal(principal, logger_obj) as client:
        first = client.post(
            "/api/external-agents/claim",
            json={"worker_instance_id": "worker-1", "project_id": 7, "workflow_id": "wf_demo"},
            headers=_auth_headers(),
        )
        second = client.post(
            "/api/external-agents/claim",
            json={"worker_instance_id": "worker-2", "project_id": 7, "workflow_id": "wf_demo"},
            headers=_auth_headers(),
        )

    assert first.status_code == 200
    assert first.json()["claimed"] is True
    assert second.status_code == 409
    assert logger_obj.jobs[41]["worker_id"] == "worker-1"


def test_external_agent_stale_heartbeat_is_rejected_after_lease_expiry():
    logger_obj = _FakeExternalAgentLogger()
    # agent:claim needed to acquire the initial lease (FS-DEEP-CAND-005);
    # the post-expiry refresh is then rejected on lease state, not scope.
    principal = _principal_with_scopes("agent:heartbeat", "agent:claim")

    with _client_for_principal(principal, logger_obj) as client:
        first = client.post(
            "/api/external-agents/heartbeat",
            json={"worker_instance_id": "worker-1", "project_id": 7},
            headers=_auth_headers(),
        )
        logger_obj.advance_time(seconds=60)
        second = client.post(
            "/api/external-agents/heartbeat",
            json={
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "lease_token": first.json()["lease_token"],
            },
            headers=_auth_headers(),
        )

    assert first.status_code == 200
    assert second.status_code == 409


def test_external_agent_duplicate_register_is_non_mutating():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:register")

    with _client_for_principal(principal, logger_obj) as client:
        first = client.post(
            "/api/external-agents/register",
            json={
                "agent_name": "ext-worker",
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "workflow_id": "wf_demo",
            },
            headers=_auth_headers(),
        )
        second = client.post(
            "/api/external-agents/register",
            json={
                "agent_name": "ext-worker",
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "workflow_id": "wf_demo",
            },
            headers=_auth_headers(),
        )

    assert first.status_code == second.status_code == 200
    assert first.json()["worker_instance_id"] == second.json()["worker_instance_id"] == "worker-1"
    assert first.json()["registration_id"] != second.json()["registration_id"]
    assert logger_obj.lease is None
    assert logger_obj.jobs[41]["status"] == "pending"


def test_external_agent_report_is_rejected_after_lease_loss():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:claim", "agent:report")

    with _client_for_principal(principal, logger_obj) as client:
        claim = client.post(
            "/api/external-agents/claim",
            json={"worker_instance_id": "worker-1", "project_id": 7, "workflow_id": "wf_demo"},
            headers=_auth_headers(),
        )
        claim_body = claim.json()
        logger_obj.advance_time(seconds=60)
        report = client.post(
            "/api/external-agents/report",
            json={
                "worker_instance_id": "worker-1",
                "project_id": 7,
                "job_id": 41,
                "lease_token": claim_body["lease_token"],
                "claim_token": claim_body["job"]["claim_token"],
                "agent_name": "ext-worker",
                "step_id": "final_review",
                "skill": "review",
                "status": "pass",
                "summary": "Late report",
                "completed": True,
            },
            headers=_auth_headers(),
        )

    assert claim.status_code == 200
    assert report.status_code == 409
    assert logger_obj.jobs[41]["status"] == "in_progress"
    assert logger_obj.lease is None


def test_external_agent_replacement_worker_can_claim_after_stale_lease_expires():
    logger_obj = _FakeExternalAgentLogger()
    principal = _principal_with_scopes("agent:heartbeat", "agent:claim")

    with _client_for_principal(principal, logger_obj) as client:
        first = client.post(
            "/api/external-agents/heartbeat",
            json={"worker_instance_id": "worker-1", "project_id": 7},
            headers=_auth_headers(),
        )
        logger_obj.advance_time(seconds=60)
        second = client.post(
            "/api/external-agents/heartbeat",
            json={"worker_instance_id": "worker-2", "project_id": 7},
            headers=_auth_headers(),
        )
        claim = client.post(
            "/api/external-agents/claim",
            json={
                "worker_instance_id": "worker-2",
                "project_id": 7,
                "workflow_id": "wf_demo",
                "lease_token": second.json()["lease_token"],
                "agent_name": "ext-worker-2",
            },
            headers=_auth_headers(),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert claim.status_code == 200
    claim_body = claim.json()
    assert claim_body["claimed"] is True
    assert claim_body["job"]["job_id"] == "41"
    assert logger_obj.jobs[41]["worker_id"] == "worker-2"
    assert logger_obj.lease["worker_instance_id"] == "worker-2"
