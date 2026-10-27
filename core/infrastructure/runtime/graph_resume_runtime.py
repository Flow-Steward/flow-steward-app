"""Reusable helpers for resuming paused graph execution."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from core.application.jobs.execution_contracts import (
    WorkflowResumeGraphOutcome,
    terminal_failure_context,
)
from core.application.jobs.invoke_child_link_repository import InvokeCancellationFenceUnavailable
from core.application.jobs.workflow_resume_state import RESUME_REASON_CHILD, RESUME_REASON_TIMER
from core.application.jobs.workflow_wait_projection import (
    DURABLE_WAIT_STATUSES,
    durable_wait_from_state,
    wait_status,
    workflow_wait_meta,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _build_resume_state_repair_update as _build_resume_state_repair_update,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _build_state_based_approval_update as _build_state_based_approval_update,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _checkpoint_contains_resume_marker as _checkpoint_contains_resume_marker,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _cursor_execution_id as _cursor_execution_id,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _is_state_based_manual_approval as _is_state_based_manual_approval,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _is_state_based_test_run_approval as _is_state_based_test_run_approval,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _resolve_resume_workflow_id_from_checkpoint as _resolve_resume_workflow_id_from_checkpoint,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _resume_timer_wait as _resume_timer_wait,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _rotate_state_approval_id as _rotate_state_approval_id,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _snapshot_has_pending_interrupts as _snapshot_has_pending_interrupts,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _workflow_id_from_state_values as _workflow_id_from_state_values,
)
from core.application.orchestration.graph.resume_checkpoint import (
    _workflow_resume_journal_entry as _workflow_resume_journal_entry,
)
from core.infrastructure.runtime import graph_resume_threads as _graph_resume_threads

build_start_graph_resume_thread_handler = (
    _graph_resume_threads.build_start_graph_resume_thread_handler
)
fence_project_resume_threads = _graph_resume_threads.fence_project_resume_threads
join_resume_threads = _graph_resume_threads.join_resume_threads
launch_graph_resume_subprocess = _graph_resume_threads.launch_graph_resume_subprocess
resume_subprocess_entry = _graph_resume_threads.resume_subprocess_entry
resume_thread_entry = _graph_resume_threads.resume_thread_entry
set_job_status_by_thread_id = _graph_resume_threads.set_job_status_by_thread_id
snapshot_resume_threads = _graph_resume_threads.snapshot_resume_threads
start_graph_resume_thread = _graph_resume_threads.start_graph_resume_thread
unregister_resume_thread = _graph_resume_threads.unregister_resume_thread
unfence_project_resume_threads = _graph_resume_threads.unfence_project_resume_threads


class TerminalPersistenceError(RuntimeError):
    """A checkpoint advanced but its terminal job projection was not persisted."""


class ResumeLeaseLost(RuntimeError):
    """Raised when a runner no longer owns its durable resume lease."""


def _default_terminal_persistence_factory(bl, *, resolve_artifact=None):
    from core.application.jobs.terminal_persistence import TerminalPersistenceService

    return TerminalPersistenceService(bl, resolve_artifact=resolve_artifact)


def _wait_for_checkpointer(
    *,
    thread_id: str,
    resume_shutdown_in_progress,
    get_checkpointer,
    logger,
    checkpointer_attempts: int,
    retry_delay_seconds: float,
    sleep,
):
    checkpointer = None
    for attempt in range(checkpointer_attempts):
        if resume_shutdown_in_progress():
            logger.warning(
                "run_graph_resume: shutdown detected while waiting for checkpointer for thread %s",
                thread_id,
            )
            return None
        checkpointer = get_checkpointer()
        if checkpointer:
            return checkpointer
        logger.warning(
            "run_graph_resume: checkpointer unavailable (attempt %d/%d) for thread %s; retrying in %.1fs",
            attempt + 1,
            checkpointer_attempts,
            thread_id,
            retry_delay_seconds,
        )
        sleep(retry_delay_seconds)
    logger.error(
        "run_graph_resume: checkpointer unavailable after retries; cannot resume thread %s",
        thread_id,
    )
    return None


def _attach_resume_callback(
    *,
    thread_id: str,
    config: dict[str, Any],
    get_logger_only,
    job_thread_store,
    to_json_obj,
    logger,
) -> tuple[Any, str | None, bool]:
    from core.infrastructure.runtime.test_mode import parse_test_mode

    bl = get_logger_only()
    job_id = None
    resume_test_mode = False
    if not bl:
        return None, None, False
    try:
        row = job_thread_store.get_job_context_by_thread_id(bl.conn, thread_id)
        if row:
            job_id = str(row["id"] or "").strip()
            if not job_id:
                raise ValueError("Resume job identity is missing")
    except Exception as exc:
        # Unknown identity must not disable the durable cancellation fence.
        # A successful no-row lookup still supports ephemeral test runs.
        raise InvokeCancellationFenceUnavailable() from exc
    try:
        if row and job_id is not None:
            project_id = row["project_id"]
            row_meta = to_json_obj(row.get("meta"))
            if isinstance(row_meta, dict):
                resume_test_mode = parse_test_mode(row_meta.get("test_mode"), False)
            from core.infrastructure.db.platform_logger import PlatformLoggerCallback

            config["callbacks"] = [PlatformLoggerCallback(bl, project_id, job_id)]
    except Exception as exc:
        logger.warning("run_graph_resume: could not attach logger callback: %s", exc)
    return bl, job_id, resume_test_mode


def _resolve_resume_workflow_id(
    *,
    thread_id: str,
    get_logger_only,
    job_thread_store,
    to_json_obj,
) -> str:
    bl = get_logger_only()
    if not bl:
        return ""
    try:
        row = job_thread_store.get_job_context_by_thread_id(bl.conn, thread_id)
    except Exception:
        row = None
    if not isinstance(row, dict):
        return ""
    job_id = row.get("id")
    job: dict[str, Any] | None = None
    if job_id is not None and hasattr(bl, "get_job"):
        try:
            loaded = bl.get_job(str(job_id))
        except Exception:
            loaded = None
        if isinstance(loaded, dict):
            job = loaded
    if not isinstance(job, dict):
        job = row
    meta = to_json_obj(job.get("meta"))
    if not isinstance(meta, dict):
        meta = {}
    return str(
        job.get("workflow_id")
        or job.get("selected_workflow")
        or job.get("selected_workflow_id")
        or job.get("selected_workflow_key")
        or meta.get("selected_workflow_id")
        or meta.get("requested_workflow_id")
        or meta.get("workflow_id")
        or ""
    ).strip()


def _log_resume_error(
    *,
    exc: Exception,
    bl,
    job_id: str | None,
    thread_id: str,
    action: str | None,
    approved: bool,
    invoke_error: str,
    logger,
    terminal_error_context: Mapping[str, Any] | None = None,
) -> None:
    if not bl or job_id is None:
        return
    try:
        source_hint, category_hint = ("system", "unknown")
        if hasattr(bl, "infer_exception_hints"):
            try:
                source_hint, category_hint = bl.infer_exception_hints(
                    exc,
                    context={
                        "thread_id": thread_id,
                        "resume_action": action,
                        "approved": bool(approved),
                    },
                )
            except Exception:
                source_hint, category_hint = ("system", "unknown")
        resume_context = {
            "thread_id": thread_id,
            "resume_action": action,
            "approved": bool(approved),
            **dict(terminal_error_context or {}),
        }
        normalized = bl.normalize_exception_error(
            exc,
            source_hint=source_hint,
            category_hint=category_hint,
            context=resume_context,
        )
        bl.log_job_error(
            str(job_id),
            source=normalized.get("source", "system"),
            category=normalized.get("category", "unknown"),
            provider_name=normalized.get("provider_name"),
            http_status=normalized.get("http_status"),
            provider_error_code=normalized.get("provider_error_code"),
            provider_error_type=normalized.get("provider_error_type"),
            request_id=normalized.get("request_id"),
            retryable=normalized.get("retryable"),
            error_message=normalized.get("error_message", invoke_error),
            response_excerpt=normalized.get("response_excerpt"),
            traceback_text=normalized.get("traceback_text"),
            context_jsonb=normalized.get("context_jsonb") or {},
        )
    except Exception as struct_err:
        logger.debug("run_graph_resume: structured error logging failed: %s", struct_err)


def _callback_terminal_error_context(config: Mapping[str, Any]) -> dict[str, Any]:
    callbacks = config.get("callbacks")
    if not isinstance(callbacks, list):
        return {}
    for callback in callbacks:
        getter = getattr(callback, "terminal_error_context", None)
        if not callable(getter):
            continue
        with contextlib.suppress(Exception):
            context = getter()
            if isinstance(context, Mapping):
                return {
                    key: context[key]
                    for key in ("step_id", "step_execution_id", "group_id", "occurrence")
                    if context.get(key) not in (None, "")
                }
    return {}


def _detect_graph_status(
    *,
    graph,
    config: dict[str, Any],
    result: dict[str, Any],
    invoke_error: str | None,
    skip_post_interrupt_probe: bool = False,
):
    graph_status = result.get("status", "in_progress")
    graph_error = result.get("error") or invoke_error
    terminal = {
        "completed",
        "failed",
        "stuck",
        "max_iterations",
        "cancelled",
    }
    if graph_status not in terminal | DURABLE_WAIT_STATUSES:
        try:
            post_state = graph.get_state(config)
            if post_state.tasks and any(task.interrupts for task in post_state.tasks):
                wait = durable_wait_from_state(post_state)
                # A state-based approval resume has no interrupt of its own, so
                # an interrupt now is a later durable pause (a Wait, an authored
                # Approval or a child) the resumed run reached.
                if not skip_post_interrupt_probe or wait is not None:
                    graph_status = wait_status(wait)
        except Exception:
            pass
    if graph_status not in terminal | DURABLE_WAIT_STATUSES:
        if skip_post_interrupt_probe and graph_status == "in_progress":
            return graph_status, graph_error, terminal
        unexpected_status = str(graph_status or "").strip() or "in_progress"
        graph_status = "failed"
        if not graph_error:
            graph_error = (
                "Graph resume returned non-terminal status "
                f"'{unexpected_status}' without interrupt; refusing to mark job completed."
            )
    return graph_status, graph_error, terminal


def _is_non_blocking_provider_sync_diagnostic(error: Exception) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    diagnostic_markers = {
        "not available",
        "unavailable",
        "not configured",
        "not registered",
        "not installed",
        "missing extension",
        "extension missing",
        "provider missing",
    }
    provider_markers = {"extension", "provider", "connector", "integration"}
    return any(marker in text for marker in diagnostic_markers) and any(
        marker in text for marker in provider_markers
    )


def _sync_provider_status(*, bl, job_id: str | None, graph_status: str, logger) -> None:
    if job_id is None:
        return
    try:
        from core.application.extensions.provider_completion import sync_provider_completion_status

        sync_provider_completion_status(job_id, graph_status, bl)
    except Exception as provider_err:
        logger.warning("Provider status sync failed for job %s: %s", job_id, provider_err)
        if _is_non_blocking_provider_sync_diagnostic(provider_err):
            logger.info(
                "Provider status sync diagnostic for job %s is non-blocking: %s",
                job_id,
                provider_err,
            )
            return
        if bl and hasattr(bl, "log_job_error"):
            try:
                bl.log_job_error(
                    str(job_id),
                    source="provider_runtime",
                    category="integration",
                    provider_name="external_provider",
                    error_message=str(provider_err),
                    context_jsonb={
                        "job_id": str(job_id),
                        "graph_status": graph_status,
                        "side_effect": "sync_provider_completion_status",
                    },
                )
            except Exception as log_exc:
                logger.debug(
                    "Could not persist provider status sync error for job %s: %s",
                    job_id,
                    log_exc,
                )


def _persist_graph_resume_result(
    *,
    bl,
    job_id: str | None,
    thread_id: str,
    graph_status: str,
    graph_error: str | None,
    result: dict[str, Any],
    resume_test_mode: bool,
    job_thread_store,
    logger,
    resolve_artifact=None,
    terminal_persistence_factory=None,
    terminal_error_context: Mapping[str, Any] | None = None,
) -> None:
    if not bl:
        return
    try:
        if graph_status in {
            "completed",
            "failed",
            "max_iterations",
            "cancelled",
        }:
            factory = terminal_persistence_factory or _default_terminal_persistence_factory
            terminal_service = factory(bl, resolve_artifact=resolve_artifact)
            if graph_status == "completed":
                terminal_result = terminal_service.complete_success(
                    job_id=str(job_id),
                    workflow_outputs=result.get("workflow_outputs"),
                    final_report=result.get("final_report"),
                )
            else:
                error_context = terminal_failure_context(
                    result,
                    observed=terminal_error_context,
                )
                context_kwargs = {
                    key: error_context[key]
                    for key in (
                        "step_id",
                        "step_execution_id",
                        "group_id",
                        "occurrence",
                        "selector",
                        "diagnostic_kind",
                        "correlation_id",
                    )
                    if error_context.get(key) not in (None, "")
                }
                if error_context.get("path"):
                    context_kwargs["diagnostic_path"] = error_context["path"]
                terminal_result = terminal_service.complete_failure(
                    job_id=str(job_id),
                    status=graph_status,
                    error_message=graph_error,
                    # Only a real code. Falling back to the run status wrote
                    # code="failed", which says nothing the status field did
                    # not already say and displaced the projection's own
                    # fallback ladder.
                    error_code=str(
                        result.get("error_code") or error_context.get("error_code") or ""
                    ).strip()
                    or None,
                    category="cancellation" if graph_status == "cancelled" else "execution",
                    final_report=result.get("final_report"),
                    **context_kwargs,
                )
            graph_status = terminal_result.status
            if resume_test_mode:
                logger.info(
                    "run_graph_resume: job_id=%s test_mode active; skipping provider status sync",
                    job_id,
                )
            else:
                _sync_provider_status(
                    bl=bl,
                    job_id=job_id,
                    graph_status=graph_status,
                    logger=logger,
                )
        else:
            job_thread_store.update_job_status_by_thread_id(
                bl.conn,
                thread_id=thread_id,
                status=graph_status,
                commit=True,
            )
            if graph_status == "awaiting_approval":
                _rotate_state_approval_id(
                    bl=bl,
                    thread_id=thread_id,
                    job_thread_store=job_thread_store,
                    logger=logger,
                )
    except Exception as exc:
        logger.exception("run_graph_resume: could not persist terminal job state")
        if graph_status in {
            "completed",
            "failed",
            "max_iterations",
            "cancelled",
        }:
            raise TerminalPersistenceError(
                f"Graph checkpoint advanced but terminal job persistence failed: {exc}"
            ) from exc
        raise


def run_graph_resume(
    thread_id: str,
    resume_value: dict[str, Any],
    approved: bool,
    action: str | None,
    comment: str,
    interrupt_payload: dict[str, Any],
    *,
    actor_username: str = "operator",
    approval_id: str | None = None,
    resume_shutdown_in_progress,
    get_checkpointer,
    build_graph,
    get_logger_only,
    job_thread_store,
    to_json_obj,
    append_approval_history,
    build_approval_entry,
    logger,
    resolve_artifact=None,
    terminal_persistence_factory=None,
    resume_cancelled=None,
    defer_terminal_persistence: bool = False,
    resume_revision: int = 0,
    checkpointer_attempts: int = 5,
    retry_delay_seconds: float = 3.0,
    sleep=time.sleep,
) -> SimpleNamespace | WorkflowResumeGraphOutcome | None:
    """
    Resume a paused graph after approval and persist the new job state.

    The heavy runtime logic lives here so HTTP transport code can stay thin.
    """
    del actor_username, append_approval_history, build_approval_entry, interrupt_payload
    from langgraph.types import Command

    from core.application.orchestration.agents.common import (
        JobCancelledException,
        run_graph_with_cancel_check,
    )
    from core.infrastructure.runtime.test_mode import reset_runtime_test_mode, set_runtime_test_mode

    _ = resume_revision

    def _assert_resume_lease() -> None:
        if callable(resume_cancelled) and resume_cancelled():
            raise ResumeLeaseLost("Workflow resume execution lease was lost")

    if resume_shutdown_in_progress():
        logger.warning(
            "run_graph_resume: shutdown in progress, skipping resume for thread %s",
            thread_id,
        )
        return WorkflowResumeGraphOutcome.not_started(reason="worker_stopping")

    checkpointer = _wait_for_checkpointer(
        thread_id=thread_id,
        resume_shutdown_in_progress=resume_shutdown_in_progress,
        get_checkpointer=get_checkpointer,
        logger=logger,
        checkpointer_attempts=checkpointer_attempts,
        retry_delay_seconds=retry_delay_seconds,
        sleep=sleep,
    )
    if not checkpointer:
        return WorkflowResumeGraphOutcome.not_started(reason="checkpoint_unavailable")

    workflow_id = _resolve_resume_workflow_id(
        thread_id=thread_id,
        get_logger_only=get_logger_only,
        job_thread_store=job_thread_store,
        to_json_obj=to_json_obj,
    )
    if not workflow_id:
        workflow_id = _resolve_resume_workflow_id_from_checkpoint(
            thread_id=thread_id,
            checkpointer=checkpointer,
            build_graph=build_graph,
            to_json_obj=to_json_obj,
            logger=logger,
        )
    # The hub restores the pinned definition. Revalidating mutable registry
    # contracts here would make an existing run depend on later author edits.
    graph = build_graph(
        checkpointer=checkpointer,
        workflow_ids=[workflow_id] if workflow_id else None,
        validate_contracts=False,
    )
    logger.info("run_graph_resume: thread %s -> graph built for resume", thread_id)
    from core.application.orchestration.graph_runtime import graph_run_config

    config: dict[str, Any] = graph_run_config(thread_id)

    if resume_shutdown_in_progress():
        logger.warning(
            "run_graph_resume: shutdown in progress before invoke for thread %s",
            thread_id,
        )
        return WorkflowResumeGraphOutcome.not_started(reason="worker_stopping")

    bl = None
    job_id = None
    resume_test_mode = False
    test_mode_token = None
    invoke_error: str | None = None
    graph_status = "in_progress"
    state_based_test_run_resume = False
    pre_resume_execution_id = ""
    try:
        try:
            _assert_resume_lease()
            bl, job_id, resume_test_mode = _attach_resume_callback(
                thread_id=thread_id,
                config=config,
                get_logger_only=get_logger_only,
                job_thread_store=job_thread_store,
                to_json_obj=to_json_obj,
                logger=logger,
            )
            if resume_test_mode:
                logger.info(
                    "run_graph_resume: thread %s job_id=%s running in test_mode",
                    thread_id,
                    job_id,
                )
            test_mode_token = set_runtime_test_mode(resume_test_mode)
            pre_resume_snapshot = graph.get_state(config)
            pre_resume_values = getattr(pre_resume_snapshot, "values", {}) or {}
            # Timer and child wakes name the recorded wait they end; either is
            # delivered only to that wait, never to another pause.
            timer_wait_id = (
                str(resume_value.get("wait_id") or "").strip()
                if isinstance(resume_value, Mapping)
                and resume_value.get("reason") in (RESUME_REASON_TIMER, RESUME_REASON_CHILD)
                else ""
            )
            if timer_wait_id:
                result = _resume_timer_wait(
                    snapshot=pre_resume_snapshot,
                    values=pre_resume_values,
                    wait_id=timer_wait_id,
                    resume_value=resume_value,
                    repair_update=lambda: _build_resume_state_repair_update(
                        state_values=pre_resume_values,
                        bl=bl,
                        thread_id=thread_id,
                        job_thread_store=job_thread_store,
                        to_json_obj=to_json_obj,
                        logger=logger,
                    ),
                    run=lambda graph_input: run_graph_with_cancel_check(
                        graph,
                        graph_input,
                        config,
                        bl=bl,
                        job_id=job_id,
                        continue_check=_assert_resume_lease,
                    ),
                )
            elif (
                _is_state_based_test_run_approval(pre_resume_snapshot)
                or _is_state_based_manual_approval(pre_resume_snapshot)
                or _checkpoint_contains_resume_marker(pre_resume_values, approval_id)
                or thread_id.startswith("test-run-")
            ):
                state_based_test_run_resume = True
                pre_resume_execution_id = _cursor_execution_id(pre_resume_values)
                checkpoint_already_applied = _checkpoint_contains_resume_marker(
                    pre_resume_values, approval_id
                )
                if checkpoint_already_applied:
                    update_payload = {
                        "status": str(pre_resume_values.get("status") or "in_progress")
                    }
                else:
                    update_payload = _build_state_based_approval_update(
                        approved=approved,
                        action=action,
                        comment=comment,
                        resume_value=resume_value,
                        state_values=pre_resume_values
                        if isinstance(pre_resume_values, dict)
                        else None,
                    )
                    if approval_id:
                        update_payload["approval_resume_applied_id"] = str(approval_id)
                    # A superseded owner must not write the decision checkpoint.
                    _assert_resume_lease()
                    graph.update_state(
                        config,
                        update_payload,
                        as_node="human_approval_gate",
                    )
                # The marker is in the same checkpoint update as the decision.
                # Keep a recoverable journal state until terminal projection has
                # committed: recovery will see the marker and never replay the
                # old approval into a later gate.
                resume_status = str(update_payload.get("status") or "").strip().lower()
                if resume_status and resume_status != "in_progress":
                    # Terminal decision (e.g. abort -> cancelled): the state
                    # update above is the final state; do not re-enter the graph.
                    result = dict(update_payload)
                else:
                    # Approve / reject. ``human_approval_gate`` routes purely via
                    # Command(goto=...) and therefore has NO static outgoing edge,
                    # so the ``update_state`` above leaves ``next`` empty —
                    # ``stream(None)`` would do nothing and the run would hang at
                    # awaiting_approval forever (BUG-3). The approval writes were
                    # already applied via ``update_state``; seed the orchestrator
                    # hub explicitly so the workflow actually advances past the
                    # gate, mirroring the gate's own real-job route
                    # (Command(update=..., goto="orchestrator")).
                    result = run_graph_with_cancel_check(
                        graph,
                        Command(goto="orchestrator"),
                        config,
                        bl=bl,
                        job_id=job_id,
                        continue_check=_assert_resume_lease,
                    )
            else:
                resume_state_update = _build_resume_state_repair_update(
                    state_values=pre_resume_values,
                    bl=bl,
                    thread_id=thread_id,
                    job_thread_store=job_thread_store,
                    to_json_obj=to_json_obj,
                    logger=logger,
                )
                if approval_id:
                    resume_state_update = dict(resume_state_update or {})
                    resume_state_update["approval_resume_applied_id"] = str(approval_id)
                command_kwargs: dict[str, Any] = {"resume": resume_value}
                if resume_state_update:
                    command_kwargs["update"] = resume_state_update
                result = run_graph_with_cancel_check(
                    graph,
                    Command(**command_kwargs),
                    config,
                    bl=bl,
                    job_id=job_id,
                    continue_check=_assert_resume_lease,
                )
                # ``Command(update=...)`` durably writes the marker together
                # with LangGraph's resume checkpoint.  If the process dies
                # before this bookkeeping update, the marker still prevents a
                # stale recovery from replaying the decision.
        except ResumeLeaseLost:
            logger.info(
                "run_graph_resume: lease lost for thread %s; skipping stale persistence", thread_id
            )
            return None
        except InvokeCancellationFenceUnavailable as exc:
            if callable(resume_cancelled) and resume_cancelled():
                return None
            logger.warning("run_graph_resume: cancellation fence unavailable for job_id=%s", job_id)
            return WorkflowResumeGraphOutcome.for_retry(reason=exc.error_code)
        except JobCancelledException as exc:
            logger.info("run_graph_resume: job_id=%s cancelled mid-resume; stopping", job_id)
            result = {"status": "cancelled"}
            if exc.error_code:
                result.update(error_code=exc.error_code, error=str(exc))
        except Exception as exc:
            logger.exception(
                "run_graph_resume: graph.invoke failed for thread %s: %s", thread_id, exc
            )
            invoke_error = str(exc)
            if state_based_test_run_resume:
                failed_update = {
                    "needs_user_approval": False,
                    "approval_context": None,
                    "status": "failed",
                    "error": invoke_error,
                    "final_report": invoke_error,
                }
                with contextlib.suppress(Exception):
                    graph.update_state(
                        config,
                        failed_update,
                        as_node="human_approval_gate",
                    )
            terminal_error_context = _callback_terminal_error_context(config)
            _log_resume_error(
                exc=exc,
                bl=bl,
                job_id=job_id,
                thread_id=thread_id,
                action=action,
                approved=approved,
                invoke_error=invoke_error,
                logger=logger,
                terminal_error_context=terminal_error_context,
            )
            result = {"status": "failed", "error": invoke_error}

        if state_based_test_run_resume:
            try:
                post_resume_snapshot = graph.get_state(config)
                post_values = getattr(post_resume_snapshot, "values", {}) or {}
                post_execution_id = _cursor_execution_id(post_values)
                result_status = str((result or {}).get("status") or "").strip().lower()
                checkpoint_status = str(post_values.get("status") or "").strip().lower()
                terminal_checkpoint_statuses = {
                    "completed",
                    "failed",
                    "stuck",
                    "max_iterations",
                    "cancelled",
                }
                if checkpoint_status in terminal_checkpoint_statuses:
                    result = {
                        **(result or {}),
                        "status": checkpoint_status,
                        "error": post_values.get("error") or (result or {}).get("error"),
                        "final_report": post_values.get("final_report")
                        or (result or {}).get("final_report"),
                    }
                    result_status = checkpoint_status
                if (
                    pre_resume_execution_id
                    and post_execution_id
                    and post_execution_id != pre_resume_execution_id
                    and result_status == "awaiting_approval"
                ):
                    from core.application.orchestration.approval_payload import (
                        NoPendingApprovalGate,
                        state_based_approval_payload,
                    )

                    current_gate = False
                    # Bare legacy cursor fragments carry no pinned authority.
                    # A versioned run must never fall back to that normalization
                    # when its definition or runtime marker becomes unavailable.
                    legacy_cursor = all(
                        values.get(key) is None
                        for values in (pre_resume_values, post_values)
                        for key in ("runtime_schema_version", "workflow_definition_ref")
                    )
                    if not legacy_cursor and _is_state_based_test_run_approval(
                        post_resume_snapshot
                    ):
                        try:
                            payload = state_based_approval_payload(post_values)
                            if not payload:
                                raise ValueError("authoritative approval identity is unavailable")
                        except NoPendingApprovalGate:
                            # A real non-Approval step can inherit stale pause flags.
                            pass
                        except Exception as exc:
                            logger.warning(
                                "run_graph_resume: next approval projection unavailable: %s", exc
                            )
                            return WorkflowResumeGraphOutcome(
                                started=True,
                                status="awaiting_approval",
                                retryable=True,
                                reason="approval_projection_unavailable",
                                result=dict(result or {}),
                                error=str(exc),
                            )
                        else:
                            current_gate = True
                    if not current_gate:
                        result = {**result, "status": "in_progress"}
            except Exception:
                pass

        graph_status, graph_error, _terminal = _detect_graph_status(
            graph=graph,
            config=config,
            result=result,
            invoke_error=invoke_error,
            skip_post_interrupt_probe=state_based_test_run_resume,
        )
        terminal_error_context = _callback_terminal_error_context(config)
        if defer_terminal_persistence:
            deferred_result = dict(result or {})
            if graph_status in DURABLE_WAIT_STATUSES:
                # The new pause, mirrored by the finalizer into workflow_wait.
                deferred_result.pop("workflow_wait", None)
                try:
                    wait = durable_wait_from_state(graph.get_state(config))
                    if wait is None and graph_status != "awaiting_approval":
                        raise ValueError("authoritative wait occurrence is unavailable")
                    if wait is not None:
                        if wait_status(wait) != graph_status:
                            raise ValueError("authoritative wait does not match the paused status")
                        deferred_result["workflow_wait"] = workflow_wait_meta(
                            wait, thread_id=thread_id
                        )
                except Exception as exc:
                    return WorkflowResumeGraphOutcome(
                        started=True,
                        status=graph_status,
                        retryable=True,
                        reason="wait_projection_unavailable",
                        result=deferred_result,
                        error=str(exc),
                    )
            if graph_status == "awaiting_approval":
                try:
                    from core.application.jobs.workflow_run_projection import (
                        approval_notification_payload_from_state,
                    )

                    approval = approval_notification_payload_from_state(graph.get_state(config))
                except Exception as exc:
                    return SimpleNamespace(
                        started=True,
                        status="awaiting_approval",
                        retryable=True,
                        reason="approval_projection_unavailable",
                        result=deferred_result,
                        error=str(exc),
                        terminal=False,
                    )
                if not approval:
                    return SimpleNamespace(
                        started=True,
                        status="awaiting_approval",
                        retryable=True,
                        reason="approval_projection_unavailable",
                        result=deferred_result,
                        error="authoritative approval occurrence is unavailable",
                        terminal=False,
                    )
                deferred_result["approval"] = approval
            return SimpleNamespace(
                started=True,
                status=graph_status,
                retryable=False,
                reason="",
                result=deferred_result,
                error=graph_error,
                terminal=graph_status in _terminal,
            )
        if callable(resume_cancelled) and resume_cancelled():
            logger.info(
                "run_graph_resume: lease lost before result persistence for thread %s", thread_id
            )
            return None
        _persist_graph_resume_result(
            bl=bl,
            job_id=job_id,
            thread_id=thread_id,
            graph_status=graph_status,
            graph_error=graph_error,
            result=result,
            resume_test_mode=resume_test_mode,
            job_thread_store=job_thread_store,
            logger=logger,
            resolve_artifact=resolve_artifact,
            terminal_persistence_factory=terminal_persistence_factory,
            terminal_error_context=terminal_error_context,
        )
    finally:
        if test_mode_token is not None:
            reset_runtime_test_mode(test_mode_token)

    logger.info(
        "run_graph_resume: thread=%s approved=%s action=%s -> %s",
        thread_id,
        approved,
        action,
        graph_status,
    )
    return None


def build_run_graph_resume_handler(
    *,
    resume_shutdown_in_progress,
    get_checkpointer,
    build_graph,
    get_logger_only,
    job_thread_store,
    to_json_obj,
    append_approval_history,
    build_approval_entry,
    logger,
    resolve_artifact=None,
    terminal_persistence_factory=None,
):
    def _runner(*args, **kwargs):
        return run_graph_resume(
            *args,
            **kwargs,
            resume_shutdown_in_progress=resume_shutdown_in_progress,
            get_checkpointer=get_checkpointer,
            build_graph=build_graph,
            get_logger_only=get_logger_only,
            job_thread_store=job_thread_store,
            to_json_obj=to_json_obj,
            append_approval_history=append_approval_history,
            build_approval_entry=build_approval_entry,
            logger=logger,
            resolve_artifact=resolve_artifact,
            terminal_persistence_factory=terminal_persistence_factory,
        )

    return _runner
