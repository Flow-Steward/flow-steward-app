from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib import error, request

TransportFn = Callable[[str, dict[str, Any], dict[str, str]], tuple[int, dict[str, Any]]]


class ReferenceExternalAgentWorker:
    """Minimal reference client for the external agent ingress contract."""

    def __init__(
        self,
        *,
        base_url: str = "",
        bearer_token: str = "",
        transport: TransportFn | None = None,
    ) -> None:
        self._base_url = str(base_url or "").rstrip("/")
        self._bearer_token = str(bearer_token or "").strip()
        self._transport = transport or self._default_transport

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        return headers

    def _default_transport(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self._base_url}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req) as resp:  # nosec B310
                return int(resp.status), json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8") if hasattr(exc, "read") else ""
            try:
                parsed = json.loads(raw) if raw else {}
            except Exception:
                parsed = {"error": raw or str(exc)}
            return int(exc.code), parsed

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        status_code, body = self._transport(path, payload, self._headers())
        if status_code >= 400:
            raise RuntimeError(str(body.get("error") or f"{path} failed with status {status_code}"))
        return dict(body or {})

    def register(
        self,
        *,
        agent_name: str,
        worker_instance_id: str,
        project_id: str | None = None,
        workflow_id: str | None = None,
        skills: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/api/external-agents/register",
            {
                "agent_name": agent_name,
                "worker_instance_id": worker_instance_id,
                "project_id": project_id,
                "workflow_id": workflow_id,
                "skills": list(skills or []),
                "metadata": dict(metadata or {}),
            },
        )

    def heartbeat(
        self,
        *,
        worker_instance_id: str,
        project_id: str,
        lease_token: str = "",
        active_job_id: str | None = None,
        agent_name: str = "",
    ) -> dict[str, Any]:
        return self._post(
            "/api/external-agents/heartbeat",
            {
                "worker_instance_id": worker_instance_id,
                "project_id": project_id,
                "lease_token": lease_token,
                "active_job_id": active_job_id,
                "agent_name": agent_name,
            },
        )

    def claim(
        self,
        *,
        worker_instance_id: str,
        project_id: str,
        workflow_id: str | None = None,
        lease_token: str = "",
        agent_name: str = "",
    ) -> dict[str, Any]:
        return self._post(
            "/api/external-agents/claim",
            {
                "worker_instance_id": worker_instance_id,
                "project_id": project_id,
                "workflow_id": workflow_id,
                "lease_token": lease_token,
                "agent_name": agent_name,
            },
        )

    def report(
        self,
        *,
        worker_instance_id: str,
        project_id: str,
        job_id: str,
        lease_token: str,
        claim_token: str,
        agent_name: str,
        step_id: str,
        skill: str,
        status: str,
        step_execution_id: str | None = None,
        attempt: int | None = None,
        summary: str = "",
        artifacts: dict[str, Any] | None = None,
        errors: list[str] | None = None,
        correlation_id: str = "",
        completed: bool = True,
    ) -> dict[str, Any]:
        return self._post(
            "/api/external-agents/report",
            {
                "worker_instance_id": worker_instance_id,
                "project_id": project_id,
                "job_id": job_id,
                "lease_token": lease_token,
                "claim_token": claim_token,
                "agent_name": agent_name,
                "step_id": step_id,
                "step_execution_id": step_execution_id,
                "attempt": attempt,
                "skill": skill,
                "status": status,
                "summary": summary,
                "artifacts": dict(artifacts or {}),
                "errors": list(errors or []),
                "correlation_id": correlation_id,
                "completed": bool(completed),
            },
        )

    def run_once(
        self,
        *,
        project_id: str,
        worker_instance_id: str,
        agent_name: str = "reference_external_agent",
        workflow_id: str | None = None,
    ) -> dict[str, Any] | None:
        self.register(
            agent_name=agent_name,
            worker_instance_id=worker_instance_id,
            project_id=project_id,
            workflow_id=workflow_id,
            skills=["reference.echo"],
        )
        claim = self.claim(
            worker_instance_id=worker_instance_id,
            project_id=project_id,
            workflow_id=workflow_id,
            agent_name=agent_name,
        )
        if not bool(claim.get("claimed")):
            return None
        job = dict(claim.get("job") or {})
        lease_token = str(claim.get("lease_token") or "")
        self.heartbeat(
            worker_instance_id=worker_instance_id,
            project_id=project_id,
            lease_token=lease_token,
            active_job_id=str(job.get("job_id") or "").strip() or None,
            agent_name=agent_name,
        )
        return self.report(
            worker_instance_id=worker_instance_id,
            project_id=project_id,
            job_id=str(job.get("job_id") or "").strip(),
            lease_token=lease_token,
            claim_token=str(job.get("claim_token") or ""),
            agent_name=agent_name,
            step_id=str(job.get("step_id") or "external_execution"),
            step_execution_id=str(job.get("step_execution_id") or "").strip() or None,
            attempt=job.get("attempt") or None,
            skill=str(job.get("skill") or "reference.echo"),
            status="pass",
            summary=str(job.get("task_summary") or "External agent completed").strip(),
            artifacts={"echo": dict(job.get("input") or {})},
            completed=True,
        )
