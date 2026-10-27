"""Durable task-request preparation and trusted job-context restoration."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from core.application.jobs.execution_contracts import (
    TaskExecutionRequest,
)
from core.application.jobs.queue_delivery_runtime import (
    queue_dead_letter_queue_name,
)
from core.application.jobs.workflow_identity import resolve_job_workflow_id
from core.application.orchestration.state_path_guards import strip_reserved_runtime_state
from core.application.workflows.test_run_input_bindings import apply_test_run_input_bindings

logger = logging.getLogger("core.application.jobs.task_execution_service")

_INVOKE_META_STATE_KEYS = (
    "invoke_child_auto_approve",
    "invoke_parent_job_id",
    "invoke_parent_task_id",
    "invoke_parent_workflow_id",
    "invoke_parent_step_id",
    "invoke_parent_step_agent_id",
    "invoke_parent_step_skill_id",
    "invoke_child_workflow_id",
)


_TRUTHY_JOB_META_STATE_KEYS = (
    "orchestrator_plan",
    "feedback_type",
    "parent_task_id",
    "parent_job_id",
    "parent_workflow_id",
)


@dataclass(frozen=True)
class _ProjectLookup:
    project_id: str | None = None
    attempted: bool = False
    row: Mapping[str, Any] | None = None
    error: Exception | None = None


def _build_request(
    message: Mapping[str, Any],
    job_row: Mapping[str, Any] | None,
    *,
    parent_job_lookup: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> TaskExecutionRequest:
    project_config = message.get("project_config")
    normalized_project_config = dict(project_config) if isinstance(project_config, Mapping) else {}
    raw_extra = message.get("extra_state")
    raw_extra_state = dict(raw_extra) if isinstance(raw_extra, Mapping) else {}
    workflow_id = _clean(message.get("workflow_id") or raw_extra_state.get("workflow_id"))
    extra_state = strip_reserved_runtime_state(raw_extra_state)
    if workflow_id:
        extra_state["workflow_id"] = workflow_id
        normalized_project_config.setdefault("workflow_id", workflow_id)
    job_id = _clean(message.get("job_id"))
    if job_id:
        extra_state["job_id"] = job_id
    test_mode = _parse_test_mode(message.get("test_mode"), False)
    if job_row:
        test_mode, workflow_id = _restore_job_meta(
            job_row,
            extra_state,
            normalized_project_config,
            test_mode,
            workflow_id,
            parent_job_lookup,
        )
    extra_state = _promote_callable_inputs(extra_state)
    if _clean(message.get("task_source")).lower() in {"step_test_run", "workflow_test_run"}:
        task_envelope = extra_state.get("task_envelope")
        if isinstance(task_envelope, Mapping):
            extra_state.update(apply_test_run_input_bindings(extra_state, task_envelope))
    if test_mode:
        extra_state["test_mode"] = True
    return TaskExecutionRequest(
        message=dict(message),
        job_id=job_id,
        task_id=_clean(message.get("task_id")),
        thread_id=_clean(message.get("thread_id")),
        task_summary=_clean(message.get("task_summary")),
        task_source=_clean(message.get("task_source")),
        project_name=_clean(message.get("project_name")),
        project_config=normalized_project_config,
        extra_state=extra_state,
        workflow_id=workflow_id,
        test_mode=test_mode,
    )


def _restore_job_meta(
    job_row: Mapping[str, Any],
    extra_state: dict[str, Any],
    project_config: dict[str, Any],
    test_mode: bool,
    workflow_id: str,
    parent_job_lookup: Callable[[str], Mapping[str, Any] | None] | None,
) -> tuple[bool, str]:
    raw_meta = job_row.get("meta")
    meta = dict(raw_meta) if isinstance(raw_meta, Mapping) else {}
    trusted_workflow = _clean(resolve_job_workflow_id(dict(job_row)))
    if trusted_workflow:
        workflow_id = trusted_workflow
        extra_state["workflow_id"] = trusted_workflow
        project_config["workflow_id"] = trusted_workflow
    if (
        _clean(job_row.get("task_source")).lower() == "step_test_run"
        and meta.get("step_test_run") is True
    ):
        step_id = _clean(meta.get("step_id"))
        execution_id = _clean(meta.get("execution_id"))
        if trusted_workflow and step_id and execution_id:
            from core.application.orchestration.run_cursor import seeded_step_cursor

            extra_state["workflow_test_run_execution_id"] = execution_id
            extra_state["step_test_run_context"] = {
                "workflow_id": trusted_workflow,
                "step_id": step_id,
                "execution_id": execution_id,
            }
            # The isolated run starts at the selected step; the cursor comes from
            # trusted job meta, never from the delivered envelope.
            extra_state["run_cursor"] = seeded_step_cursor(step_id, seed=execution_id).dump()
    if (
        _clean(job_row.get("task_source")).lower() == "workflow_test_run"
        and meta.get("workflow_test_run") is True
        and isinstance(meta.get("kept_test_outputs"), Mapping)
    ):
        # Kept test outputs come only from trusted job meta of a workflow test run.
        extra_state["workflow_test_run_kept_outputs"] = {
            str(step_id): dict(outputs)
            for step_id, outputs in meta["kept_test_outputs"].items()
            if isinstance(outputs, Mapping)
        }
    test_mode = test_mode or _parse_test_mode(meta.get("test_mode"), False)
    if meta.get("attachments"):
        extra_state.setdefault("attachments", meta["attachments"])
    trusted_invoke_child = _has_confirmed_invoke_child_lineage(
        parent_job_lookup,
        job_row,
        meta,
    )
    if not trusted_invoke_child:
        extra_state.pop("invoke_child_auto_approve", None)
    for key in _INVOKE_META_STATE_KEYS:
        if key == "invoke_child_auto_approve" and not trusted_invoke_child:
            continue
        if key in meta:
            extra_state.setdefault(key, meta[key])
    for key in ("task_envelope", "workflow_input_payload", "inputs"):
        value = meta.get(key)
        if isinstance(value, Mapping) and value:
            extra_state.setdefault(key, dict(value))
    for key in _TRUTHY_JOB_META_STATE_KEYS:
        if meta.get(key):
            extra_state.setdefault(key, meta[key])
    if meta.get("resume_parent") is not None:
        extra_state.setdefault("resume_parent", bool(meta.get("resume_parent")))
    return test_mode, workflow_id


def _has_confirmed_invoke_child_lineage(
    parent_job_lookup: Callable[[str], Mapping[str, Any] | None] | None,
    job_row: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> bool:
    if _clean(job_row.get("task_source")).lower() != "invoke_workflow":
        return False
    if meta.get("invoke_child_auto_approve") is not True:
        return False
    parent_job_id = _clean(meta.get("invoke_parent_job_id"))
    parent_task_id = _clean(meta.get("invoke_parent_task_id"))
    parent_workflow_id = _clean(meta.get("invoke_parent_workflow_id"))
    child_workflow_id = _clean(meta.get("invoke_child_workflow_id"))
    if not all((parent_job_id, parent_task_id, parent_workflow_id, child_workflow_id)):
        return False
    if not callable(parent_job_lookup):
        return False
    try:
        parent = parent_job_lookup(parent_job_id)
    except Exception:
        return False
    if not isinstance(parent, Mapping):
        return False
    if _clean(parent.get("project_id")) != _clean(job_row.get("project_id")):
        return False
    if _clean(parent.get("task_id")) != parent_task_id:
        return False
    raw_parent_meta = parent.get("meta")
    parent_meta = dict(raw_parent_meta) if isinstance(raw_parent_meta, Mapping) else {}
    durable_parent_workflow = _clean(
        parent.get("selected_workflow")
        or parent.get("selected_workflow_key")
        or parent_meta.get("selected_workflow_id")
    )
    return durable_parent_workflow == parent_workflow_id


def _promote_callable_inputs(extra_state: dict[str, Any]) -> dict[str, Any]:
    try:
        from core.application.orchestration.state import promote_callable_input_state

        return promote_callable_input_state(extra_state)
    except Exception as exc:
        logger.debug("Callable workflow input promotion skipped: %s", exc)
        return extra_state


def _with_project_scope(
    request: TaskExecutionRequest,
    job_row: Mapping[str, Any] | None,
    project_lookup: _ProjectLookup,
) -> TaskExecutionRequest:
    project_id = project_lookup.project_id
    if not project_id and not job_row:
        return request
    extra_state = dict(request.extra_state)
    project_config = dict(request.project_config)
    if project_id:
        extra_state["project_id"] = project_id
        project_config["project_id"] = project_id
    for source in (project_lookup.row, job_row):
        if not source:
            continue
        for key in ("account_id", "tenant_id", "workspace_id"):
            value = _clean(source.get(key))
            if value:
                extra_state[key] = value
                project_config[key] = value
    scoped_request: TaskExecutionRequest = replace(
        request,
        extra_state=extra_state,
        project_config=project_config,
    )
    return scoped_request


def _queue_context(
    message: Mapping[str, Any],
    job_row: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str, str, Mapping[str, Any] | None]:
    raw_meta = (job_row or {}).get("meta")
    meta = _coerce_mapping(raw_meta)
    policy = meta.get("project_policy_snapshot")
    project_policy = policy if isinstance(policy, Mapping) else None
    queue_name = _clean((job_row or {}).get("queue_name") or message.get("queue_name"))
    if not queue_name:
        project_id = _clean((job_row or {}).get("project_id") or message.get("project_id"))
        queue_name = f"platform.project.{project_id}" if project_id else ""
    return meta, queue_name, queue_dead_letter_queue_name(queue_name), project_policy


def _parse_test_mode(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        with contextlib.suppress(Exception):
            loaded = json.loads(value)
            if isinstance(loaded, Mapping):
                return dict(loaded)
    return {}


def _clean(value: Any) -> str:
    return str(value or "").strip()
