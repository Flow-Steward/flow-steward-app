# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .command_services_common import *  # NOSONAR

# FS-DEEP-CAND-004: An `agent:claim` service account is a low-privilege remote
# worker credential. The claim response must expose only the minimal,
# non-sensitive project runtime fields a worker needs to execute a job — never
# the full project run configuration (agents_config, raw config blob, project
# rules, provider/connection metadata, assigned data connections, DB schema
# snapshots, or quality policies), which may carry secrets or sensitive
# deployment metadata.
_CLAIM_PROJECT_CONFIG_ALLOWLIST: tuple[str, ...] = (
    "project_id",
    "project_name",
)


def _minimize_claim_project_config(run_config: dict[str, Any]) -> dict[str, Any]:
    """Return only the allowlisted, non-sensitive subset of the project run config.

    FS-DEEP-CAND-004: prevents over-exposure of broad/sensitive project
    configuration to a narrow `agent:claim` service-account credential.
    """
    source = run_config if isinstance(run_config, dict) else {}
    return {key: source[key] for key in _CLAIM_PROJECT_CONFIG_ALLOWLIST if key in source}


class ExternalAgentCommandService:
    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        terminal_persistence_factory: Callable[[Any], Any] | None = None,
    ):
        self._get_logger = get_logger
        self._terminal_persistence_factory = terminal_persistence_factory

    def _terminal_persistence(self, bl: Any) -> Any:
        if self._terminal_persistence_factory is not None:
            return self._terminal_persistence_factory(bl)
        from core.application.jobs.terminal_persistence import TerminalPersistenceService

        return TerminalPersistenceService(bl)

    def _logger(self) -> Any:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        return bl

    def _project(self, bl: Any, project_id: str, scope: ScopeContext) -> dict[str, Any]:
        if not hasattr(bl, "get_project"):
            raise ServiceUnavailableError("Project storage is unavailable")
        project = bl.get_project(str(project_id))
        if not project:
            raise ServiceNotFoundError(ERR_PROJECT_NOT_FOUND)
        require_project_access(scope, project)
        return dict(project)

    @staticmethod
    def _job_meta(job: dict[str, Any]) -> dict[str, Any]:
        meta = job.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        return dict(meta or {}) if isinstance(meta, dict) else {}

    def register(
        self,
        command: ExternalAgentRegisterRequestV1,
        *,
        principal: ResolvedPrincipal | dict[str, Any] | None,
        scope: ScopeContext | None = None,
    ) -> ExternalAgentRegisterResponseV1:
        command = ExternalAgentRegisterRequestV1.model_validate(command)
        resolved_principal = _external_agent_principal(principal, required_scope="agent:register")
        resolved_scope = _authoritative_scope(
            scope,
            resolved_principal,
            project_id=command.project_id,
            workflow_id=command.workflow_id,
        )
        if command.project_id is not None:
            self._project(self._logger(), command.project_id, resolved_scope)
        if command.workflow_id:
            require_workflow_access(resolved_scope, command.workflow_id)

        worker_instance_id = (
            command.worker_instance_id or f"{command.agent_name}-{uuid.uuid4().hex[:12]}"
        )
        return ExternalAgentRegisterResponseV1(
            registration_id=f"reg_{uuid.uuid4().hex}",
            worker_instance_id=worker_instance_id,
            agent_name=command.agent_name,
            account_id=resolved_scope.account_id,
            principal_type=resolved_scope.principal_type or resolved_principal.principal_type,
            scopes=list(resolved_principal.scopes or []),
            allowed_project_ids=sorted(resolved_scope.allowed_project_ids),
            allowed_workflow_ids=sorted(resolved_scope.allowed_workflow_ids),
            project_id=command.project_id,
            workflow_id=command.workflow_id,
            server_time=_utc_now_iso(),
        )

    def heartbeat(
        self,
        command: ExternalAgentHeartbeatRequestV1,
        *,
        principal: ResolvedPrincipal | dict[str, Any] | None,
        scope: ScopeContext | None = None,
    ) -> ExternalAgentHeartbeatResponseV1:
        command = ExternalAgentHeartbeatRequestV1.model_validate(command)
        resolved_principal = _external_agent_principal(principal, required_scope="agent:heartbeat")
        resolved_scope = _authoritative_scope(
            scope, resolved_principal, project_id=command.project_id
        )
        bl = self._logger()
        self._project(bl, command.project_id, resolved_scope)
        if not hasattr(bl, "acquire_project_execution_lease") or not hasattr(
            bl, "heartbeat_project_execution_lease"
        ):
            raise ServiceUnavailableError(ERR_PROJECT_LEASE_UNAVAILABLE)

        if command.lease_token:
            lease = bl.heartbeat_project_execution_lease(
                project_id=command.project_id,
                worker_instance_id=command.worker_instance_id,
                lease_token=command.lease_token,
                drain_status="external_active" if command.active_job_id else "external_idle",
                active_job_id=command.active_job_id,
            )
        else:
            # FS-DEEP-CAND-005: Acquiring a *new* project execution lease (no
            # lease_token supplied) is a scheduling-capacity operation that
            # belongs to the claim flow. A heartbeat-only credential must not be
            # able to acquire/refresh a slot and starve legitimate workers under
            # the concurrency cap. Refreshing an existing lease (lease_token
            # present, handled above) remains a pure heartbeat operation.
            if not principal_has_required_scopes(
                resolved_principal.model_dump(), ["agent:claim"], require_all=True
            ):
                raise ServiceForbiddenError(
                    "Acquiring a project execution lease requires the 'agent:claim' scope"
                )
            lease = bl.acquire_project_execution_lease(
                project_id=command.project_id,
                worker_instance_id=command.worker_instance_id,
                ttl_seconds=45,
                drain_status="external_idle",
                active_job_id=command.active_job_id,
            )
        if not isinstance(lease, dict):
            raise ServiceConflictError("Project is currently leased by another worker")

        return ExternalAgentHeartbeatResponseV1(
            status="ok",
            worker_instance_id=command.worker_instance_id,
            project_id=command.project_id,
            lease_token=str(lease.get("lease_token") or ""),
            active_job_id=str(lease.get("active_job_id") or "").strip() or None,
            server_time=_utc_now_iso(),
        )

    def claim(
        self,
        command: ExternalAgentClaimRequestV1,
        *,
        principal: ResolvedPrincipal | dict[str, Any] | None,
        scope: ScopeContext | None = None,
    ) -> ExternalAgentClaimResponseV1:
        command = ExternalAgentClaimRequestV1.model_validate(command)
        resolved_principal = _external_agent_principal(principal, required_scope="agent:claim")
        resolved_scope = _authoritative_scope(
            scope,
            resolved_principal,
            project_id=command.project_id,
            workflow_id=command.workflow_id,
        )
        if command.workflow_id:
            require_workflow_access(resolved_scope, command.workflow_id)

        bl = self._logger()
        project = self._project(bl, command.project_id, resolved_scope)
        if not hasattr(bl, "acquire_project_execution_lease") or not hasattr(
            bl, "release_project_execution_lease"
        ):
            raise ServiceUnavailableError(ERR_PROJECT_LEASE_UNAVAILABLE)
        if not hasattr(bl, "claim_external_agent_job"):
            raise ServiceUnavailableError("External agent claim storage is unavailable")

        lease = bl.acquire_project_execution_lease(
            project_id=command.project_id,
            worker_instance_id=command.worker_instance_id,
            lease_token=command.lease_token or None,
            ttl_seconds=45,
            drain_status="external_claim",
            active_job_id=None,
        )
        if not isinstance(lease, dict):
            raise ServiceConflictError("Project is currently leased by another worker")

        claimed_job = bl.claim_external_agent_job(
            project_id=command.project_id,
            worker_instance_id=command.worker_instance_id,
            workflow_id=command.workflow_id,
            allowed_workflow_ids=sorted(resolved_scope.allowed_workflow_ids),
            claim_token=uuid.uuid4().hex,
            agent_name=command.agent_name or command.worker_instance_id,
        )
        if not isinstance(claimed_job, dict):
            bl.release_project_execution_lease(
                project_id=command.project_id,
                worker_instance_id=command.worker_instance_id,
                lease_token=str(lease.get("lease_token") or ""),
            )
            return ExternalAgentClaimResponseV1(
                claimed=False,
                worker_instance_id=command.worker_instance_id,
                project_id=command.project_id,
                lease_token="",
                job=None,
                server_time=_utc_now_iso(),
            )

        effective_workflow_id = (
            str(
                claimed_job.get("selected_workflow")
                or self._job_meta(claimed_job).get("external_agent", {}).get("workflow_id")
                or ""
            ).strip()
            or None
        )
        if effective_workflow_id:
            require_workflow_access(resolved_scope, effective_workflow_id)
        if hasattr(bl, "heartbeat_project_execution_lease"):
            bl.heartbeat_project_execution_lease(
                project_id=command.project_id,
                worker_instance_id=command.worker_instance_id,
                lease_token=str(lease.get("lease_token") or ""),
                drain_status="external_claimed",
                active_job_id=str(claimed_job.get("id") or "").strip() or None,
            )
        if hasattr(bl, "update_job_runtime"):
            bl.update_job_runtime(
                str(claimed_job.get("id") or "").strip(),
                selected_workflow=effective_workflow_id,
                current_agent=command.agent_name or command.worker_instance_id,
                heartbeat_now=True,
            )

        meta = self._job_meta(claimed_job)
        external_meta = (
            meta.get("external_agent") if isinstance(meta.get("external_agent"), dict) else {}
        )
        if not isinstance(external_meta, dict):
            external_meta = {}
        run_config = _minimize_claim_project_config(
            dict(bl.get_project_run_config(command.project_id) or {})
            if hasattr(bl, "get_project_run_config")
            else {}
        )
        claimed_payload = ExternalAgentClaimedJobV1(
            job_id=str(claimed_job.get("id") or "").strip(),
            claim_token=str(external_meta.get("claim_token") or ""),
            thread_id=str(claimed_job.get("thread_id") or "").strip() or None,
            task_id=str(claimed_job.get("task_id") or "").strip(),
            task_summary=str(claimed_job.get("task_summary") or "").strip(),
            task_source=str(claimed_job.get("task_source") or "").strip(),
            project_id=str(claimed_job.get("project_id") or command.project_id).strip(),
            project_name=str(claimed_job.get("project_name") or project.get("name") or "").strip(),
            workflow_id=effective_workflow_id,
            step_id=str(external_meta.get("step_id") or "").strip() or None,
            step_execution_id=str(external_meta.get("step_execution_id") or "").strip() or None,
            attempt=external_meta.get("attempt") or None,
            skill=str(external_meta.get("skill") or "").strip() or None,
            input={
                "project_config": run_config,
                "mode": claimed_job.get("mode"),
                "task_summary": str(claimed_job.get("task_summary") or "").strip(),
                "task_source": str(claimed_job.get("task_source") or "").strip(),
                "payload": dict(external_meta.get("input") or {})
                if isinstance(external_meta.get("input"), dict)
                else {},
            },
            job_meta=meta,
        )
        return ExternalAgentClaimResponseV1(
            claimed=True,
            worker_instance_id=command.worker_instance_id,
            project_id=command.project_id,
            lease_token=str(lease.get("lease_token") or ""),
            job=claimed_payload,
            server_time=_utc_now_iso(),
        )

    def report(
        self,
        command: ExternalAgentReportRequestV1,
        *,
        principal: ResolvedPrincipal | dict[str, Any] | None,
        scope: ScopeContext | None = None,
    ) -> ExternalAgentReportResponseV1:
        command = ExternalAgentReportRequestV1.model_validate(command)
        resolved_principal = _external_agent_principal(principal, required_scope="agent:report")
        resolved_scope = _authoritative_scope(
            scope, resolved_principal, project_id=command.project_id
        )
        bl = self._logger()
        project = self._project(bl, command.project_id, resolved_scope)
        if not hasattr(bl, "get_project_execution_lease"):
            raise ServiceUnavailableError(ERR_PROJECT_LEASE_UNAVAILABLE)
        lease = bl.get_project_execution_lease(
            command.project_id,
            active_only=True,
            worker_instance_id=command.worker_instance_id,
            lease_token=command.lease_token,
        )
        if not isinstance(lease, dict):
            raise ServiceConflictError("Project lease is not active")
        if (
            str(lease.get("worker_instance_id") or "").strip() != command.worker_instance_id
            or str(lease.get("lease_token") or "").strip() != command.lease_token
        ):
            raise ServiceConflictError("Project lease is not owned by this worker")
        if not hasattr(bl, "get_job"):
            raise ServiceUnavailableError("Job storage is unavailable")

        job = bl.get_job(command.job_id)
        if not isinstance(job, dict):
            raise ServiceNotFoundError(ERR_JOB_NOT_FOUND)
        if str(job.get("project_id") or "").strip() != command.project_id:
            raise ServiceForbiddenError("Access denied")

        require_project_access(
            resolved_scope,
            {
                "id": str(project.get("id") or command.project_id).strip(),
                "account_id": str(project.get("account_id") or resolved_scope.account_id).strip(),
            },
        )
        meta = self._job_meta(job)
        external_meta = (
            meta.get("external_agent") if isinstance(meta.get("external_agent"), dict) else {}
        )
        if not isinstance(external_meta, dict):
            external_meta = {}
        if str(external_meta.get("worker_instance_id") or "").strip() != command.worker_instance_id:
            raise ServiceConflictError("Job is not claimed by this worker")
        if str(external_meta.get("claim_token") or "").strip() != command.claim_token:
            raise ServiceConflictError("Claim token does not match")
        claimed_execution = str(external_meta.get("step_execution_id") or "").strip()
        if claimed_execution and (
            command.step_execution_id != claimed_execution
            or command.step_id != str(external_meta.get("step_id") or "").strip()
        ):
            # A report answers the exact execution it was handed, never another one.
            raise ServiceConflictError("Report does not match the claimed step execution")

        effective_workflow_id = (
            str(job.get("selected_workflow") or external_meta.get("workflow_id") or "").strip()
            or None
        )
        if effective_workflow_id:
            require_workflow_access(resolved_scope, effective_workflow_id)

        correlation_id = (
            command.correlation_id
            or str(external_meta.get("correlation_id") or "").strip()
            or uuid.uuid4().hex
        )
        published_at = _utc_now_iso()

        # SEC-008: Scan external agent ingress before persistence
        _ext_project_id = str(job.get("project_id") or command.project_id).strip()
        _safe_summary = command.summary
        _safe_artifacts = dict(command.artifacts or {})
        _safe_errors = list(command.errors or [])
        try:
            from core.infrastructure.security.integration import scan_inbound, scan_outbound

            _in = scan_inbound(
                _safe_summary or "",
                project_id=_ext_project_id,
                job_id=command.job_id,
            )
            if _in.redacted_text is not None:
                _safe_summary = _in.redacted_text
            import json as _json

            _art_str = _json.dumps(_safe_artifacts, default=str)
            _out = scan_outbound(
                _art_str,
                project_id=_ext_project_id,
                job_id=command.job_id,
            )
            if _out.redacted_text is not None and _out.redacted_text != _art_str:
                with contextlib.suppress(Exception):
                    _safe_artifacts = _json.loads(_out.redacted_text)
            _err_str = _json.dumps(_safe_errors, default=str)
            _err = scan_outbound(
                _err_str,
                project_id=_ext_project_id,
                job_id=command.job_id,
            )
            if _err.redacted_text is not None and _err.redacted_text != _err_str:
                try:
                    parsed_errors = _json.loads(_err.redacted_text)
                    if isinstance(parsed_errors, list):
                        _safe_errors = [str(item or "") for item in parsed_errors]
                except Exception:
                    pass
        except Exception:
            pass

        envelope = build_agent_message(
            from_agent=command.agent_name,
            to_agent="orchestrator",
            step_id=command.step_id,
            step_execution_id=command.step_execution_id,
            attempt=command.attempt,
            skill=command.skill,
            status=command.status,
            correlation_id=correlation_id,
            caused_by=[
                {"type": "job", "id": command.job_id},
                {"type": "external_agent", "id": command.worker_instance_id},
            ],
            artifacts=_safe_artifacts,
            summary=_safe_summary,
            errors=list(_safe_errors),
            started_at=str(external_meta.get("claimed_at") or "").strip() or None,
            completed_at=published_at,
            published_at=published_at,
        )
        message_published = bool(
            hasattr(bl, "log_agent_message") and bl.log_agent_message(command.job_id, envelope)
        )

        _action_payload = command.dump()
        _action_payload["summary"] = _safe_summary
        _action_payload["artifacts"] = _safe_artifacts
        _action_payload["errors"] = list(_safe_errors)
        if str(_action_payload.get("lease_token") or "").strip():
            _action_payload["lease_token"] = "[REDACTED]"
        if str(_action_payload.get("claim_token") or "").strip():
            _action_payload["claim_token"] = "[REDACTED]"

        external_meta.update(
            {
                "correlation_id": correlation_id,
                "last_reported_at": published_at,
                "last_status": command.status,
                "last_summary": _safe_summary,
            }
        )
        meta["external_agent"] = external_meta
        if hasattr(bl, "merge_job_meta"):
            bl.merge_job_meta(command.job_id, {"external_agent": external_meta})
        if hasattr(bl, "update_job_runtime"):
            bl.update_job_runtime(
                command.job_id,
                selected_workflow=effective_workflow_id,
                current_step_id=command.step_id,
                current_agent=command.agent_name,
                heartbeat_now=True,
                last_error=_safe_summary if command.status == "fail" and _safe_summary else None,
                last_response_excerpt=_safe_summary or None,
                clear_last_error=command.status != "fail",
            )
        if hasattr(bl, "log_action"):
            bl.log_action(
                project_id=str(job.get("project_id") or command.project_id).strip(),
                job_id=command.job_id,
                task_id=str(job.get("task_id") or "external-agent").strip() or "external-agent",
                task_summary=str(job.get("task_summary") or "External agent execution").strip()
                or "External agent execution",
                agent=command.agent_name,
                action_type="external_agent_report",
                status=command.status,
                payload=_action_payload,
                result={
                    "message_published": message_published,
                    "completed": bool(command.completed),
                },
                meta={
                    "account_id": resolved_scope.account_id,
                    "workflow_id": effective_workflow_id,
                    "worker_instance_id": command.worker_instance_id,
                    "correlation_id": correlation_id,
                },
            )

        job_status = str(job.get("status") or "").strip() or "in_progress"
        lease_released = False
        if command.completed:
            terminal_persistence = self._terminal_persistence(bl)
            if command.status == "pass":
                terminal_persistence.complete_success(
                    job_id=command.job_id,
                    workflow_outputs={},
                    final_report=_safe_summary or None,
                )
                job_status = "completed"
            elif command.status == "fail":
                error_text = _safe_summary or (
                    _safe_errors[0] if _safe_errors else "External agent reported failure"
                )
                terminal_persistence.complete_failure(
                    job_id=command.job_id,
                    status="failed",
                    error_message=error_text,
                    error_code="external_agent_failed",
                    category="execution",
                )
                job_status = "failed"
            else:
                if hasattr(bl, "update_job"):
                    bl.update_job(command.job_id, status="awaiting_approval")
                if hasattr(bl, "update_job_runtime"):
                    bl.update_job_runtime(
                        command.job_id,
                        current_step_id=command.step_id,
                        current_agent=command.agent_name,
                        heartbeat_now=True,
                        last_response_excerpt=_safe_summary or None,
                    )
                job_status = "awaiting_approval"

            if hasattr(bl, "release_project_execution_lease"):
                lease_released = bool(
                    bl.release_project_execution_lease(
                        project_id=command.project_id,
                        worker_instance_id=command.worker_instance_id,
                        lease_token=command.lease_token,
                    )
                )
        elif hasattr(bl, "heartbeat_project_execution_lease"):
            bl.heartbeat_project_execution_lease(
                project_id=command.project_id,
                worker_instance_id=command.worker_instance_id,
                lease_token=command.lease_token,
                drain_status="external_active",
                active_job_id=command.job_id,
            )

        return ExternalAgentReportResponseV1(
            job_id=command.job_id,
            status="accepted",
            job_status=job_status,
            correlation_id=correlation_id,
            message_published=message_published,
            lease_released=lease_released,
            server_time=published_at,
        )


__all__ = [name for name in globals() if not name.startswith("__")]
