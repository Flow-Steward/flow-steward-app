# ruff: noqa: F403, F405, RUF012
# mypy: ignore-errors
from __future__ import annotations

from core.application.orchestration.agent_message_projection import (
    public_replay_outcome,
)

from .command_services_common import *  # NOSONAR


def _cursor_execution_for_step(runtime_state: dict[str, Any], step_id: str | None) -> str:
    """The cursor's execution id, only when the cursor is on *step_id*."""
    cursor = runtime_state.get("run_cursor") if isinstance(runtime_state, dict) else None
    if not isinstance(cursor, dict) or not step_id:
        return ""
    if str(cursor.get("step_id") or "").strip() != step_id:
        return ""
    return str(cursor.get("step_execution_id") or "").strip()


def _job_group_for_step(job: dict[str, Any], step_id: Any) -> str | None:
    """The job's recorded group, kept only while the job is still on *step_id*."""
    step = str(step_id or "").strip()
    if step and str(job.get("current_step_id") or "").strip() == step:
        return str(job.get("current_group_id") or "").strip() or None
    return None


class JobCommandService:
    _TERMINAL_STATUSES: ClassVar[set[str]] = set(_JOB_DELETE_TERMINAL_STATUSES)

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        build_rerun_workflow_selection: Callable[..., tuple[dict[str, Any], dict[str, Any]]]
        | None = None,
        load_replay_snapshot: Callable[..., dict[str, Any]] | None = None,
        enqueue_task: Callable[..., int] | None = None,
        get_thread_state_values: Callable[[str], dict[str, Any]] | None = None,
        runtime_step_scope_id: Callable[[dict[str, Any]], str] | None = None,
        latest_runtime_message_id: Callable[..., int | None] | None = None,
        override_workflow_id_from_value: Callable[[Any], str] | None = None,
        is_empty_original_value: Callable[[Any], bool] | None = None,
        derive_override_original_value: Callable[..., Any] | None = None,
        validate_explicit_workflow_override: Callable[..., Any] | None = None,
        to_json_obj: Callable[[Any], Any] | None = None,
        serialize_decision_override_row: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        serialize_job_step_replay_row: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        sanitize_retryable_interrupted_step_snapshot: Callable[[Any], dict[str, Any] | None]
        | None = None,
        resolve_job_project_name: Callable[[Any, dict[str, Any]], str] | None = None,
        append_runtime_journal: Callable[..., Any] | None = None,
        enqueue_task_in_queue: Callable[..., Any] | None = None,
        resolve_replay_message_row: Callable[..., tuple[dict[str, Any] | None, str | None, int]]
        | None = None,
        collect_replay_step_trace: Callable[[Any, Any, dict[str, Any]], dict[str, Any]]
        | None = None,
        extract_replay_prompt_capture: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        derive_original_replay_outcome: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        invoke_step_reexecute_runtime: Callable[..., dict[str, Any]] | None = None,
        invoke_step_replay_llm: Callable[..., dict[str, Any]] | None = None,
        build_replay_diff_excerpt: Callable[[str, str], str] | None = None,
        retryable_interrupted_step_status: str = "retryable_interrupted_step",
        get_project_run_config: Callable[[str], dict[str, Any] | None] | None = None,
    ):
        self._get_logger = get_logger
        self._build_rerun_workflow_selection = build_rerun_workflow_selection
        self._load_replay_snapshot = load_replay_snapshot
        self._enqueue_task = enqueue_task
        self._get_thread_state_values = get_thread_state_values or (lambda thread_id: {})
        self._runtime_step_scope_id = runtime_step_scope_id or (lambda runtime_state: "")
        self._latest_runtime_message_id = latest_runtime_message_id or (
            lambda runtime_state, step_execution_id="": None
        )
        self._override_workflow_id_from_value = override_workflow_id_from_value or (
            lambda value: str(value or "").strip()
        )
        self._is_empty_original_value = is_empty_original_value or (
            lambda value: value in (None, "", {})
        )
        self._derive_override_original_value = derive_override_original_value
        self._validate_explicit_workflow_override = validate_explicit_workflow_override
        self._to_json_obj = to_json_obj or (lambda value: value)
        self._serialize_decision_override_row = serialize_decision_override_row or (
            lambda row: dict(row or {})
        )
        self._serialize_job_step_replay_row = serialize_job_step_replay_row or (
            lambda row: dict(row or {})
        )
        self._sanitize_retryable_interrupted_step_snapshot = (
            sanitize_retryable_interrupted_step_snapshot
            or (lambda value: value if isinstance(value, dict) else None)
        )
        self._resolve_job_project_name = resolve_job_project_name or (
            lambda bl, job: str((job or {}).get("project_name") or "").strip()
        )
        self._append_runtime_journal = append_runtime_journal
        self._enqueue_task_in_queue = enqueue_task_in_queue
        self._resolve_replay_message_row = resolve_replay_message_row
        self._collect_replay_step_trace = collect_replay_step_trace
        self._extract_replay_prompt_capture = extract_replay_prompt_capture or (
            lambda trace_payload: {}
        )
        self._derive_original_replay_outcome = derive_original_replay_outcome or (
            lambda trace_payload: {}
        )
        self._invoke_step_reexecute_runtime = invoke_step_reexecute_runtime
        self._invoke_step_replay_llm = invoke_step_replay_llm
        self._build_replay_diff_excerpt = build_replay_diff_excerpt or (
            lambda original_text, replayed_text: ""
        )
        self._retryable_interrupted_step_status = str(
            retryable_interrupted_step_status or "retryable_interrupted_step"
        )
        self._get_project_run_config = get_project_run_config

    def _logger(self) -> Any:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        return bl

    def _verified_replay_snapshot(
        self,
        job: dict[str, Any],
        *,
        project_id: str,
    ) -> dict[str, Any]:
        from core.application.jobs.job_invocation_artifacts import JobReplaySnapshotError

        meta = self._to_json_obj(job.get("meta"))
        if not isinstance(meta, dict):
            meta = {}
        pointer = meta.get("replay_snapshot")
        if not isinstance(pointer, dict) or not pointer:
            exc = JobReplaySnapshotError("job.replay_snapshot_absent")
            raise ServiceValidationError(
                exc.code,
                payload={"error": exc.code, "code": exc.code},
            ) from exc
        if self._load_replay_snapshot is None:
            raise ServiceUnavailableError("Job replay snapshot loader is not available")
        try:
            return self._load_replay_snapshot(
                pointer=pointer,
                project_id=project_id,
                job_id=str(job.get("id") or ""),
            )
        except JobReplaySnapshotError as exc:
            raise ServiceValidationError(
                exc.code,
                payload={"error": exc.code, "code": exc.code},
            ) from exc

    def _project_run_config(self, bl: Any, project_id: str) -> dict[str, Any]:
        loader = self._get_project_run_config
        if loader is None:
            loader = getattr(bl, "get_project_run_config", None)
        if not callable(loader):
            raise ServiceUnavailableError("Project run configuration is not available")
        loaded = loader(str(project_id))
        return dict(loaded) if isinstance(loaded, dict) else {}

    def load_project_run_config(self, project_id: str) -> dict[str, Any]:
        """Load project execution config through the composed project-store port."""
        loader = self._get_project_run_config
        if not callable(loader):
            raise ServiceUnavailableError("Project run configuration is not available")
        loaded = loader(str(project_id))
        return dict(loaded) if isinstance(loaded, dict) else {}

    def _require_job(self, bl: Any, job_id: str) -> dict[str, Any]:
        job = bl.get_job(job_id)
        if not job:
            raise ServiceNotFoundError(ERR_JOB_NOT_FOUND)
        return job

    def _require_job_in_scope(self, bl: Any, job_id: str, scope: ScopeContext) -> dict[str, Any]:
        job = self._require_job(bl, job_id)
        project_id = str(job.get("project_id") or "").strip()
        if not project_id:
            require_account_access(scope, job)
            return job
        project = bl.get_project(project_id)
        if not project:
            # The project row is gone while the job row survives — a job whose
            # queue payload references a deleted project is exactly the state an
            # operator has to clear. Refusing here made every documented recovery
            # (acknowledge, restart, cancel) answer 404, so readiness stayed red
            # with no product-level way out. Authorize against the job's own
            # account instead, the same rule already applied to jobs that never
            # had a project; a caller from another account is still refused.
            require_account_access(scope, job)
            return job
        require_project_access(scope, project)
        job_account_id = str(job.get("account_id") or "").strip()
        project_account_id = str(project.get("account_id") or "").strip()
        if job_account_id and project_account_id and job_account_id != project_account_id:
            raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
        return job

    def _require_project(self, bl: Any, project_id: str) -> dict[str, Any]:
        project = bl.get_project(project_id)
        if not project:
            raise ServiceNotFoundError(ERR_PROJECT_NOT_FOUND)
        return project

    @staticmethod
    def _normalize_optional_int(value: Any) -> int | None:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _normalize_optional_id(value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized if normalized and normalized != "0" else None

    def _effective_runtime_state(self, thread_id: str) -> dict[str, Any]:
        runtime_state = self._get_thread_state_values(str(thread_id or "").strip())
        return runtime_state if isinstance(runtime_state, dict) else {}

    def _replay_message_row(
        self,
        bl: Any,
        *,
        job_id: str,
        message_id: str = "",
        correlation_id: str = "",
    ) -> dict[str, Any]:
        if self._resolve_replay_message_row is None:
            raise ServiceUnavailableError("Replay resolution is not available")
        row, error, status_code = self._resolve_replay_message_row(
            bl,
            job_id=job_id,
            message_id=message_id,
            correlation_id=correlation_id,
        )
        if error:
            error_text = str(error)
            if int(status_code or 500) == 404:
                raise ServiceNotFoundError(error_text)
            raise ServiceValidationError(error_text)
        if not row:
            raise ServiceNotFoundError("Message not found")
        return dict(row)

    @staticmethod
    def _normalized_positive_ids(values: list[str]) -> list[str]:
        normalized: set[str] = set()
        for value in values or []:
            identity = str(value or "").strip()
            if not identity:
                continue
            normalized.add(identity)
        return sorted(normalized)

    def acknowledge_job(
        self,
        job_id: str,
        command: JobAcknowledgeInput,
        *,
        scope: ScopeContext | None = None,
    ) -> JobLifecycleResultView:
        bl = self._logger()
        resolved = _service_scope(scope)
        self._require_job_in_scope(bl, str(job_id), resolved) if hasattr(bl, "get_job") else None
        actor = str(command.actor or "system").strip() or "system"
        try:
            ok = bl.acknowledge_job(str(job_id), actor)
        except Exception as exc:
            logger.warning("acknowledge_job(%s) failed: %s", job_id, exc)
            raise ServiceError("Failed to acknowledge job") from exc
        if not ok:
            raise ServiceNotFoundError("Job not found or already acknowledged")
        return JobLifecycleResultView(job_id=str(job_id), status="acknowledged")

    def unacknowledge_job(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> JobLifecycleResultView:
        bl = self._logger()
        resolved = _service_scope(scope)
        self._require_job_in_scope(bl, str(job_id), resolved) if hasattr(bl, "get_job") else None
        try:
            ok = bl.unacknowledge_job(str(job_id))
        except Exception as exc:
            logger.warning("unacknowledge_job(%s) failed: %s", job_id, exc)
            raise ServiceError("Failed to unacknowledge job") from exc
        if not ok:
            raise ServiceNotFoundError("Job not found or not acknowledged")
        return JobLifecycleResultView(job_id=str(job_id), status="unacknowledged")

    def cancel_job(
        self,
        job_id: str,
        *,
        actor: str = "operator",
        comment: str = CANCELLED_BY_USER_VALUE,
        scope: ScopeContext | None = None,
    ) -> JobLifecycleResultView:
        del actor
        bl = self._logger()
        resolved = _service_scope(scope, project_id=None)
        job = self._require_job_in_scope(bl, str(job_id), resolved)
        job_status = str(job.get("status") or "").strip().lower()
        if job_status in self._TERMINAL_STATUSES:
            raise ServiceConflictError(f"Job is already {job.get('status')}")

        thread_id = str(job.get("thread_id") or "").strip() or None
        project_id = str(job.get("project_id") or "").strip() or None
        try:
            from core.application.jobs.terminal_persistence import (
                TerminalPersistenceConflict,
                TerminalPersistenceService,
            )

            TerminalPersistenceService(bl).complete_failure(
                job_id=str(job_id),
                status="cancelled",
                error_message=str(comment or CANCELLED_BY_USER_VALUE),
                error_code="cancelled",
                category="cancellation",
                final_report=str(comment or CANCELLED_BY_USER_VALUE),
            )
        except TerminalPersistenceConflict as exc:
            raise ServiceConflictError("Job already reached a terminal state") from exc
        except Exception as exc:
            logger.warning("cancel_job(%s) failed: %s", job_id, exc)
            raise ServiceError("Failed to cancel job") from exc

        try:
            from core.infrastructure.runtime.subprocess_lifecycle import (
                terminalize_running_worker_subprocesses,
            )

            terminalize_running_worker_subprocesses(
                bl,
                job_id=str(job_id),
                status="cancelled",
                reason=str(comment or CANCELLED_BY_USER_VALUE),
            )
        except Exception as exc:
            logger.debug("cancel_job(%s) subprocess terminalization failed: %s", job_id, exc)
        try:
            update_runtime = getattr(bl, "update_job_runtime", None)
            if callable(update_runtime):
                update_runtime(
                    str(job_id),
                    current_step_id="",
                    current_agent="",
                    heartbeat_now=True,
                    last_error=str(comment or CANCELLED_BY_USER_VALUE)[:16000],
                    last_response_excerpt="",
                )
        except Exception as exc:
            logger.debug("cancel_job(%s) runtime reconciliation failed: %s", job_id, exc)

        return JobLifecycleResultView(
            job_id=str(job_id),
            status="cancelled",
            project_id=project_id,
            thread_id=thread_id,
            cancelled=True,
        )

    def restart_job(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> JobLifecycleResultView:
        bl = self._logger()
        resolved = _service_scope(scope, project_id=None)
        job = self._require_job_in_scope(bl, str(job_id), resolved)
        if self._build_rerun_workflow_selection is None or self._enqueue_task is None:
            raise ServiceUnavailableError("Job restart is not available")

        project_id = str(job.get("project_id") or "").strip()
        if not project_id:
            raise ServiceValidationError("Job has no project_id")

        run_cfg = self._project_run_config(bl, project_id)
        project_name = str((run_cfg or {}).get("project_name") or "").strip()
        replay_snapshot = self._verified_replay_snapshot(job, project_id=project_id)
        rerun_extra_state, rerun_meta = self._build_rerun_workflow_selection(
            job,
            replay_snapshot=replay_snapshot,
        )

        try:
            new_job_id = self._enqueue_task(
                task_id=job["task_id"],
                task_summary=job["task_summary"],
                task_source=job.get("task_source", "manual"),
                project_id=project_id,
                project_name=project_name,
                project_config=run_cfg,
                mode=job.get("mode"),
                extra_state=rerun_extra_state or None,
                meta=rerun_meta or None,
                test_mode=bool(replay_snapshot.get("test_mode")),
                replay_snapshot=replay_snapshot,
            )
        except Exception as exc:
            logger.warning("restart_job(%s) failed: %s", job_id, exc)
            raise ServiceError("Failed to restart job") from exc

        return JobLifecycleResultView(
            job_id=str(job_id),
            status="pending",
            project_id=project_id,
            new_job_id=str(new_job_id),
            enqueued=True,
        )

    def delete_job(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> JobDeleteResultView:
        """Soft-delete (trash) a job.  Logs and observability data are preserved."""
        bl = self._logger()
        resolved = _service_scope(scope, project_id=None)
        job = self._require_job_in_scope(bl, str(job_id), resolved)
        conflict_message = _job_delete_conflict_message(
            str(job_id),
            job,
            allow_quiescing=True,
        )
        if conflict_message:
            raise ServiceConflictError(conflict_message)
        try:
            with bl.conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET deleted_at = NOW(), deleted_by = %s WHERE id = %s AND deleted_at IS NULL",
                    (str(resolved.actor_id or "system"), str(job_id)),
                )
                if not cur.rowcount:
                    raise ServiceNotFoundError("Job not found or already trashed")
        except (ServiceNotFoundError, ServiceConflictError):
            raise
        except Exception as exc:
            logger.exception("delete_job (trash) failed for job_id=%s", job_id)
            raise ServiceError("Failed to trash job") from exc
        return JobDeleteResultView(job_id=str(job_id))

    def bulk_delete_project_jobs(
        self,
        command: ProjectJobsBulkDeleteInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectJobsBulkDeleteResultView:
        """Soft-delete (trash) selected jobs.  Logs and observability data are preserved."""
        bl = self._logger()
        resolved = _service_scope(scope, project_id=command.project_id)
        project = self._require_project(bl, str(command.project_id))
        require_project_access(resolved, project)
        if str(command.confirm or "").strip() != "DELETE":
            raise ServiceValidationError("Confirmation must be exactly DELETE")

        ids = self._normalized_positive_ids(command.ids)
        if not ids:
            raise ServiceValidationError("No job ids provided")

        deleted: list[str] = []
        skipped: list[dict[str, Any]] = []
        for job_id in ids:
            job = bl.get_job(job_id)
            if not job:
                skipped.append({"job_id": job_id, "reason": "not_found"})
                continue
            if str(job.get("project_id") or "").strip() != str(command.project_id):
                skipped.append({"job_id": job_id, "reason": "wrong_project"})
                continue
            blocker = _job_delete_blocker(job, allow_quiescing=True)
            if blocker is not None:
                blocker_type, status_label = blocker
                skipped.append({"job_id": job_id, "reason": f"{blocker_type} ({status_label})"})
                continue
            try:
                with bl.conn.cursor() as cur:
                    cur.execute(
                        "UPDATE jobs SET deleted_at = NOW(), deleted_by = %s WHERE id = %s AND deleted_at IS NULL",
                        (str(resolved.actor_id or "system"), str(job_id)),
                    )
                    if cur.rowcount:
                        deleted.append(job_id)
                    else:
                        skipped.append({"job_id": job_id, "reason": "already_trashed"})
            except Exception as exc:
                logger.warning(
                    "bulk_delete_project_jobs (trash) failed for job %s: %s", job_id, exc
                )
                skipped.append({"job_id": job_id, "reason": "trash_failed"})

        return ProjectJobsBulkDeleteResultView(
            requested=ids,
            deleted=deleted,
            deleted_count=len(deleted),
            skipped=skipped,
        )

    def create_decision_override(
        self,
        job_id: str,
        command: JobDecisionOverrideInput,
        *,
        scope: ScopeContext | None = None,
    ) -> JobDecisionOverrideResultView:
        bl = self._logger()
        resolved = _service_scope(scope)
        job = self._require_job_in_scope(bl, str(job_id), resolved)
        if not hasattr(bl, "create_job_decision_override"):
            raise ServiceUnavailableError("Decision override storage is unavailable")

        normalized_domain = str(command.domain or "").strip().lower()
        actor_username = str(command.created_by or "operator").strip() or "operator"
        runtime_state = self._effective_runtime_state(str(job.get("thread_id") or ""))

        effective_workflow_id = (
            str(
                command.workflow_id
                or runtime_state.get("workflow_id")
                or job.get("selected_workflow")
                or job.get("workflow_id")
                or ""
            ).strip()
            or None
        )
        effective_step_id = (
            str(command.step_id or self._runtime_step_scope_id(runtime_state) or "").strip() or None
        )
        effective_step_execution_id = (
            str(
                command.step_execution_id
                or _cursor_execution_for_step(runtime_state, effective_step_id)
                or ""
            ).strip()
            or None
        )
        effective_message_id = command.message_id or self._latest_runtime_message_id(
            runtime_state,
            step_execution_id=str(effective_step_execution_id or ""),
        )
        effective_message_id = self._normalize_optional_id(effective_message_id)

        if normalized_domain != "gate_outcome":
            raise ServiceValidationError(
                f"{normalized_domain} decision overrides are no longer supported"
            )

        effective_original_value = command.original_value
        if self._is_empty_original_value(effective_original_value):
            if self._derive_override_original_value is None:
                raise ServiceUnavailableError("Decision override derivation is not available")
            effective_original_value = self._derive_override_original_value(
                job=job,
                domain=normalized_domain,
                step_execution_id=str(effective_step_execution_id or ""),
                workflow_id=str(effective_workflow_id or ""),
                step_id=str(effective_step_id or ""),
                bl=bl,
            )

        try:
            row = bl.create_job_decision_override(
                job_id=str(job_id),
                domain=str(command.domain or ""),
                override_value=command.override_value,
                original_value=effective_original_value,
                reason=str(command.reason or ""),
                created_by=actor_username,
                active=bool(command.active),
                message_id=effective_message_id,
                workflow_id=effective_workflow_id,
                step_id=effective_step_id,
                step_execution_id=effective_step_execution_id,
            )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
        except Exception as exc:
            logger.warning("create_decision_override(%s) failed: %s", job_id, exc)
            raise ServiceError("Failed to create decision override") from exc

        if row is None:
            raise ServiceUnavailableError("Decision override storage is unavailable")

        serialized_override = self._serialize_decision_override_row(dict(row))

        try:
            if hasattr(bl, "log_event"):
                bl.log_event(
                    job_id=str(job_id),
                    phase=str(effective_step_id or effective_workflow_id or "orchestrator"),
                    agent=actor_username,
                    event_type="decision_override",
                    title=f"Operator override created ({command.domain})",
                    detail={
                        "override_id": row.get("id"),
                        "domain": command.domain,
                        "message_id": effective_message_id,
                        "workflow_id": effective_workflow_id,
                        "step_id": effective_step_id,
                        "step_execution_id": effective_step_execution_id,
                        "reason": command.reason,
                        "created_by": actor_username,
                        "active": bool(command.active),
                        "original_value": serialized_override.get("original_value"),
                        "override_value": serialized_override.get("override_value"),
                    },
                    severity="warn",
                )
        except Exception:
            pass

        return JobDecisionOverrideResultView(job_id=str(job_id), override=serialized_override)

    def execute_step_replay(
        self,
        job_id: str,
        command: JobStepReplayExecutionInput,
        *,
        scope: ScopeContext | None = None,
    ) -> JobStepReplayExecutionResultView:
        bl = self._logger()
        resolved = _service_scope(scope, project_id=None)
        job = self._require_job_in_scope(bl, str(job_id), resolved)
        message_row = self._replay_message_row(
            bl,
            job_id=str(job_id),
            message_id=str(command.message_id or "").strip(),
            correlation_id=str(command.correlation_id or "").strip(),
        )

        if self._collect_replay_step_trace is None:
            raise ServiceUnavailableError("Replay trace collection is not available")

        replay_trace = self._collect_replay_step_trace(bl, str(job_id), dict(message_row))
        capture = self._extract_replay_prompt_capture(replay_trace)
        replay_mode = str(command.replay_mode or "step_reexecute").strip().lower()
        if replay_mode not in {"step_reexecute", "prompt_reinvoke"}:
            raise ServiceValidationError(f"Unsupported replay_mode '{command.replay_mode}'")

        system_prompt = str(capture.get("system_prompt") or "")
        user_prompt = str(capture.get("user_prompt") or "")
        message = replay_trace.get("message")
        message = message if isinstance(message, dict) else {}
        original_outcome = self._derive_original_replay_outcome(replay_trace)
        meta = self._to_json_obj(job.get("meta"))
        if not isinstance(meta, dict):
            meta = {}
        policy_snapshot = meta.get("project_policy_snapshot")
        if not isinstance(policy_snapshot, dict):
            policy_snapshot = {}
        project_config_snapshot = meta.get("project_config_snapshot")
        if not isinstance(project_config_snapshot, dict):
            project_config_snapshot = {}
        prompt_version = str(capture.get("prompt_version") or "").strip()
        requested_by = str(command.requested_by or "operator").strip() or "operator"

        if replay_mode == "prompt_reinvoke" and (
            not system_prompt.strip() or not user_prompt.strip()
        ):
            failed_row = None
            if hasattr(bl, "create_job_step_replay"):
                try:
                    failed_row = bl.create_job_step_replay(
                        str(job_id),
                        message_id=self._normalize_optional_id(message.get("id")),
                        correlation_id=str(message.get("correlation_id") or "").strip() or None,
                        requested_by=requested_by,
                        replay_mode=replay_mode,
                        status="failed",
                        prompt_version=prompt_version or None,
                        policy_snapshot=policy_snapshot,
                        captured_input={
                            "replay_mode": replay_mode,
                            "system_prompt": system_prompt,
                            "user_prompt": user_prompt,
                            "prompt_version": prompt_version or None,
                            "agent_id": message.get("from_agent"),
                            "provider": capture.get("provider"),
                            "model_used": capture.get("model_used"),
                            "skill": capture.get("skill"),
                            "step_id": capture.get("step_id") or message.get("step_id"),
                            "step_execution_id": message.get("step_execution_id"),
                        },
                        original_outcome=original_outcome,
                    )
                except Exception:
                    failed_row = None
            raise ServiceValidationError(
                "Captured prompt_request payload is missing system/user prompt text",
                payload={
                    "error": "Captured prompt_request payload is missing system/user prompt text",
                    "replay": self._serialize_job_step_replay_row(failed_row)
                    if isinstance(failed_row, dict)
                    else None,
                },
            )

        replay_row = None
        replay_id = ""
        captured_input = {
            "replay_mode": replay_mode,
            "agent_id": message.get("from_agent"),
            "skill": capture.get("skill") or message.get("skill"),
            "step_id": capture.get("step_id") or message.get("step_id"),
            "step_execution_id": message.get("step_execution_id"),
            "prompt_version": prompt_version or None,
            "provider": capture.get("provider"),
            "model_used": capture.get("model_used"),
        }
        if replay_mode == "prompt_reinvoke":
            captured_input["system_prompt"] = system_prompt
            captured_input["user_prompt"] = user_prompt

        if hasattr(bl, "create_job_step_replay"):
            try:
                replay_row = bl.create_job_step_replay(
                    str(job_id),
                    message_id=self._normalize_optional_id(message.get("id")),
                    correlation_id=str(message.get("correlation_id") or "").strip() or None,
                    requested_by=requested_by,
                    replay_mode=replay_mode,
                    status="running",
                    prompt_version=prompt_version or None,
                    policy_snapshot=policy_snapshot,
                    captured_input=captured_input,
                    original_outcome=original_outcome,
                )
                replay_id = str((replay_row or {}).get("id") or "").strip()
            except Exception as exc:
                logger.warning("execute_step_replay(%s): create replay row failed: %s", job_id, exc)

        try:
            project_id = str(job.get("project_id") or "").strip()
            project_config = dict(project_config_snapshot)
            if not project_config and project_id:
                project_config = self._project_run_config(bl, project_id)
            if replay_mode == "step_reexecute":
                if self._invoke_step_reexecute_runtime is None:
                    raise ServiceUnavailableError("Step replay execution is not available")
                replay_outcome = self._invoke_step_reexecute_runtime(
                    bl=bl,
                    job=job,
                    message=message,
                    trace_payload=replay_trace,
                    project_config=project_config if isinstance(project_config, dict) else {},
                    policy_snapshot=policy_snapshot,
                    prompt_version=prompt_version,
                    provider=str(capture.get("provider") or "").strip(),
                    model=str(capture.get("model_used") or "").strip(),
                )
            else:
                if self._invoke_step_replay_llm is None:
                    raise ServiceUnavailableError("Prompt replay execution is not available")
                replay_outcome = self._invoke_step_replay_llm(
                    project_config=project_config if isinstance(project_config, dict) else {},
                    agent_name=str(message.get("from_agent") or "").strip(),
                    provider=str(capture.get("provider") or "").strip(),
                    model=str(capture.get("model_used") or "").strip(),
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    prompt_version=prompt_version,
                    policy_snapshot=policy_snapshot,
                )
            diff_excerpt = self._build_replay_diff_excerpt(
                str(original_outcome.get("response_text") or ""),
                str(replay_outcome.get("response_text") or ""),
            )
            updated_row = replay_row
            if replay_id and hasattr(bl, "update_job_step_replay"):
                updated_row = bl.update_job_step_replay(
                    replay_id,
                    status="succeeded",
                    replay_outcome=replay_outcome,
                    original_outcome=original_outcome,
                    diff_excerpt=diff_excerpt,
                    error_message=None,
                )
            return JobStepReplayExecutionResultView(
                job_id=str(job_id),
                message_id=str(message.get("id") or "").strip(),
                replay_id=replay_id or None,
                original_outcome=public_replay_outcome(original_outcome),
                replay_outcome=public_replay_outcome(replay_outcome),
                diff_excerpt=diff_excerpt,
                replay=self._serialize_job_step_replay_row(updated_row)
                if isinstance(updated_row, dict)
                else None,
            )
        except Exception as exc:
            error_text = str(exc)
            updated_row = replay_row
            if replay_id and hasattr(bl, "update_job_step_replay"):
                with contextlib.suppress(Exception):
                    updated_row = bl.update_job_step_replay(
                        replay_id,
                        status="failed",
                        replay_outcome={},
                        original_outcome=original_outcome,
                        diff_excerpt=None,
                        error_message=error_text,
                    )
            status_code = 400 if isinstance(exc, ValueError) else getattr(exc, "status_code", 500)
            raise ServiceError(
                f"Replay execution failed: {error_text}",
                status_code=int(status_code or 500),
                payload={
                    "error": f"Replay execution failed: {error_text}",
                    "job_id": str(job_id),
                    "message_id": str(message.get("id") or "").strip(),
                    "replay_id": replay_id or None,
                    "replay": self._serialize_job_step_replay_row(updated_row)
                    if isinstance(updated_row, dict)
                    else None,
                },
            ) from exc

    def retry_interrupted_step(
        self,
        job_id: str,
        command: JobRetryInterruptedStepInput,
        *,
        scope: ScopeContext | None = None,
    ) -> JobLifecycleResultView:
        bl = self._logger()
        resolved = _service_scope(scope, project_id=None)
        job = self._require_job_in_scope(bl, str(job_id), resolved)

        meta = self._to_json_obj(job.get("meta"))
        if not isinstance(meta, dict):
            meta = {}
        snapshot = self._sanitize_retryable_interrupted_step_snapshot(
            self._to_json_obj(meta.get("retryable_interrupted_step"))
        )
        if not snapshot:
            raise ServiceConflictError(
                "No retryable interrupted-step snapshot is available for this job"
            )

        if str(job.get("status") or "").strip().lower() not in {
            self._retryable_interrupted_step_status,
            "orphaned",
            "stuck",
            "failed",
        }:
            raise ServiceConflictError(
                f"Job status '{job.get('status')}' is not eligible for interrupted-step retry"
            )

        project_id = str(job.get("project_id") or "").strip()
        if not project_id:
            raise ServiceValidationError("Job has no project_id")

        thread_id = str(job.get("thread_id") or "").strip()
        if not thread_id:
            raise ServiceConflictError("Job has no thread_id checkpoint key")

        run_cfg = self._project_run_config(bl, project_id)
        project_config = dict(run_cfg) if isinstance(run_cfg, dict) else {}
        project_name = str(
            project_config.get("project_name") or self._resolve_job_project_name(bl, job) or ""
        ).strip()
        if not project_name:
            project_name = str(job.get("project_name") or f"project-{project_id}").strip()

        if self._build_rerun_workflow_selection is None or self._enqueue_task_in_queue is None:
            raise ServiceUnavailableError("Interrupted-step retry is not available")

        replay_snapshot = self._verified_replay_snapshot(job, project_id=project_id)
        extra_state, _ = self._build_rerun_workflow_selection(
            job,
            replay_snapshot=replay_snapshot,
        )
        if not isinstance(extra_state, dict):
            extra_state = {}
        extra_state["retry_interrupted_step"] = True
        extra_state["retry_interrupted_agent"] = str(snapshot.get("agent_id") or "")
        extra_state["retry_interrupted_skill"] = str(snapshot.get("skill") or "")
        extra_state["retry_interrupted_step_id"] = str(snapshot.get("step_id") or "")
        extra_state["retry_interrupted_step_execution_id"] = str(
            snapshot.get("step_execution_id") or ""
        )

        queue_name = f"platform.project.{project_id}"
        original_meta = dict(meta)
        updated_meta = dict(meta)
        updated_meta.pop("retryable_interrupted_step", None)
        updated_meta["last_retry_step_request"] = {
            "requested_at": datetime.now(UTC).isoformat(),
            "requested_by": str(command.requested_by or "operator").strip() or "operator",
            "agent_id": snapshot.get("agent_id"),
            "skill": snapshot.get("skill"),
            "step_id": snapshot.get("step_id"),
            "step_execution_id": snapshot.get("step_execution_id"),
        }

        try:
            bl.update_job(
                str(job_id),
                status="pending",
                queue_name=queue_name,
                queued_at="NOW()",
                worker_id="",
                meta=updated_meta,
            )
            if hasattr(bl, "update_job_runtime"):
                bl.update_job_runtime(
                    str(job_id),
                    current_step_id=str(snapshot.get("step_id") or ""),
                    current_group_id=_job_group_for_step(job, snapshot.get("step_id")),
                    current_agent=str(snapshot.get("agent_id") or ""),
                    heartbeat_now=True,
                    clear_last_error=True,
                    last_error=None,
                )

            self._enqueue_task_in_queue(
                project_id=project_id,
                job_id=str(job_id),
                task_id=str(job.get("task_id") or f"retry-step-{job_id}"),
                task_summary=str(job.get("task_summary") or "Retry interrupted step"),
                task_source=str(job.get("task_source") or "manual"),
                project_name=project_name,
                project_config=project_config,
                thread_id=thread_id,
                mode=job.get("mode"),
                extra_state=extra_state,
            )
        except Exception as exc:
            try:
                rollback_meta = dict(original_meta)
                rollback_meta["retryable_interrupted_step"] = snapshot
                bl.update_job(
                    str(job_id),
                    status=self._retryable_interrupted_step_status,
                    worker_id="",
                    meta=rollback_meta,
                )
                if hasattr(bl, "update_job_runtime"):
                    bl.update_job_runtime(
                        str(job_id),
                        heartbeat_now=True,
                        last_error=f"Retry step enqueue failed: {str(exc)[:3000]}",
                    )
            except Exception:
                pass
            raise ServiceError(
                f"Retry step enqueue failed: {exc}",
                payload={
                    "error": f"Retry step enqueue failed: {exc}",
                    "job_id": str(job_id),
                    "status": self._retryable_interrupted_step_status,
                },
            ) from exc

        if self._append_runtime_journal is not None:
            self._append_runtime_journal(
                bl,
                str(job_id),
                event="manual_retry_step",
                reason="Interrupted step re-queued by operator",
                detail={
                    "requested_by": str(command.requested_by or "operator").strip() or "operator",
                    "queue_name": queue_name,
                    "thread_id": thread_id,
                    "agent_id": snapshot.get("agent_id"),
                    "skill": snapshot.get("skill"),
                    "step_id": snapshot.get("step_id"),
                    "step_execution_id": snapshot.get("step_execution_id"),
                },
            )
        return JobLifecycleResultView(
            job_id=str(job_id),
            status="pending",
            thread_id=thread_id,
            queue_name=queue_name,
            enqueued=True,
        )


__all__ = [name for name in globals() if not name.startswith("__")]
