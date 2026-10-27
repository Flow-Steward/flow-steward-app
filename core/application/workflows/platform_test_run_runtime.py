"""Ephemeral builder-native workflow and step test-run helpers."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any, cast

from fastapi import HTTPException

from core.application.orchestration.agents.common_runtime_helpers import (
    build_prompt_safe_state,
)
from core.application.orchestration.agents.prompt_agent import PromptAgent
from core.application.orchestration.agents.prompt_builder import PromptBuilder
from core.application.orchestration.control_flow_step_node import control_flow_step_node
from core.application.orchestration.graph.nodes import execute_step_node
from core.application.orchestration.graph_runtime import build_registry_graph
from core.application.orchestration.helpers import resolve_step_agent
from core.application.orchestration.invoke_workflow_step_node import invoke_workflow_step_node
from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION, seeded_step_cursor
from core.application.orchestration.run_definition import (
    RUNTIME_SCHEMA_VERSION_KEY,
    WORKFLOW_DEFINITION_REF_KEY,
    prime_run_definition,
)
from core.application.orchestration.state import (
    OrchestratorState,
    build_initial_orchestrator_state,
    promote_callable_input_state,
)
from core.application.registry.registry_manager import RegistryManager
from core.application.registry.workflow_manifest import StepConfig
from core.application.workflows import platform_step_test_run_helpers as _step_helpers
from core.application.workflows import platform_test_run_common as _common
from core.application.workflows.test_run_input_bindings import (
    apply_test_run_input_bindings as _apply_test_run_input_bindings,
)
from core.application.workflows.test_run_result_values import (
    _materialize_workflow_outputs,
    _step_test_result_artifacts,
)
from core.application.workflows.workflow_definition_snapshot import (
    definition_digest,
    definition_ref,
    frozen_definition,
)
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_AGENT,
    STEP_KIND_INVOKE_WORKFLOW,
    is_control_flow_step_kind,
    normalize_step_kind,
)

logger = logging.getLogger(__name__)

# A Test Run parks at an approval gate by *state* — the ephemeral runner
# stops streaming the graph as soon as the state says `awaiting_approval`, so the
# graph never reaches a LangGraph `interrupt()` and the checkpoint carries no
# interrupt to identify the gate by. The approval lifecycle is keyed on an
# approval id everywhere (the payload advertises it, the decision endpoint
# requires it, `validate_current_gate` compares against it), so a state-based
# gate needs a durable identity of its own. It lives on the anchor job's meta
# under this key (AUDIT-038 symptom b).
STATE_APPROVAL_ID_META_KEY = "state_approval_id"

_text = _common._text
_jsonable = _common._jsonable
redact_test_run_payload = _common.redact_test_run_payload
_coerce_update = _common._coerce_update
_as_dict = _common._as_dict
_state_selector_without_default = _step_helpers._state_selector_without_default
_extract_unresolved_template_selectors = _step_helpers._extract_unresolved_template_selectors
_fail_step_test_run = _step_helpers._fail_step_test_run
_preflight_step_test_run = _step_helpers._preflight_step_test_run
resolve_step_for_test_run = _step_helpers.resolve_step_for_test_run


def _latest_message(state: dict[str, Any]) -> dict[str, Any] | None:
    messages = state.get("agent_messages")
    if isinstance(messages, list):
        for item in reversed(messages):
            if isinstance(item, dict):
                return item
    return None


def _reload_registry_for_test_run() -> RegistryManager:
    with suppress(Exception):
        return RegistryManager.read_only()
    return RegistryManager.instance()


def _get_thread_state_values(thread_id: str, *, force_refresh: bool = False) -> dict:
    from core.infrastructure.runtime.control import state_reader

    return state_reader.get_thread_state_values(thread_id, force_refresh=force_refresh)


def _get_thread_state_snapshot(thread_id: str, *, force_refresh: bool = False):
    from core.infrastructure.runtime.control import state_reader

    return state_reader.get_thread_state_snapshot(thread_id, force_refresh=force_refresh)


def _current_approval_id(thread_id: str) -> str:
    if not thread_id:
        return ""
    from core.application.jobs.workflow_run_projection import ensure_single_approval_interrupt

    snapshot = _get_thread_state_snapshot(thread_id)
    interrupt = ensure_single_approval_interrupt(snapshot)
    if interrupt is None:
        return ""
    return _text(getattr(interrupt, "id", None) or getattr(interrupt, "interrupt_id", None))


def _approval_id_from_state(final_state: Mapping[str, Any]) -> str:
    """Recover the approval id from the run state when the checkpoint has none.

    The checkpoint snapshot is the primary source, but it is written
    independently of the state the graph returns. A run could therefore report
    `awaiting_approval` while the snapshot carried no interrupt yet, and the
    payload then advertised an approval with no id — which is exactly the dead
    end the reviewer hit: *"Workflow test run approval ID is unavailable. Refresh
    before deciding."* with the run stuck awaiting approval (AUDIT-038).
    """
    from core.application.orchestration.approval_payload import (
        state_based_approval_payload,
    )

    gate = state_based_approval_payload(final_state)
    if gate:
        return _text(gate.get("approval_id"))
    for key in ("approval_id", "interrupt_id"):
        candidate = _text(final_state.get(key))
        if candidate:
            return candidate
    pending = final_state.get("pending_interrupt") or final_state.get("approval_interrupt")
    if isinstance(pending, dict):
        for key in ("approval_id", "id", "interrupt_id"):
            candidate = _text(pending.get(key))
            if candidate:
                return candidate
    return ""


def _resolve_project_scope(bl, project_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    project = bl.get_project(project_id)
    if not isinstance(project, dict):
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    run_cfg = bl.get_project_run_config(project_id) or {}
    if not isinstance(run_cfg, dict):
        run_cfg = {}
    return project, run_cfg


def _test_run_task_envelope(
    workflow_id: str,
    project_id: str,
    task_summary: str,
    envelope: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(envelope, dict) and envelope:
        payload = dict(envelope)
        payload.setdefault("project_id", project_id)
        payload.setdefault("requested_workflow_id", workflow_id)
        payload.setdefault("title", task_summary)
        payload.setdefault("description", task_summary)
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.setdefault("source", "workflow_test_run")
        metadata.setdefault("requested_workflow_id", workflow_id)
        payload["metadata"] = metadata
        inputs = payload.get("inputs")
        if not isinstance(inputs, dict):
            inputs = {}
        inputs.setdefault("project_id", project_id)
        payload["inputs"] = inputs
        return payload
    return {
        "title": task_summary,
        "description": task_summary,
        "project_id": project_id,
        "requested_workflow_id": workflow_id,
        "inputs": {"project_id": project_id},
        "metadata": {
            "source": "workflow_test_run",
            "requested_workflow_id": workflow_id,
        },
    }


def _build_initial_state(
    *,
    workflow_id: str,
    project_id: str,
    task_summary: str,
    task_source: str,
    project_name: str,
    project_config: dict[str, Any],
    task_id: str,
    task_envelope: dict[str, Any],
    extra_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project_config = {
        **dict(project_config or {}),
        # The selected workflow argument is the launch authority for this run.
        # A child inherits the parent's project configuration, whose workflow_id
        # still names the parent; letting that trusted scope overwrite this value
        # makes an ephemeral child execute the parent recursively.
        "workflow_id": workflow_id,
    }
    workflow_inputs = task_envelope.get("inputs")
    if not isinstance(workflow_inputs, dict):
        workflow_inputs = {}
    scope_project_id = (
        _text(project_config.get("project_id") if isinstance(project_config, dict) else "")
        or project_id
    )
    scope_account_id = _text(
        project_config.get("account_id") if isinstance(project_config, dict) else ""
    )
    scope_tenant_id = _text(
        project_config.get("tenant_id") if isinstance(project_config, dict) else ""
    )
    scope_workspace_id = _text(
        project_config.get("workspace_id") if isinstance(project_config, dict) else ""
    )
    base_extra_state = {
        "project_id": scope_project_id,
        "account_id": scope_account_id or None,
        "tenant_id": scope_tenant_id or None,
        "workspace_id": scope_workspace_id or None,
        "workflow_id": workflow_id,
        "workflow_selection_source": "explicit_request",
        "task_envelope": task_envelope,
        "inputs": dict(workflow_inputs),
        # Skill input validation resolves bare required fields like "request"
        # from materialized_inputs during ephemeral test runs.
        "materialized_inputs": dict(workflow_inputs),
        "debug_test_run": True,
    }
    if isinstance(extra_state, dict):
        base_extra_state.update(extra_state)
    base_extra_state = promote_callable_input_state(base_extra_state)
    state = build_initial_orchestrator_state(
        task_id=task_id,
        task_summary=task_summary,
        task_source=task_source,
        project_name=project_name,
        project_config=project_config,
        test_mode=True,
        status="in_progress",
        extra_state=base_extra_state,
    )
    binding_update = _apply_test_run_input_bindings(state, task_envelope)
    if binding_update:
        state.update(binding_update)
    return state


def _artifact_source_payload(artifacts: dict[str, Any]) -> Any:
    for key in (
        "input_payload",
        "connector_invocation",
        "connector_result",
        "http_request",
        "dataset_query",
        "_invoke_workflow",
        "workflow_outputs",
        "workflow_terminal",
    ):
        if key in artifacts:
            return artifacts.get(key)
    return None


def _message_to_trace_entry(message: dict[str, Any]) -> dict[str, Any]:
    artifacts = message.get("artifacts")
    artifacts = dict(artifacts) if isinstance(artifacts, dict) else {}
    step_kind = _text(artifacts.get("step_kind")) or (
        "invoke_workflow" if "_invoke_workflow" in artifacts else ""
    )
    step_id = _text(message.get("step_id")) or _text(artifacts.get("step_id"))
    display_name = _text(artifacts.get("step_display_name"))
    errors = message.get("errors") if isinstance(message.get("errors"), list) else []
    source_payload = _artifact_source_payload(artifacts)
    return {
        "step_id": step_id,
        "step_execution_id": _text(message.get("step_execution_id")),
        "group_id": _text(message.get("group_id")),
        "step_kind": step_kind or "agent",
        "step_label": display_name or step_id or _text(message.get("from_agent")) or "step",
        "status": _text(message.get("status")) or "unknown",
        "summary": _text(message.get("summary")),
        "error_summary": _text(errors[0] if errors else artifacts.get("error")),
        "source_payload": redact_test_run_payload(source_payload),
        "artifacts": redact_test_run_payload(artifacts),
        "from_agent": _text(message.get("from_agent")),
        "skill": _text(message.get("skill")),
    }


def _workflow_trace_from_state(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    messages = state.get("agent_messages")
    if not isinstance(messages, list):
        return []
    trace: list[dict[str, Any]] = []
    previous_fingerprint: tuple[str, str, str, str] | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        entry = _message_to_trace_entry(message)
        fingerprint = (
            _text(entry.get("step_execution_id")),
            _text(entry.get("step_id")),
            _text(entry.get("status")),
            _text(entry.get("summary")) or _text(entry.get("error_summary")),
        )
        if fingerprint == previous_fingerprint:
            continue
        trace.append(entry)
        previous_fingerprint = fingerprint
    return trace


def _cursor_step_execution_id(state: Mapping[str, Any]) -> str:
    cursor = state.get("run_cursor")
    return _text(cursor.get("step_execution_id")) if isinstance(cursor, Mapping) else ""


def _pending_human_gate_outcome(
    final_state: Mapping[str, Any],
    trace: list[dict[str, Any]],
) -> tuple[str, str]:
    """Whether the run waits for a human at its active step execution.

    A test run parks by state: either a recovery approval the hub opened for
    the execution, or the execution's authored Approval step. An Approval
    execution that already reported its decision is past the gate, whatever
    status the checkpoint still carries.
    """
    if _text(final_state.get("status")).lower() != "awaiting_approval":
        return "", ""
    explicit_context = _text(final_state.get("approval_context"))
    pending = final_state.get("recovery_pending")
    if isinstance(pending, Mapping) and pending:
        return (
            "awaiting_approval",
            explicit_context
            or _text(pending.get("reason"))
            or "Workflow is waiting for human approval.",
        )
    execution_id = _cursor_step_execution_id(final_state)
    if execution_id and any(
        isinstance(entry, dict) and _text(entry.get("step_execution_id")) == execution_id
        for entry in trace
    ):
        return "", ""
    step_id = _text(final_state.get("current_step_id"))
    return (
        "awaiting_approval",
        explicit_context
        or (
            f"Step '{step_id}' is waiting for human approval."
            if step_id
            else "Workflow is waiting for human approval."
        ),
    )


def _derive_workflow_final_outcome(
    final_state: Mapping[str, Any],
    trace: list[dict[str, Any]],
) -> tuple[str, str]:
    explicit_status = _text(final_state.get("status"))
    explicit_report = _text(final_state.get("final_report"))
    if explicit_status and explicit_status not in {
        "in_progress",
        "resume_pending",
        "queued",
        "awaiting_approval",
    }:
        return explicit_status, explicit_report

    pending_status, pending_report = _pending_human_gate_outcome(final_state, trace)
    if pending_status:
        return pending_status, pending_report

    # Workflow resume can leave a stale explicit awaiting_approval status in checkpoint
    # state even after the graph has advanced beyond the human gate. Once no pending
    # human-approval gate is actually active, treat the state as resumed and continue
    # deriving the real in-progress/terminal outcome from the trace.
    if explicit_status == "awaiting_approval":
        explicit_status = "in_progress"

    for entry in reversed(trace):
        if not isinstance(entry, dict):
            continue
        status = _text(entry.get("status"))
        if status == "fail":
            return "fail", _text(entry.get("summary")) or _text(entry.get("error_summary"))
    return explicit_status or "in_progress", explicit_report


def run_workflow_test_run_ephemeral(
    *,
    workflow_id: str,
    project_id: str,
    task_summary: str,
    task_id: str,
    task_envelope: dict[str, Any],
    project_name: str,
    project_config: dict[str, Any],
    extra_state: dict[str, Any] | None = None,
    bl: Any = None,
) -> dict[str, Any]:
    registry = _reload_registry_for_test_run()
    thread_id = f"test-run-{uuid.uuid4().hex}"
    execution_id = uuid.uuid4().hex
    initial_state = _build_initial_state(
        workflow_id=workflow_id,
        project_id=project_id,
        task_summary=task_summary,
        task_source="workflow_test_run",
        project_name=project_name,
        project_config=project_config,
        task_id=task_id,
        task_envelope=task_envelope,
        extra_state=extra_state,
    )
    initial_state["workflow_test_run_thread_id"] = thread_id
    initial_state["workflow_test_run_execution_id"] = execution_id
    from core.application.orchestration.graph_runtime import graph_run_config

    invoke_config = graph_run_config(thread_id)
    final_state: dict[str, Any] = {}
    with RegistryManager.process_instance_override(registry):
        graph = build_registry_graph(
            checkpointer=None,
            workflow_ids=[workflow_id],
            registry=registry,
            refresh_registry=False,
        )
        for snapshot in graph.stream(initial_state, invoke_config, stream_mode="values"):
            if isinstance(snapshot, dict):
                final_state = snapshot
                snapshot_status = _text(snapshot.get("status")).lower()
                if (
                    snapshot_status == "awaiting_approval"
                    or snapshot.get("needs_user_approval") is True
                ):
                    break
    if not final_state:
        final_state = initial_state
    payload = _workflow_test_run_payload_from_state(
        final_state,
        workflow_id=workflow_id,
        project_id=project_id,
        thread_id=thread_id,
        execution_id=execution_id,
    )
    state_approval_id = ""
    if payload.get("needs_user_approval") and not _text(payload.get("approval_id")):
        # No interrupt could be resolved, so mint the gate's durable identity here
        # rather than advertising an approval nobody can act on.
        state_approval_id = f"state-approval-{uuid.uuid4().hex}"
    job_id = anchor_test_run_awaiting_approval(
        bl,
        payload,
        project_id=project_id,
        workflow_id=workflow_id,
        task_id=task_id,
        task_summary=task_summary,
        state_approval_id=state_approval_id,
    )
    if job_id:
        payload["job_id"] = job_id
        if state_approval_id:
            payload["approval_id"] = state_approval_id
            payload["approval_undecidable_reason"] = ""
    return payload


def anchor_test_run_awaiting_approval(
    bl,
    payload: Mapping[str, Any],
    *,
    project_id: str,
    workflow_id: str,
    task_id: str,
    task_summary: str,
    state_approval_id: str = "",
) -> str:
    """Give a Test Run parked at an approval gate a durable job row.

    A run that needs a human decision has to be durable enough to receive one.
    Test runs are durable in the *checkpointer* but were not durable as *jobs*,
    and both halves of the approval lifecycle are job-anchored:

    * `get_awaiting_approval_jobs` selects `j.status = 'awaiting_approval'` from
      `jobs`, so a run with no row can never appear in the approvals drawer;
    * `ApprovalCommandService.resume_task` resolves the owning project through
      the same row in order to run `require_project_access`, so a run with no
      row answers 404 *"Approval job not found"* when someone tries to decide it.

    That is one root cause under two of the `AUDIT-038` symptoms (RD-004): the
    reviewer could neither see the pending approval nor act on it. The jobless
    rejection in `resume_task` is a deliberate security property and is left
    exactly as it is — the row is what carries the scope, so the fix is to give
    the run an anchor, never to skip the check.

    The anchor is created only when the run actually parks at a gate. A test run
    that completes stays ephemeral, so the common case is unchanged.

    Returns the job id, or "" when no anchor was needed or one could not be
    created. Failing to anchor must not fail the run itself: the caller still
    gets its result, it is simply not decidable until the run is re-driven.
    """
    if bl is None:
        return ""
    if not payload.get("needs_user_approval"):
        return ""
    thread_id = _text(payload.get("thread_id"))
    if not thread_id:
        return ""
    meta: dict[str, Any] = {
        "selected_workflow_id": workflow_id,
        "workflow_test_run": True,
        "execution_id": _text(payload.get("execution_id")),
    }
    durable_approval_id = _text(state_approval_id) or _text(payload.get("approval_id"))
    if durable_approval_id:
        # Polling reads the anchor row rather than the transient checkpoint
        # projection, so persist the current gate identity regardless of whether
        # it came from a real interrupt or the state-based fallback.
        meta[STATE_APPROVAL_ID_META_KEY] = durable_approval_id
    try:
        return (
            _text(
                bl.create_job(
                    project_id,
                    task_id,
                    task_summary=task_summary,
                    task_source="workflow_test_run",
                    thread_id=thread_id,
                    status="awaiting_approval",
                    meta=meta,
                )
            )
            or ""
        )
    except Exception:  # pragma: no cover - anchoring is best effort
        logger.warning(
            "Could not anchor workflow test run %s awaiting approval; "
            "the run is not decidable until it is re-driven",
            thread_id,
        )
        return ""


def _step_test_run_inputs(final_state: Mapping[str, Any]) -> dict[str, Any]:
    """The values a step run was given: workflow inputs plus the step's own inputs.

    A step run keeps the values typed for the step's inputs in
    ``metadata.input_binding_values`` so the workflow interface does not judge
    them; the report still lists them as the inputs the step used.
    """
    inputs = _as_dict(final_state.get("inputs"))
    metadata = _as_dict(_as_dict(final_state.get("task_envelope")).get("metadata"))
    return {**inputs, **_as_dict(metadata.get("input_binding_values"))}


def _workflow_test_run_payload_from_state(
    final_state: Mapping[str, Any],
    *,
    workflow_id: str,
    project_id: str,
    thread_id: str = "",
    execution_id: str = "",
) -> dict[str, Any]:
    trace = _workflow_trace_from_state(final_state)
    derived_status, derived_report = _derive_workflow_final_outcome(final_state, trace)
    workflow_outputs, outputs, artifacts = _materialize_workflow_outputs(final_state)
    approval_context = _text(final_state.get("approval_context"))
    if derived_status == "awaiting_approval" and not approval_context:
        approval_context = derived_report
    resolved_thread_id = thread_id or _text(final_state.get("workflow_test_run_thread_id"))
    resolved_execution_id = (
        execution_id or _text(final_state.get("workflow_test_run_execution_id")) or uuid.uuid4().hex
    )
    approval_id = ""
    approval_undecidable_reason = ""
    if derived_status == "awaiting_approval":
        approval_id = _current_approval_id(resolved_thread_id) or _approval_id_from_state(
            final_state
        )
        if not approval_id:
            # Never advertise an approval the caller cannot act on. Saying so
            # explicitly beats a null id that the UI can only report as
            # "unavailable, refresh before deciding" — advice that did not help,
            # because refreshing did not produce one either.
            approval_undecidable_reason = (
                "The run is awaiting approval but no approval id could be resolved from "
                "the checkpoint or the run state, so it cannot be decided from here. "
                "Use the job approvals surface for this thread."
            )
    payload = {
        "status": derived_status,
        "workflow_id": workflow_id,
        "project_id": project_id,
        "thread_id": resolved_thread_id or None,
        "execution_id": resolved_execution_id,
        "final_status": derived_status,
        "final_report": derived_report,
        "error": _text(final_state.get("error")),
        "needs_user_approval": derived_status == "awaiting_approval",
        "approval_id": approval_id or None,
        "approval_undecidable_reason": approval_undecidable_reason or None,
        "approval_context": (approval_context or None)
        if derived_status == "awaiting_approval"
        else None,
        "current_step_id": _text(final_state.get("current_step_id")),
        "trace": trace,
        "outputs": redact_test_run_payload(outputs),
        "artifacts": redact_test_run_payload(artifacts),
        "workflow_outputs": redact_test_run_payload(workflow_outputs),
        "workflow_terminal": redact_test_run_payload(final_state.get("workflow_terminal")),
        "state": redact_test_run_payload(
            {
                "inputs": final_state.get("inputs") or {},
                "workflow_data": final_state.get("workflow_data") or {},
            }
        ),
    }
    if _text(final_state.get("task_source")).lower() == "step_test_run" and trace:
        latest_step = trace[-1]
        latest_artifacts = _step_test_result_artifacts(
            final_state, _as_dict(latest_step.get("artifacts"))
        )
        payload.update(
            {
                "step_id": _text(latest_step.get("step_id")),
                "step_kind": _text(latest_step.get("step_kind")),
                "summary": _text(latest_step.get("summary")),
                "inputs": redact_test_run_payload(_step_test_run_inputs(final_state)),
                "result": redact_test_run_payload(latest_artifacts),
                "state_patch": redact_test_run_payload(
                    _as_dict(latest_artifacts.get("_state_patch"))
                ),
            }
        )
    return payload


def get_workflow_test_run_state(
    thread_id: str,
    *,
    durable_state_reader: Callable[[str], Mapping[str, Any] | None],
) -> dict[str, Any]:
    normalized_thread_id = _text(thread_id)
    if not normalized_thread_id:
        return {}

    durable = dict(durable_state_reader(normalized_thread_id) or {})
    final_state = _get_thread_state_values(normalized_thread_id)
    initial_status = (
        _text(final_state.get("status")).lower() if isinstance(final_state, dict) else ""
    )
    if initial_status == "awaiting_approval":
        refreshed_state = _get_thread_state_values(
            normalized_thread_id,
            force_refresh=True,
        )
        refreshed_status = (
            _text(refreshed_state.get("status")).lower()
            if isinstance(refreshed_state, dict)
            else ""
        )
        if (
            isinstance(refreshed_state, dict)
            and refreshed_state
            and (
                refreshed_status != initial_status
                or refreshed_state.get("user_approved") is True
                or refreshed_state.get("needs_user_approval") is False
            )
        ):
            final_state = refreshed_state
    if not isinstance(final_state, dict) or not final_state:
        # Submission persists the queued job before the worker has written its
        # first graph checkpoint.  During that normal window the durable public
        # projection is the only authoritative state; returning an empty object
        # turns the first poll into a false 404 and makes the builder abandon an
        # otherwise healthy run.
        return durable
    task_envelope = _as_dict(final_state.get("task_envelope"))
    project_id = _text(final_state.get("project_id")) or _text(task_envelope.get("project_id"))
    workflow_id = (
        _text(final_state.get("selected_workflow"))
        or _text(final_state.get("requested_workflow_id"))
        or _text(task_envelope.get("requested_workflow_id"))
        or _text(final_state.get("workflow_id"))
    )
    if not workflow_id:
        return durable
    payload = _workflow_test_run_payload_from_state(
        final_state,
        workflow_id=workflow_id,
        project_id=project_id,
        thread_id=normalized_thread_id,
    )
    for key in (
        "job_id",
        "project_name",
        "task_id",
        "task_summary",
        "task_envelope",
        "mode",
        "test_mode",
    ):
        if durable.get(key) is not None and payload.get(key) in (None, "", {}):
            payload[key] = durable[key]
    durable_status = _text(durable.get("status")).lower()
    # A run parked on a Wait timer or a child workflow keeps ``in_progress`` in
    # its graph state; only the durable row says what it waits for.
    if durable_status in {
        "resume_pending",
        "in_progress",
        "awaiting_timer",
        "awaiting_child",
        "completed",
        "failed",
        "stuck",
        "max_iterations",
        "cancelled",
    } and (
        durable_status == "resume_pending"
        or payload.get("status") == "awaiting_approval"
        or durable_status not in {"in_progress"}
    ):
        payload["status"] = durable_status
        payload["final_status"] = durable_status
        payload["needs_user_approval"] = False
        payload["approval_context"] = None
        if durable_status in {
            "completed",
            "failed",
            "stuck",
            "max_iterations",
            "cancelled",
        }:
            payload["error"] = _text(durable.get("error")) or payload.get("error")
            payload["final_report"] = _text(durable.get("final_report")) or payload.get(
                "final_report"
            )
    return payload


def _resolve_agent_preview(
    *,
    state: Mapping[str, Any],
    step: dict[str, Any],
) -> dict[str, Any]:
    state_dict = dict(state)
    registry = _reload_registry_for_test_run()
    agent_id = resolve_step_agent(step, registry, state=state_dict)
    if not agent_id:
        return {}
    manifest = registry.agents.get_agent(agent_id)
    if manifest is None:
        return {}
    skill = _text(step.get("skill") or step.get("skill_id"))
    prompt_state = build_prompt_safe_state(state_dict)
    builder = PromptBuilder()
    system_prompt, user_prompt, _ = builder.build(
        agent=manifest,
        skill=skill,
        step=StepConfig.from_dict(step),
        tool_registry=registry.tools,
        state=prompt_state,
        feedback=state.get("feedback"),
    )
    tool_config = PromptAgent(manifest, tool_registry=registry.tools)._build_tool_config(
        state_dict,
        skill=skill,
    )
    if tool_config:
        user_prompt = user_prompt + "\n\n" + tool_config
    return {
        "agent_id": agent_id,
        "skill": skill,
        "system_prompt": redact_test_run_payload(system_prompt),
        "user_prompt": redact_test_run_payload(user_prompt),
    }


def _step_result_payload(
    *,
    state: Mapping[str, Any],
    update: dict[str, Any],
    workflow_id: str,
    step: dict[str, Any],
    input_payload: dict[str, Any],
) -> dict[str, Any]:
    final_state = dict(state)
    final_state.update(update)
    latest_message = _latest_message(final_state)
    latest_artifacts = _step_test_result_artifacts(
        final_state, _as_dict((latest_message or {}).get("artifacts"))
    )
    step_kind = normalize_step_kind(step.get("step_kind"), default=STEP_KIND_AGENT)
    payload = {
        "workflow_id": workflow_id,
        "step_id": _text(step.get("step_id")),
        "step_kind": step_kind,
        "status": _text((latest_message or {}).get("status"))
        or _text(final_state.get("status"))
        or "in_progress",
        "summary": _text((latest_message or {}).get("summary")),
        "error": _text(
            (latest_message or {}).get("errors", [""])[0]
            if isinstance((latest_message or {}).get("errors"), list)
            and (latest_message or {}).get("errors")
            else final_state.get("error")
        ),
        "inputs": redact_test_run_payload(input_payload),
        "result": redact_test_run_payload(latest_artifacts),
        "state_patch": redact_test_run_payload(latest_artifacts.get("_state_patch")),
    }
    _describe_step_test_run_without_message(payload, latest_message=latest_message)
    if step_kind == STEP_KIND_AGENT:
        payload["agent"] = _resolve_agent_preview(state=final_state, step=step)
    return payload


_TERMINAL_STEP_TEST_RUN_STATUSES = frozenset({"fail", "failed", "error", "cancelled", "timeout"})


def _describe_step_test_run_without_message(
    payload: dict[str, Any],
    *,
    latest_message: Mapping[str, Any] | None,
) -> None:
    """Never hand the drawer a failure with every field empty.

    A step that was rejected before it ran produces no step message, so the payload
    was assembled from nothing: status "failed" with a blank summary, a blank error
    and an empty result. The run status is the only thing left that knows anything,
    so say that much rather than rendering a failure with no cause at all.
    """
    if latest_message is not None:
        return
    if _text(payload.get("status")).lower() not in _TERMINAL_STEP_TEST_RUN_STATUSES:
        return
    if _text(payload.get("summary")):
        return
    error = _text(payload.get("error"))
    payload["summary"] = (
        f"Step test run failed before the step produced a result: {error}"
        if error
        else "Step test run failed and the step produced no result."
    )


def _prime_ephemeral_definition(workflow: Mapping[str, Any]) -> dict[str, Any]:
    """Pin the workflow's current definition for an in-process run without a job.

    The run reads its steps through the same verified cache a durable run uses;
    the snapshot id is unique to this run and never names an archive row.
    """
    definition = frozen_definition(workflow)
    ref = definition_ref(
        f"ephemeral:{uuid.uuid4()}",
        int(workflow.get("revision") or 0),
        definition_digest(definition),
    )
    prime_run_definition(ref, definition)
    return ref


def run_step_test_run_ephemeral(
    *,
    workflow_id: str,
    workflow: Mapping[str, Any],
    step_id: str,
    project_id: str,
    task_summary: str,
    task_id: str,
    task_envelope: dict[str, Any],
    project_name: str,
    project_config: dict[str, Any],
) -> dict[str, Any]:
    _reload_registry_for_test_run()
    step = resolve_step_for_test_run(
        None, workflow_id=workflow_id, step_id=step_id, workflow=dict(workflow)
    )
    normalized_step_id = _text(step.get("step_id"))
    execution_id = uuid.uuid4().hex
    step_state = cast(
        OrchestratorState,
        _build_initial_state(
            workflow_id=workflow_id,
            project_id=project_id,
            task_summary=task_summary,
            task_source="step_test_run",
            project_name=project_name,
            project_config=project_config,
            task_id=task_id,
            task_envelope=task_envelope,
            extra_state={
                RUNTIME_SCHEMA_VERSION_KEY: RUNTIME_SCHEMA_VERSION,
                WORKFLOW_DEFINITION_REF_KEY: _prime_ephemeral_definition(workflow),
                "run_cursor": seeded_step_cursor(normalized_step_id, seed=execution_id).dump(),
                "current_step_id": normalized_step_id,
                "workflow_test_run_execution_id": execution_id,
                "step_test_run_context": {
                    "workflow_id": workflow_id,
                    "execution_id": execution_id,
                    "step_id": normalized_step_id,
                },
                "status": "in_progress",
                "test_mode": True,
            },
        ),
    )
    step_kind = normalize_step_kind(step.get("step_kind"), default=STEP_KIND_AGENT)
    executed_input_payload, preflight_failure = _preflight_step_test_run(
        state=step_state,
        step=step,
    )
    if preflight_failure:
        payload = _fail_step_test_run(
            workflow_id=workflow_id,
            step=step,
            step_kind=step_kind,
            summary=str(preflight_failure.get("summary") or "").strip()
            or "Step test run failed preflight.",
            error=str(preflight_failure.get("error") or "").strip()
            or "step_test_run_preflight_failed",
            input_payload=executed_input_payload,
            result=dict(preflight_failure.get("result") or {}),
        )
        if step_kind == STEP_KIND_AGENT:
            payload["agent"] = _resolve_agent_preview(state=step_state, step=step)
        return payload

    if is_control_flow_step_kind(step_kind):
        update = _coerce_update(control_flow_step_node(step_state))
    elif step_kind == STEP_KIND_INVOKE_WORKFLOW:
        update = _coerce_update(invoke_workflow_step_node(step_state))
    else:
        update = _coerce_update(execute_step_node(step_state))
    result = _step_result_payload(
        state=step_state,
        update=update,
        workflow_id=workflow_id,
        step=step,
        input_payload=executed_input_payload,
    )
    merged_state = dict(step_state)
    merged_state.update(update)
    merged_status = _text(merged_state.get("status"))
    result["final_status"] = (
        result["status"] if merged_status in {"", "in_progress"} else merged_status
    )
    result["final_report"] = _text(merged_state.get("final_report")) or result["summary"]
    result["workflow_outputs"] = redact_test_run_payload(
        _as_dict(merged_state.get("workflow_outputs"))
    )
    return result
