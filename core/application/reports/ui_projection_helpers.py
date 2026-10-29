"""Semantic UI projection layer for Dashboard and Job Detail.

Derives simplified, human-readable view models from the canonical
Epic 13 trace/runtime/approval data. Does NOT modify or
replace the canonical API contracts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from core.application.jobs.job_artifact_access import (
    artifact_metadata_belongs_to_job,
    artifact_metadata_is_downloadable,
)
from core.application.jobs.workflow_wait_projection import (
    STATUS_AWAITING_CHILD,
    STATUS_AWAITING_TIMER,
)
from core.application.orchestration.active_step import ActiveStep, active_step
from core.application.orchestration.helpers import resolve_step_agent
from core.application.orchestration.state_paths import StatePathError, resolve_state_path
from core.application.workflows.workflow_step_kinds import (
    CONTROL_FLOW_STEP_KINDS,
    STEP_KIND_COLLECT,
    STEP_KIND_RUNTIME_ACTOR_LABELS,
    STEP_KIND_TRANSFORM,
)

from . import ui_projection_common as _common
from . import ui_projection_costs as _costs
from . import ui_projection_review_workspace as _review

#: Pauses that are the active step still running (not a review gate).
_SELF_RESUMING_WAIT_STATUSES = frozenset({STATUS_AWAITING_TIMER, STATUS_AWAITING_CHILD})
_SPECIAL_WORDS = _common._SPECIAL_WORDS
_STEP_STATUS_META = _common._STEP_STATUS_META
NOT_RECORDED = _common.NOT_RECORDED
_AUTONOMY_GATE_SENTINEL = _common._AUTONOMY_GATE_SENTINEL
_visible_product_label = _common._visible_product_label
_normalize_dict = _common._normalize_dict
_normalize_rows = _common._normalize_rows
_normalize_role = _common._normalize_role
_as_datetime = _common._as_datetime
_format_datetime = _common._format_datetime
_format_duration_ms = _common._format_duration_ms
_format_currency = _common._format_currency
_format_cost_knownness_display = _common._format_cost_knownness_display
_format_token_count = _common._format_token_count
_truncate_text = _common._truncate_text
_step_runtime_summary = _common._step_runtime_summary
_step_runtime_result = _common._step_runtime_result
_decision_tone = _common._decision_tone
_human_readable_machine_output = _common._human_readable_machine_output
_redact_for_ui = _common._redact_for_ui
_bounded_diagnostic_value = _common._bounded_diagnostic_value
_serialize_raw = _common._serialize_raw
_humanize_identifier = _common._humanize_identifier
_status_meta = _common._status_meta
_map_step_status = _common._map_step_status
_message_summary = _common._message_summary
_message_evidence = _common._message_evidence
_message_reason = _common._message_reason
_step_scope_id = _common._step_scope_id
_approval_workspace_title = _review._approval_workspace_title
_is_autonomy_gate_payload = _review._is_autonomy_gate_payload
_parse_autonomy_gate_meta = _review._parse_autonomy_gate_meta
_autonomy_gate_title = _review._autonomy_gate_title
_override_copy = _review._override_copy
_override_supported = _review._override_supported
_autonomy_gate_intro = _review._autonomy_gate_intro
_autonomy_gate_content = _review._autonomy_gate_content
_build_review_workspace = _review._build_review_workspace
_build_cost_breakdown = _costs._build_cost_breakdown

_LOGGER = logging.getLogger("platform.reports.ui_projection_helpers")
_ARTIFACT_HANDLE_PREFIX = "artifact:"

_TECHNICAL_STEP_FIELDS = {
    "connection_ref",
    "continuation_token",
    "cursor",
    "next_cursor",
    "page_token",
}


def _first_nonempty_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        normalized = _normalize_dict(value)
        if normalized:
            return normalized
    return {}


def _recorded_collect_inputs(artifacts: dict[str, Any]) -> dict[str, Any]:
    """Keep the one recorded contribution in the public dictionary IO shape."""
    if artifacts.get("step_kind") != STEP_KIND_COLLECT or "input_payload" not in artifacts:
        return {}
    payload = artifacts["input_payload"]
    # A present null/empty collection is a contribution, not absent history.
    return dict(payload) if isinstance(payload, dict) and payload else {"value": payload}


#: A step's inputs and outputs are already cut to a reading size when recorded
#: (``step_io_preview``); cutting every list to two items again here turned
#: 25 rows into "11 items" and [10, 10, 5] into two numbers.
_STEP_IO_MAX_ITEMS = 26
_BUILTIN_STEP_LOCALIZED_FIELDS = frozenset(
    {"rules", "value", "source", "result_target", "step_id", "step_kind"}
)


def _builtin_step_field_label_keys(
    artifacts: dict[str, Any], payload_key: str = ""
) -> dict[str, str]:
    step_kind = str(artifacts.get("step_kind") or "").strip()
    if step_kind != STEP_KIND_TRANSFORM and step_kind not in CONTROL_FLOW_STEP_KINDS:
        return {}
    # Only the first-party runner's IO snapshot is a known schema. Fallback
    # message inputs and state patches may contain arbitrary owner/provider keys.
    payload = _normalize_dict(artifacts.get(payload_key)) if payload_key else artifacts
    if step_kind == STEP_KIND_COLLECT and payload_key == "input_payload":
        payload = _recorded_collect_inputs(artifacts)
    return {
        field: f"react.jobs.field_{field}"
        for field in payload
        if field in _BUILTIN_STEP_LOCALIZED_FIELDS
    }


def _redacted_debug_preview(value: Any, *, max_items: int | None = None) -> Any:
    from core.application.workflows.workflow_start_runtime import (
        _sanitize_invocation_value,
    )

    sanitized, _ = _sanitize_invocation_value(value)
    if max_items is None:
        return _bounded_diagnostic_value(sanitized)
    return _bounded_diagnostic_value(sanitized, max_items=max_items)


def _artifact_id_from_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized.startswith(_ARTIFACT_HANDLE_PREFIX):
        return ""
    return normalized.removeprefix(_ARTIFACT_HANDLE_PREFIX).strip()


def _artifact_ids(value: Any) -> list[str]:
    resolved: list[str] = []

    def _visit(candidate: Any) -> None:
        artifact_id = _artifact_id_from_value(candidate)
        if artifact_id:
            if artifact_id not in resolved:
                resolved.append(artifact_id)
            return
        if isinstance(candidate, dict):
            direct_artifact_id = str(candidate.get("artifact_id") or "").strip()
            if direct_artifact_id and direct_artifact_id not in resolved:
                resolved.append(direct_artifact_id)
            for nested in candidate.values():
                _visit(nested)
        elif isinstance(candidate, (list, tuple)):
            for nested in candidate:
                _visit(nested)

    _visit(value)
    return resolved


def _display_debug_value(value: Any, *, preserve_empty_items: bool = False) -> Any:
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, nested in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in _TECHNICAL_STEP_FIELDS or normalized_key.endswith("_cursor"):
                continue
            if _artifact_id_from_value(nested):
                continue
            normalized = _display_debug_value(nested, preserve_empty_items=preserve_empty_items)
            # An empty result is itself the answer ("the filter kept nothing");
            # only a value emptied by leaving out technical fields is dropped.
            if normalized not in ({}, []) or nested in ({}, []):
                projected[str(key)] = normalized
        return projected
    if isinstance(value, list):
        return [
            normalized
            for item in value
            if not _artifact_id_from_value(item)
            for normalized in [
                _display_debug_value(item, preserve_empty_items=preserve_empty_items)
            ]
            if normalized not in ({}, []) or (preserve_empty_items and item in ({}, []))
        ]
    return value


def _display_step_outputs(outputs: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    """Keep an inline HTTP body literal even when its text resembles a file handle."""
    display = _display_debug_value(outputs)
    http_request = _normalize_dict(artifacts.get("http_request"))
    response_mode = str(http_request.get("response_mode") or "inline").strip().lower()
    if (
        artifacts.get("step_kind") == "http_request"
        and response_mode == "inline"
        and "body" in outputs
    ):
        display["body"] = outputs["body"]
    return display


def _artifact_discovery_outputs(
    outputs: dict[str, Any], artifacts: dict[str, Any]
) -> dict[str, Any]:
    """Do not interpret literal inline HTTP body text as an artifact handle."""
    http_request = _normalize_dict(artifacts.get("http_request"))
    response_mode = str(http_request.get("response_mode") or "inline").strip().lower()
    if artifacts.get("step_kind") != "http_request" or response_mode != "inline":
        return outputs
    return {key: value for key, value in outputs.items() if key != "body"}


def _http_saved_output(message: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    """Project the value an HTTP step wrote, separately from its diagnostic envelope."""
    if artifacts.get("step_kind") != "http_request" or message.get("status") != "pass":
        return {}
    target = str(artifacts.get("response_target") or "").strip()
    if not target.startswith("state.") or target == "state.":
        return {}
    state_patch = artifacts.get("_state_patch")
    if not isinstance(state_patch, dict):
        return {}
    try:
        value, found = resolve_state_path(state_patch, target)
    except StatePathError:
        return {}
    if not found:
        return {}

    http_request = _normalize_dict(artifacts.get("http_request"))
    response_mode = str(http_request.get("response_mode") or "inline").strip().lower()
    is_file = response_mode == "artifact" and bool(_artifact_id_from_value(value))
    return {
        "target": _redacted_debug_preview(target),
        # Keep artifact handles and inline text that happens to start with
        # "artifact:" distinct; _display_debug_value intentionally filters
        # file handles from the generic diagnostics view.
        "value": _redacted_debug_preview(value),
        "is_file": is_file,
    }


def _artifact_metadata(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, dict) else {}
    if hasattr(value, "dump"):
        dumped = value.dump()
        return dict(dumped) if isinstance(dumped, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _artifact_descriptors(
    value: Any,
    *,
    role: str,
    project_id: str,
    job_id: str,
    resolve_artifact: Callable[[str], Any] | None,
) -> list[dict[str, Any]]:
    if not callable(resolve_artifact) or not project_id or not job_id:
        return []
    descriptors: list[dict[str, Any]] = []
    for artifact_id in _artifact_ids(value):
        try:
            metadata = _artifact_metadata(resolve_artifact(artifact_id))
        except Exception:
            metadata = {}
        if not artifact_metadata_belongs_to_job(
            metadata,
            project_id=project_id,
            job_id=job_id,
        ):
            continue
        available = artifact_metadata_is_downloadable(metadata)
        from core.application.extensions.artifact_presentation import artifact_presentation_view

        metadata = artifact_presentation_view(metadata, resolve_artifact=resolve_artifact)
        descriptors.append(
            {
                "artifact_id": artifact_id,
                "role": role,
                "filename": str(metadata.get("filename") or "Artifact").strip() or "Artifact",
                "content_type": str(
                    metadata.get("content_type") or "application/octet-stream"
                ).strip(),
                "size_bytes": max(0, int(metadata.get("size_bytes") or 0)),
                "available": available,
                "download_url": (
                    f"/api/jobs/{job_id}/artifacts/{artifact_id}" if available else None
                ),
            }
        )
    return descriptors


def _step_duration_ms(*, started_at: Any, finished_at: Any, explicit_ms: Any) -> int | None:
    if explicit_ms not in (None, "") and not isinstance(explicit_ms, bool):
        try:
            return max(0, int(float(explicit_ms)))
        except (TypeError, ValueError):
            pass
    started = _as_datetime(started_at)
    finished = _as_datetime(finished_at)
    if started is None or finished is None or started == finished:
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _tabular_feed_outputs(artifacts: dict[str, Any]) -> dict[str, Any]:
    """Read the runtime's compact result, without scanning or copying workflow state."""
    result = _normalize_dict(artifacts.get("tabular_feed"))
    if artifacts.get("step_kind") != "tabular_feed" or result.get("ok") is not True:
        return {}
    outputs = {
        key: result[key]
        for key in ("dataset_handle", "row_count", "result_count")
        if key in result and isinstance(result[key], str | int | float | bool)
    }
    handle = result.get("artifact_handle")
    if isinstance(handle, str) and handle.strip():
        # Core tabular writes return a bare ID. Normalize only this known field
        # in the report copy; leave the canonical runtime/mapping value intact.
        artifact_id = handle.strip().removeprefix(_ARTIFACT_HANDLE_PREFIX)
        if artifact_id:
            outputs["artifact_handle"] = f"{_ARTIFACT_HANDLE_PREFIX}{artifact_id}"
    return outputs


