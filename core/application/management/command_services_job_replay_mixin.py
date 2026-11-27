# ruff: noqa: F403, F405
# mypy: ignore-errors
"""Step replay and interrupted-step retry for ``JobCommandService``.

``execute_step_replay`` re-runs one recorded step of a job against its stored
replay snapshot and records the comparison; ``retry_interrupted_step`` puts a
job whose step was cut off back on the queue at that step.
"""

from __future__ import annotations

from core.application.orchestration.agent_message_projection import (
    public_replay_outcome,
)

from .command_services_common import *  # NOSONAR


def _job_group_for_step(job: dict[str, Any], step_id: Any) -> str | None:
    """The job's recorded group, kept only while the job is still on *step_id*."""
    step = str(step_id or "").strip()
    if step and str(job.get("current_step_id") or "").strip() == step:
        return str(job.get("current_group_id") or "").strip() or None
    return None


class JobCommandServiceReplayMixin:
    def execute_step_replay(
        self,
        job_id: str,
        command: JobStepReplayExecutionInput,
        *,
        scope: ScopeContext | None = None,
    ) -> JobStepReplayExecutionResultView:
        bl = self._logger()
        resolved = _service_scope(scope, project_id=None)
        job = self._require_job_in_scope(bl, str(job_id), resolved, permission="jobs.manage")
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
        job = self._require_job_in_scope(bl, str(job_id), resolved, permission="jobs.write")

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
