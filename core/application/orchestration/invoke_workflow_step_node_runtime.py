"""Child runtime and task-envelope helpers for invoke_workflow_step_node."""

from __future__ import annotations

import contextlib
import copy
import logging
import time
import uuid
from contextvars import Context
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.application.orchestration.child_execution_runtime import (
    current_child_execution_runtime,
)
from core.application.orchestration.helpers import now_iso as _now_iso
from core.application.orchestration.helpers import to_bool as _to_bool
from core.application.orchestration.invoke_workflow_step_node_common import (
    _CHILD_POLICY_CONTEXT_KEYS,
    _SENSITIVE_RUNTIME_KEY_FRAGMENTS,
    TERMINAL_CHILD_STATUSES,
    _cancel_child_job_on_timeout,
    _parse_status,
    _resolve_invoke_child_poll_seconds,
    _resolve_invoke_child_timeout_seconds,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState


def _parent_job_cancelled(bl, parent_job_id: str) -> bool:
    """Report whether the parent job has been cancelled out from under this wait."""
    if not parent_job_id:
        return False
    try:
        row = bl.get_job(parent_job_id)
    except Exception:
        return False
    return bool(
        isinstance(row, dict) and str(row.get("status") or "").strip().lower() == "cancelled"
    )


def _wait_for_terminal_child(
    bl,
    child_job_id: Any,
    *,
    parent_job_id: Any | None = None,
    parent_agent: str = "",
    child_workflow_id: str = "",
) -> tuple[dict | None, str]:
    timeout_seconds = _resolve_invoke_child_timeout_seconds()
    poll_seconds = _resolve_invoke_child_poll_seconds()
    deadline = time.time() + timeout_seconds

    last_row = None
    normalized_parent_job_id = str(parent_job_id or "").strip()
    while time.time() < deadline:
        if normalized_parent_job_id and _parent_job_cancelled(bl, normalized_parent_job_id):
            # Cancelling the parent has to end this wait: otherwise the worker slot
            # and its database connection stay held until the invoke timeout expires.
            logger.info(
                "invoke_workflow: parent job %s was cancelled; releasing wait on child %s",
                normalized_parent_job_id,
                child_job_id,
            )
            with contextlib.suppress(Exception):
                refreshed = bl.get_job(child_job_id)
                if isinstance(refreshed, dict):
                    last_row = refreshed
            return last_row, "cancelled"
        if normalized_parent_job_id and hasattr(bl, "update_job_runtime"):
            try:
                bl.update_job_runtime(
                    normalized_parent_job_id,
                    current_agent=parent_agent or None,
                    heartbeat_now=True,
                    last_response_excerpt=(
                        f"Waiting for child workflow '{child_workflow_id or 'child'}' "
                        f"(job {child_job_id})"
                    ),
                )
            except Exception as heartbeat_err:
                logger.debug(
                    "invoke_workflow: parent heartbeat update failed parent_job_id=%s child_job_id=%s: %s",
                    parent_job_id,
                    child_job_id,
                    heartbeat_err,
                )
        row = bl.get_job(child_job_id)
        if isinstance(row, dict):
            last_row = row
            status = _parse_status(row.get("status"))
            if status in TERMINAL_CHILD_STATUSES:
                return row, status
        time.sleep(poll_seconds)

    if _cancel_child_job_on_timeout(
        bl,
        child_job_id,
        timeout_seconds=timeout_seconds,
        child_workflow_id=child_workflow_id,
    ):
        with contextlib.suppress(Exception):
            refreshed = bl.get_job(child_job_id)
            if isinstance(refreshed, dict):
                last_row = refreshed

    return last_row, "timeout"


def _current_worker_identity() -> str:
    runtime = current_child_execution_runtime()
    return str(runtime.worker_id or "").strip() if runtime is not None else ""


def _should_execute_child_inline(parent_job: dict | None) -> bool:
    if not _to_bool(_runtime_env.get("FS_INVOKE_INLINE_EXECUTION", "1"), True):
        return False
    if not isinstance(parent_job, dict):
        return False
    parent_worker_id = str(parent_job.get("worker_id") or "").strip()
    if not parent_worker_id:
        return False
    current_worker_id = _current_worker_identity()
    return bool(current_worker_id and current_worker_id == parent_worker_id)


def _execute_child_job_inline(task_message: dict) -> dict[str, object]:
    runtime = current_child_execution_runtime()
    if runtime is None:
        raise RuntimeError("child execution runtime is not bound")

    # A child graph invoked synchronously from a parent LangGraph node would
    # otherwise inherit the parent's runnable/callback ContextVars. That makes
    # the parent PlatformLoggerCallback observe child events and overwrite the
    # parent's workflow/phase projection with the child's runtime state.
    # Execute the child delivery in a fresh context; its TaskExecutionService
    # establishes the child-owned runtime, MCP, and test-mode scopes itself.
    return dict(Context().run(runtime.execute_inline, dict(task_message)))


def _strip_invoke_summary_prefix(summary: str) -> str:
    normalized = str(summary or "").strip()
    if not normalized.startswith("[invoke_workflow:"):
        return normalized
    _head, sep, tail = normalized.partition("] ")
    return tail.strip() if sep else normalized


def _first_non_empty_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _scoped_item_summary_from_inputs(child_inputs: dict[str, Any] | None) -> str:
    inputs = child_inputs if isinstance(child_inputs, dict) else {}
    candidates: list[dict[str, Any]] = []
    for preferred_key in ("current_item", "item", "record", "subject"):
        preferred = inputs.get(preferred_key)
        if isinstance(preferred, dict):
            candidates.append(preferred)
    for value in inputs.values():
        if isinstance(value, dict) and value not in candidates:
            candidates.append(value)

    for candidate in candidates:
        title = _first_non_empty_text(
            candidate.get("title"),
            candidate.get("name"),
            candidate.get("summary"),
            candidate.get("label"),
            candidate.get("description"),
        )
        identifier = _first_non_empty_text(
            candidate.get("id"),
            candidate.get("key"),
            candidate.get("slug"),
            candidate.get("code"),
        )
        if title and identifier:
            return f"{identifier}: {title}"
        if title:
            return title
        if identifier:
            return identifier

    return _first_non_empty_text(
        inputs.get("title"),
        inputs.get("summary"),
        inputs.get("description"),
        inputs.get("name"),
    )


def _derive_child_task_summary(
    parent_summary: str,
    workflow_id: str,
    step_id: str,
    *,
    child_inputs: dict[str, Any] | None = None,
) -> str:
    normalized_parent = _strip_invoke_summary_prefix(parent_summary)
    inputs = child_inputs if isinstance(child_inputs, dict) else {}
    explicit_summary = _strip_invoke_summary_prefix(str(inputs.get("task_summary") or ""))
    scoped_item_summary = _scoped_item_summary_from_inputs(inputs)

    # If the child invocation inherited the parent summary unchanged while also
    # providing a narrower work item, scope the child workflow to that work item.
    if scoped_item_summary and (not explicit_summary or explicit_summary == normalized_parent):
        return scoped_item_summary

    if explicit_summary:
        return explicit_summary
    if scoped_item_summary:
        return scoped_item_summary
    if normalized_parent:
        return normalized_parent
    return f"Invoke child workflow '{workflow_id}' from step '{step_id}'"


def _strip_sensitive_runtime_values(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key or "").strip().lower()
            if any(fragment in key_text for fragment in _SENSITIVE_RUNTIME_KEY_FRAGMENTS):
                continue
            cleaned[key] = _strip_sensitive_runtime_values(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_sensitive_runtime_values(item) for item in value]
    return copy.deepcopy(value)


def _child_policy_runtime_context(state: OrchestratorState) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for key in _CHILD_POLICY_CONTEXT_KEYS:
        if key not in state:
            continue
        value = state.get(key)
        if value is None:
            continue
        context[key] = _strip_sensitive_runtime_values(value)
    return context


def _resolve_child_runtime_context(
    state: OrchestratorState,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Propagate the current project policy into child workflow execution payloads."""
    project_config = (
        state.get("project_config") if isinstance(state.get("project_config"), dict) else {}
    )
    child_project_config = dict(project_config)
    child_policy_context = _child_policy_runtime_context(state)
    child_project_config.update(child_policy_context)
    return child_project_config, copy.deepcopy(child_policy_context)


def _build_child_task_id(
    parent_task_id: str,
    step_id: str,
    workflow_id: str,
) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    safe_step = str(step_id or "step").strip() or "step"
    safe_workflow = str(workflow_id or "workflow").strip() or "workflow"
    return f"{parent_task_id}:invoke:{safe_workflow}:{safe_step}:{ts}"


def _build_child_summary(
    parent_summary: str,
    workflow_id: str,
    step_id: str,
    *,
    hook_label: str = "",
) -> str:
    """The child job's task summary.

    A hook's run reads as the hook (*hook_label*), never as the storage
    workflow that keeps its steps.
    """
    invoke_tag = f"[invoke_workflow:{'hook' if hook_label else workflow_id}]"
    if parent_summary:
        return f"{invoke_tag} {parent_summary}"
    if hook_label:
        return f"Hook '{hook_label}' from step '{step_id}'"
    return f"Invoke child workflow '{workflow_id}' from step '{step_id}'"


def _build_child_task_envelope(
    *,
    state: OrchestratorState,
    child_workflow_id: str,
    child_task_summary: str,
    child_inputs: dict[str, Any],
    project_id: str,
    parent_workflow_id: str,
    step_id: str,
) -> dict[str, Any]:
    from core.application.orchestration.task_envelope import TaskEnvelope

    parent_envelope = (
        dict(state.get("task_envelope") or {})
        if isinstance(state.get("task_envelope"), dict)
        else {}
    )
    parent_files = (
        list(parent_envelope.get("files") or [])
        if isinstance(parent_envelope.get("files"), list)
        else []
    )
    parent_metadata = (
        dict(parent_envelope.get("metadata") or {})
        if isinstance(parent_envelope.get("metadata"), dict)
        else {}
    )
    parent_entry_context = (
        dict(parent_envelope.get("entry_context") or {})
        if isinstance(parent_envelope.get("entry_context"), dict)
        else {}
    )

    child_envelope = TaskEnvelope(
        entry_path=str(parent_envelope.get("entry_path") or "").strip(),
        source_system=str(parent_envelope.get("source_system") or "invoke_workflow").strip()
        or "invoke_workflow",
        source_event_id=str(state.get("job_id") or "").strip(),
        source_object_id=str(state.get("task_id") or "").strip(),
        task_type=str(
            child_inputs.get("task_type")
            or parent_envelope.get("task_type")
            or state.get("mode")
            or ""
        ).strip(),
        title=child_task_summary,
        description=str(child_inputs.get("description") or child_task_summary).strip(),
        priority=str(parent_envelope.get("priority") or state.get("priority") or "").strip(),
        project_id=str(project_id or "").strip(),
        requested_workflow_id=str(child_workflow_id or "").strip(),
        inputs=dict(child_inputs or {}),
        files=parent_files,
        metadata={
            **parent_metadata,
            # The parent's own routing identity must not travel into the child:
            # inheriting it makes the child's envelope name the parent.
            "requested_workflow_id": str(child_workflow_id or "").strip() or None,
            "invoked_by_workflow_id": str(parent_workflow_id or "").strip() or None,
            "invoked_from_step_id": str(step_id or "").strip() or None,
        },
        entry_context={
            **parent_entry_context,
            "workflow_selection_source": "parent_call",
        },
    ).to_dict()
    child_envelope["metadata"] = {
        key: value
        for key, value in dict(child_envelope.get("metadata") or {}).items()
        if value is not None
    }
    return child_envelope


def _append_agent_message(
    *,
    state: OrchestratorState,
    from_agent: str,
    skill: str,
    status: str,
    summary: str,
    artifacts: dict | None = None,
    errors: list[str] | None = None,
    started_at: str | None = None,
) -> dict:
    """Publish the invoke step's result with the active step's identity."""
    from core.application.orchestration.active_step import active_step
    from core.application.orchestration.agent_messages import build_agent_message
    from core.application.orchestration.agents.common import publish_agent_message

    timestamp = _now_iso()
    safe_artifacts = dict(artifacts or {})
    current = active_step(state)
    step = current.step if current is not None else {}
    step_display_name = str(step.get("display_name") or "").strip()
    step_kind = str(step.get("step_kind") or "invoke_workflow").strip()
    if current is not None:
        safe_artifacts.setdefault("step_id", current.step_id)
    if step_display_name:
        safe_artifacts.setdefault("step_display_name", step_display_name)
    safe_artifacts.setdefault("step_kind", step_kind or "invoke_workflow")
    message = build_agent_message(
        from_agent=from_agent or "invoke_workflow",
        to_agent="orchestrator",
        skill=skill or "invoke_workflow",
        status=status,
        correlation_id=uuid.uuid4().hex,
        caused_by=[],
        artifacts=safe_artifacts,
        summary=summary,
        errors=[str(item) for item in (errors or []) if str(item).strip()],
        started_at=str(started_at or timestamp),
        completed_at=timestamp,
        published_at=timestamp,
        **(current.message_identity() if current is not None else {}),
    )
    publish_agent_message(state, message)
    return message