#: Artifact keys that are bookkeeping, inputs or file references, not an answer.
_NON_ANSWER_ARTIFACT_KEYS = frozenset(
    {
        "raw_output",
        "input_payload",
        "inputs",
        "connector_invocation",
        "tabular_feed",
        "step_kind",
        "step_id",
        "step_display_name",
        "operation_id",
        "operation_deadline_epoch_seconds",
        "error",
    }
)


def _agent_answer_outputs(artifacts: dict[str, Any]) -> dict[str, Any]:
    """The answer keys an agent step recorded, the same ones its evidence shows."""
    if str(artifacts.get("step_kind") or "agent").strip() != "agent":
        # Any other step kind states its outputs; its remaining artifacts are
        # bookkeeping (the failure, the deadline), not an answer.
        return {}
    return {
        str(key): value
        for key, value in artifacts.items()
        if not str(key).startswith("_")
        and str(key) not in _NON_ANSWER_ARTIFACT_KEYS
        and "artifact" not in str(key).lower()
        and value not in (None, "", {}, [])
    }


def _message_step_debug_fields(
    message: dict[str, Any],
    *,
    project_id: str = "",
    job_id: str = "",
    resolve_artifact: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    artifacts = _normalize_dict(message.get("artifacts"))
    envelope = _normalize_dict(message.get("envelope"))
    timestamps = _first_nonempty_dict(
        message.get("timestamps"),
        envelope.get("timestamps"),
    )
    connector_invocation = _normalize_dict(artifacts.get("connector_invocation"))
    connector_response = _normalize_dict(connector_invocation.get("response"))
    tabular_outputs = _tabular_feed_outputs(artifacts)

    inputs = _first_nonempty_dict(
        _recorded_collect_inputs(artifacts),
        artifacts.get("input_payload"),
        artifacts.get("inputs"),
        message.get("inputs"),
        envelope.get("inputs"),
    )
    outputs = _first_nonempty_dict(
        connector_response.get("result"),
        artifacts.get("output_payload"),
        artifacts.get("outputs"),
        artifacts.get("_state_patch"),
        message.get("outputs"),
        envelope.get("outputs"),
        tabular_outputs,
        # An agent step stores its answer as artifacts under its output keys
        # (a plain-text reply lands under the skill's primary key, e.g.
        # ``output``). The evidence cards read exactly those keys, so without
        # this the panel showed the answer as evidence next to "No step outputs".
        _agent_answer_outputs(artifacts),
    )
    if tabular_outputs and isinstance(artifacts.get("output_payload"), dict):
        # A tabular step's own summary (row count, handles) and its preview
        # (columns, first rows) belong together, not one instead of the other.
        outputs = {**tabular_outputs, **outputs}
    projected_artifacts = {
        str(key): value
        for key, value in artifacts.items()
        if not str(key).startswith("_") and "artifact" in str(key).lower()
    }

    started_at = str(timestamps.get("started_at") or message.get("started_at") or "").strip()
    finished_at = str(
        timestamps.get("completed_at")
        or timestamps.get("finished_at")
        or message.get("finished_at")
        or message.get("timestamp")
        or ""
    ).strip()
    duration_ms = _step_duration_ms(
        started_at=started_at,
        finished_at=finished_at,
        explicit_ms=message.get("duration_ms") or envelope.get("duration_ms"),
    )

    input_artifacts = _artifact_descriptors(
        inputs,
        role="input",
        project_id=project_id,
        job_id=job_id,
        resolve_artifact=resolve_artifact,
    )
    output_artifacts = _artifact_descriptors(
        {
            "outputs": _artifact_discovery_outputs(outputs, artifacts),
            "tabular_feed": tabular_outputs,
        },
        role="output",
        project_id=project_id,
        job_id=job_id,
        resolve_artifact=resolve_artifact,
    )
    provider_processing_time = outputs.get("processing_time")
    saved_output = _http_saved_output(message, artifacts)
    provider_processing_time_unit = outputs.get("processing_time_unit")

    errors = message.get("errors")
    first_error = errors[0] if isinstance(errors, list) and errors else None
    error = _human_readable_machine_output(first_error, limit=600)

    return {
        "input_field_label_keys": _builtin_step_field_label_keys(artifacts, "input_payload"),
        "output_field_label_keys": _builtin_step_field_label_keys(artifacts, "output_payload"),
        "inputs": (
            _redacted_debug_preview(
                _display_debug_value(
                    inputs,
                    preserve_empty_items=artifacts.get("step_kind") == STEP_KIND_COLLECT
                    and "input_payload" in artifacts,
                ),
                max_items=_STEP_IO_MAX_ITEMS,
            )
            if inputs
            else {}
        ),
        "outputs": (
            _redacted_debug_preview(
                _display_step_outputs(outputs, artifacts), max_items=_STEP_IO_MAX_ITEMS
            )
            if outputs
            else {}
        ),
        "artifacts": (_redacted_debug_preview(projected_artifacts) if projected_artifacts else {}),
        **({"saved_output": saved_output} if saved_output else {}),
        "input_artifacts": input_artifacts,
        "output_artifacts": output_artifacts,
        "error": error,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "provider_processing_time": provider_processing_time,
        "provider_processing_time_unit": (
            str(provider_processing_time_unit).strip() or None
            if provider_processing_time_unit is not None
            else None
        ),
        "model": str(message.get("model") or envelope.get("model") or "").strip(),
        "attempt": message.get("attempt") or envelope.get("attempt"),
    }


def _build_message_steps(
    messages: list[dict[str, Any]],
    *,
    project_id: str = "",
    job_id: str = "",
    resolve_artifact: Callable[[str], Any] | None = None,
) -> list[dict[str, Any]]:
    def _sort_key(message: dict[str, Any]) -> tuple[str, str]:
        timestamp = str(message.get("timestamp") or "")
        message_id = str(message.get("id") or "").strip()
        return (timestamp, message_id)

    steps: list[dict[str, Any]] = []
    for index, message in enumerate(sorted(messages, key=_sort_key)):
        debug_fields = _message_step_debug_fields(
            message,
            project_id=project_id,
            job_id=job_id,
            resolve_artifact=resolve_artifact,
        )
        artifacts = _normalize_dict(message.get("artifacts"))
        connector_invocation = _normalize_dict(artifacts.get("connector_invocation"))
        envelope = _normalize_dict(message.get("envelope"))
        operation_id = str(
            artifacts.get("action_id")
            or connector_invocation.get("operation_id")
            or artifacts.get("operation_id")
            or envelope.get("operation_id")
            or artifacts.get("function_name")
            or artifacts.get("tool_id")
            or ""
        ).strip()
        message_step_id = str(message.get("step_id") or artifacts.get("step_id") or "").strip()
        group_id = str(message.get("group_id") or "").strip()
        for_each_id = str(message.get("for_each_id") or envelope.get("for_each_id") or "").strip()
        item_index = _item_index(envelope.get("item_index", message.get("item_index")))
        agent = str(message.get("from_agent") or "").strip()
        skill = str(message.get("skill") or "").strip()
        # A step kind's own runtime actor is not an agent: name the kind of
        # step instead, and the extension it called as its tool.
        step_type_label = STEP_KIND_RUNTIME_ACTOR_LABELS.get(agent, "")
        step_type_keys = {
            "connector": "react.workflows.tool_action",
            "http_request": "react.workflows.http_request",
            "dataset_query": "react.workflows.dataset_query",
            "transform": "react.workflows.transform",
            "tabular_feed": "react.workflows.tabular_feed",
            "file_from_base64": "react.workflows.save_file_from_base64",
            "template": "react.workflows.template",
            "invoke_workflow": "react.workflows.invoke_workflow",
            "if": "react.workflows.if",
            "switch": "react.workflows.switch",
            "stop_success": "react.workflows.finish_successfully",
            "stop_fail": "react.workflows.finish_with_failure",
            "set_variable": "react.workflows.set_variable",
            "collect": "react.workflows.step_kind_collect",
            "break": "react.workflows.step_kind_break",
            "continue": "react.workflows.step_kind_continue",
            "batch": "react.workflows.batch",
        }
        step_type_key = (
            step_type_keys.get(str(artifacts.get("step_kind") or ""), "") if step_type_label else ""
        )
        tool_id = str(artifacts.get("extension_id") or artifacts.get("connector_id") or "").strip()
        message_id = str(message.get("id") or "").strip()
        scope_id = _step_scope_id(
            step_id=message_step_id,
            agent=agent,
            skill=skill,
            fallback=f"message-{message.get('id') or index}",
        )
        step_id = f"message-{message_id}" if message_id else f"{scope_id or 'message'}:{index}"
        step_status = _map_step_status(message.get("status"))
        group_label = _humanize_identifier(group_id)
        # A step in a For each body runs once per item: the row names the item.
        for_each_label = _humanize_identifier(for_each_id)
        item_number = item_index + 1 if for_each_id and item_index is not None else None
        container_label = (
            f"{for_each_label} #{item_number}" if for_each_label and item_number else for_each_label
        ) or group_label
        skill_label = _humanize_identifier(skill) or _humanize_identifier(agent) or "Workflow step"
        # The name the author gave the step says more than its kind; without
        # one, the step id tells two set-variable steps apart.
        display_name = str(artifacts.get("step_display_name") or "").strip()
        title = _step_row_title(
            name=display_name or skill_label,
            step_label="" if display_name else _humanize_identifier(message_step_id),
            group_label=container_label,
        )
        child_workflow = str(artifacts.get("child_workflow") or "").strip()
        if child_workflow:
            title = f"{child_workflow} / {title}"
        steps.append(
            {
                "id": step_id,
                "scope_id": scope_id or f"message-{index}",
                "step_id": message_step_id or None,
                "step_execution_id": str(message.get("step_execution_id") or "").strip() or None,
                "group_id": group_id or None,
                "group_label": group_label,
                "for_each_id": for_each_id or None,
                "for_each_label": for_each_label,
                "item_number": item_number,
                "agent": agent,
                "agent_label": "" if step_type_label else (_humanize_identifier(agent) or agent),
                "skill": skill,
                "skill_label": skill_label,
                "step_type_label": step_type_label,
                "step_type_label_key": step_type_key,
                "tool_label": tool_id,
                "operation_id": operation_id or None,
                "operation_label": _operation_label(operation_id),
                "title": title,
                "status": step_status,
                "status_label": _STEP_STATUS_META.get(step_status, _STEP_STATUS_META["completed"])[
                    "label"
                ],
                "tone": _STEP_STATUS_META.get(step_status, _STEP_STATUS_META["completed"])["tone"],
                "summary": _message_summary(message),
                "summary_key": str(artifacts.get("summary_key") or "") if step_type_label else "",
                "summary_params": _normalize_dict(artifacts.get("summary_params"))
                if step_type_label
                else {},
                "reason": _message_reason(message),
                # No canned "result" or "next" text: it read the same on every
                # step and told the user nothing. The status, summary, outputs
                # and errors say what happened.
                "result": "",
                "next": "",
                "timestamp": str(message.get("timestamp") or ""),
                "message_id": message_id or None,
                "evidence": _message_evidence(
                    message, field_label_keys=_builtin_step_field_label_keys(artifacts)
                ),
                "source": "message",
                "selected": False,
                "can_restart_from_here": False,
                **debug_fields,
            }
        )
    return steps


def _item_index(value: Any) -> int | None:
    """A message's 0-based For each item position, or None when it has none."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _step_row_title(*, name: str, step_label: str = "", group_label: str = "") -> str:
    """A step row's title: its name, led by its step id and the group it runs in.

    "Geocode: Http Request" for a step known only by its kind, "Batches: Push"
    inside a group, but never "Start: Start workflow".
    """
    title = name
    if step_label and step_label.lower() not in name.lower():
        title = f"{step_label}: {name}"
    if group_label and not title.lower().startswith(group_label.lower()):
        title = f"{group_label}: {title}"
    return title


def _operation_label(operation_id: str) -> str | None:
    if not operation_id:
        return None
    if " " in operation_id or "://" in operation_id:
        # Already readable, such as "GET https://host/path": splitting on dots
        # would keep only the tail of the host name.
        return operation_id.replace("https://", "").replace("http://", "")
    leaf = operation_id.rsplit(".", 1)[-1]
    return _humanize_identifier(leaf) or None


def _thread_active_step(thread_state: dict[str, Any]) -> ActiveStep | None:
    """The step the run cursor points at, resolved in the run's pinned definition.

    A read model never fails the page over it: a cursor whose definition cannot
    be loaded shows no active step.
    """
    try:
        return active_step(thread_state)
    except Exception as exc:
        _LOGGER.warning(
            "ui_projection: active step of job %s unavailable: %s",
            str(thread_state.get("job_id") or "").strip() or "unknown",
            exc,
        )
        return None


def _build_runtime_steps(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any],
    thread_state: dict[str, Any],
    seen_scope_ids: set[str],
) -> list[dict[str, Any]]:
    """The step in flight, when no recorded message or run row shows it yet.

    The cursor names one step at a time; the graph decides what follows, so no
    later step is listed as planned.
    """
    job_status = str(job.get("status") or "").strip().lower()
    retryable = _normalize_dict(runtime.get("retryable_interrupted_step"))
    retry_scope = _step_scope_id(
        step_id=retryable.get("step_id"),
        agent=retryable.get("agent_id"),
        skill=retryable.get("skill"),
    )
    current = _thread_active_step(thread_state)
    if current is None or current.step_id in seen_scope_ids:
        return []
    # Only the step's own fields: a read model does not consult the registry
    # for an agent's primary skill.
    agent = resolve_step_agent(current.step, None, state=thread_state)
    skill = str(current.step.get("skill") or current.step.get("skill_id") or "").strip()
    if (
        job_status in {"retryable_interrupted_step", "interrupted", "failed", "stuck", "orphaned"}
        and bool(runtime.get("can_retry_interrupted_step"))
        and bool(retry_scope)
        and retry_scope in {current.step_id, skill}
    ):
        step_status = "interrupted"
    elif job_status == "awaiting_approval":
        step_status = "awaiting_review"
    elif job_status in {"in_progress", "resume_pending", "pending", *_SELF_RESUMING_WAIT_STATUSES}:
        # A Wait or child invocation is the step still running, only paused.
        step_status = "in_progress"
    else:
        # A finished run keeps its last cursor; the recorded rows say how that
        # step ended.
        return []
    display_name = str(current.step.get("display_name") or "").strip()
    step_label = _humanize_identifier(current.step_id)
    skill_label = (
        _humanize_identifier(display_name or skill or agent) or step_label or "Workflow step"
    )
    group_label = _humanize_identifier(current.group_id)
    return [
        {
            "id": current.step_id,
            "scope_id": current.step_id,
            "step_id": current.step_id,
            "step_execution_id": current.step_execution_id,
            "group_id": current.group_id,
            "group_label": group_label,
            "agent": agent,
            "agent_label": _humanize_identifier(agent) or agent,
            "skill": skill,
            "skill_label": skill_label,
            "title": _step_row_title(
                name=skill_label,
                step_label="" if display_name else step_label,
                group_label=group_label,
            ),
            "status": step_status,
            "status_label": _STEP_STATUS_META[step_status]["label"],
            "tone": _STEP_STATUS_META[step_status]["tone"],
            "summary": _step_runtime_summary(step_status),
            "reason": str(runtime.get("wait_reason") or "").strip(),
            "result": _step_runtime_result(step_status),
            "next": (
                "A human decision is required before Flow Steward can continue."
                if step_status == "awaiting_review"
                else "Flow Steward will continue to the next step when this one finishes."
            ),
            "timestamp": "",
            "message_id": None,
            "evidence": [],
            "source": "runtime",
            "raw": _bounded_diagnostic_value(current.step),
            "selected": False,
            "can_restart_from_here": False,
            "attempt": current.attempt,
        }
    ]


def _build_retry_step(
    *,
    runtime: dict[str, Any],
    seen_scope_ids: set[str],
) -> list[dict[str, Any]]:
    snapshot = _normalize_dict(runtime.get("retryable_interrupted_step"))
    if not snapshot:
        return []
    scope_id = _step_scope_id(
        step_id=snapshot.get("step_id"),
        agent=snapshot.get("agent_id"),
        skill=snapshot.get("skill"),
        fallback="retryable-step",
    )
    if not scope_id or scope_id in seen_scope_ids:
        return []
    snapshot_step_id = str(snapshot.get("step_id") or "").strip()
    step_label = _humanize_identifier(snapshot_step_id)
    skill_label = (
        _humanize_identifier(snapshot.get("skill"))
        or _humanize_identifier(snapshot.get("agent_id"))
        or "Interrupted step"
    )
    return [
        {
            "id": scope_id,
            "scope_id": scope_id,
            "step_id": snapshot_step_id or None,
            "step_execution_id": str(snapshot.get("step_execution_id") or "").strip() or None,
            "agent": str(snapshot.get("agent_id") or "").strip(),
            "agent_label": _humanize_identifier(snapshot.get("agent_id"))
            or str(snapshot.get("agent_id") or "").strip(),
            "skill": str(snapshot.get("skill") or "").strip(),
            "skill_label": skill_label,
            "title": _step_row_title(name=skill_label, step_label=step_label),
            "status": "interrupted",
            "status_label": _STEP_STATUS_META["interrupted"]["label"],
            "tone": _STEP_STATUS_META["interrupted"]["tone"],
            "summary": "Flow Steward stopped while this step was in flight.",
            "reason": "The run captured a retryable snapshot for this exact step.",
            "result": "You can retry from this step without restarting the entire job.",
            "next": "Use the step-level retry action to continue from the interrupted point.",
            "timestamp": str(snapshot.get("detected_at") or ""),
            "message_id": snapshot.get("message_id"),
            "evidence": [
                {
                    "title": "Retryable snapshot",
                    "summary": _truncate_text(
                        f"Agent: {snapshot.get('agent_id') or 'unknown'} | Skill: {snapshot.get('skill') or 'unknown'} | Step: {step_label or 'unknown'}",
                        limit=280,
                    ),
                    "meta": "Recovery snapshot",
                    "raw": _serialize_raw(snapshot),
                    "tone": "amber",
                }
            ],
            "source": "retryable_snapshot",
            "raw": _bounded_diagnostic_value(snapshot),
            "selected": False,
            "can_restart_from_here": False,
        }
    ]


def _select_step(
    *,
    steps: list[dict[str, Any]],
    runtime: dict[str, Any],
) -> dict[str, Any] | None:
    if not steps:
        return None
    retryable = _normalize_dict(runtime.get("retryable_interrupted_step"))
    retry_scope = _step_scope_id(
        step_id=retryable.get("step_id"),
        agent=retryable.get("agent_id"),
        skill=retryable.get("skill"),
    )
    current_scope = str(runtime.get("current_step_id") or "").strip()
    preferred_scopes = [scope for scope in (retry_scope, current_scope) if scope]
    for preferred in preferred_scopes:
        for step in reversed(steps):
            if preferred in {
                str(step.get("id") or ""),
                str(step.get("scope_id") or ""),
                str(step.get("skill") or ""),
            }:
                return step
    for desired_status in ("interrupted", "failed", "awaiting_review", "in_progress"):
        for step in reversed(steps):
            if step.get("status") == desired_status:
                return step
    return steps[-1]


def _selected_step_payload(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any],
    selected_step: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(selected_step, dict):
        return None
    payload = dict(selected_step)
    payload["action"] = None
    retryable = _normalize_dict(runtime.get("retryable_interrupted_step"))
    retry_scope = _step_scope_id(
        step_id=retryable.get("step_id"),
        agent=retryable.get("agent_id"),
        skill=retryable.get("skill"),
    )
    retryable_statuses = {
        "retryable_interrupted_step",
        "interrupted",
        "failed",
        "stuck",
        "orphaned",
    }
    if (
        str(job.get("status") or "").strip().lower() in retryable_statuses
        and bool(runtime.get("can_retry_interrupted_step"))
        and str(job.get("id") or "").strip()
        and retry_scope
        and retry_scope
        in {
            str(payload.get("id") or ""),
            str(payload.get("scope_id") or ""),
            str(payload.get("skill") or ""),
        }
    ):
        payload["can_restart_from_here"] = True
        payload["action"] = {
            "kind": "restart_from_step",
            "label": "Restart from this step",
            "method": "post",
            "endpoint": f"/api/jobs/{str(job.get('id') or '').strip()}/retry-step",
            "confirm": "Retry this interrupted step?",
        }
    return payload
