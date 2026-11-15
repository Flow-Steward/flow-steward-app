"""Launch of the child job of an ``invoke_workflow`` step.

The child is enqueued as its own workflow start. Where the parent has a job,
the child is linked to it in the creating transaction (one child per
invocation, see ``invoke_child_link_repository``). The child either runs inline
under the current project lease or is published to the queue.

The node passes in the collaborators it owns (inline execution, the link, the
artifact plane and its logger), so they are resolved from the node at the call.
Errors propagate unchanged: the node decides what a conflict or a closed parent
means for the step.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.application.orchestration.helpers import now_iso as _now_iso
from core.application.orchestration.helpers import to_bool as _to_bool

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState


def launch_child_job(
    state: OrchestratorState,
    *,
    bl: Any,
    attempted_inline: bool,
    parent_job_id: str,
    step_id: str,
    step_execution_id: str,
    step_agent: str,
    step_skill: str,
    invocation_key: str,
    child_workflow_id: str,
    child_input_key: str,
    child_task_id: str,
    child_summary: str,
    project_id: str,
    project_name: str,
    child_project_config: Any,
    child_extra_state: dict[str, Any],
    child_secret_ref_snapshot: Any,
    child_meta: dict[str, Any],
    execute_inline: Callable[[dict[str, Any]], dict[str, Any]],
    link_in_transaction: Callable[..., Any],
    artifact_plane_provider: Callable[[], Any],
    logger: logging.Logger,
) -> Any:
    """Enqueue the child workflow start and return the new child job id."""
    from core.application.jobs.task_enqueue_runtime import enqueue_job
    from core.application.orchestration.agents.common import emit_event
    from core.application.workflows.workflow_start_runtime import enqueue_workflow_start

    def _dispatch_child(**task_message: Any) -> None:
        if attempted_inline:
            emit_event(
                state,
                "orchestrator",
                "invoke_workflow_inline_execute",
                f"Executing child workflow '{child_workflow_id}' inline under current project lease",
                {
                    "child_job_id": task_message.get("job_id"),
                    "child_workflow_id": child_workflow_id,
                    "parent_job_id": parent_job_id,
                },
            )
            try:
                disposition = execute_inline(dict(task_message))
                action = str(disposition.get("action") or "").strip()
                if action == "retry":
                    from core.infrastructure.runtime.worker.queue_publisher import (
                        enqueue_task,
                    )

                    enqueue_task(**task_message)
                    return
                if action != "ack":
                    reason = str(disposition.get("reason") or "inline_execution_incomplete")
                    raise RuntimeError(f"inline child execution was not acknowledged: {reason}")
            except Exception as inline_error:
                logger.warning(
                    "invoke_workflow inline child execution raised job_id=%s child_job_id=%s: %s",
                    parent_job_id,
                    task_message.get("job_id"),
                    inline_error,
                )
                raise
            return

        from core.infrastructure.runtime.worker.queue_publisher import enqueue_task

        enqueue_task(**task_message)

    def _link_child(conn: Any, created_job_id: str) -> None:
        # Linked in the creating transaction under the parent row
        # lock: one child per invocation, none after a parent cancel.
        link_in_transaction(
            conn,
            parent_job_id=parent_job_id,
            child_job_id=created_job_id,
            entry={
                "child_workflow_id": child_workflow_id,
                "step_id": step_id,
                "step_execution_id": step_execution_id,
                "invocation_key": invocation_key,
                "step_agent": step_agent,
                "step_skill": step_skill,
                "input_fingerprint": child_input_key,
                "created_at": _now_iso(),
            },
        )

    def _enqueue_child(**kwargs: Any) -> str:
        return str(
            enqueue_job(
                bl=bl,
                enqueue_task_fn=_dispatch_child,
                build_project_config_snapshot=lambda config: copy.deepcopy(config),
                logger=logger,
                log_prefix="invoke_workflow",
                artifact_plane=artifact_plane_provider(),
                job_created_hook=_link_child if parent_job_id else None,
                **kwargs,
            )
        )

    launched_child_job_id = enqueue_workflow_start(
        enqueue_task_fn=_enqueue_child,
        task_id=child_task_id,
        task_summary=child_summary,
        task_source="invoke_workflow",
        project_id=project_id,
        project_name=project_name,
        project_config=child_project_config,
        selected_workflow_id=child_workflow_id,
        workflow_selection_source="parent_call",
        entry_path="invoke_workflow",
        input_contract="task_envelope",
        source_system="invoke_workflow",
        requested_workflow_id=child_workflow_id,
        source_event_id=child_task_id,
        source_object_id=parent_job_id,
        test_mode=_to_bool(child_extra_state.get("test_mode"), False),
        task_envelope=dict(child_extra_state.get("task_envelope") or {}),
        inputs=dict(child_extra_state.get("inputs") or {}),
        files=list(child_extra_state.get("files") or []),
        extra_state=child_extra_state,
        workflow_secret_ref_fingerprints=child_secret_ref_snapshot,
        job_meta=child_meta,
    )
    return (
        int(launched_child_job_id)
        if str(launched_child_job_id).isdigit()
        else launched_child_job_id
    )
