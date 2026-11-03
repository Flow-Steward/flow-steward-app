"""Durable submission boundary for workflow-editor test runs."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Literal

TestRunKind = Literal["workflow", "step"]


class TestRunInputRejected(ValueError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("Workflow test-run inputs are invalid")
        self.errors = errors


class TestRunReadinessRejected(ValueError):
    def __init__(self, validation: dict[str, Any]) -> None:
        super().__init__("Selected step is not ready for a test run")
        self.validation = validation


def _validated_step_values(
    envelope: Mapping[str, Any], preview: Mapping[str, Any] | None
) -> dict[str, Any]:
    names = {
        str(row.get("name") or "").strip()
        for group in ("locked_required", "unresolved_required", "optional", "custom")
        for row in (preview or {}).get(group, [])
        if isinstance(row, Mapping)
    }
    metadata = envelope.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    values = {
        **dict(envelope.get("inputs") or {}),
        **dict(metadata.get("input_binding_values") or {}),
    }
    return {name: values[name] for name in names if name in values}


def _validate_supplied_step_readiness(
    validation: Mapping[str, Any], step_id: str, values: Mapping[str, Any]
) -> None:
    def supplied(issue: Mapping[str, Any]) -> bool:
        name = str(issue.get("input_name") or "")
        return (
            issue.get("code") == "workflow.step_io.error"
            and issue.get("issue_type") == "step_required_input_unavailable"
            and issue.get("step_id") == step_id
            and name in values
            and not _missing(values[name])
        )

    original_errors = validation.get("errors") or []
    errors = [dict(issue) for issue in original_errors if not supplied(issue)]
    # Only identified, satisfied input issues can explain an invalid report.
    if errors or (not validation.get("valid", False) and not original_errors):
        raise TestRunReadinessRejected({**dict(validation), "valid": False, "errors": errors})


def _text(value: object) -> str:
    return str(value or "").strip()


def _bounded_caller_meta(caller: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(caller or {})
    result: dict[str, Any] = {}
    for key in (
        "actor_id",
        "principal_type",
        "correlation_id",
        "idempotency_key",
    ):
        value = _text(source.get(key))
        if value:
            result[key] = value[:512]
    for key in ("allowed_project_ids", "allowed_workflow_ids"):
        raw = source.get(key)
        if isinstance(raw, (list, tuple, set, frozenset)):
            result[key] = sorted({_text(item) for item in raw if _text(item)})
    return result


def _missing(value: Any) -> bool:
    return value is None or value == "" or value == {} or value == []


def _substitute_sample_files(
    interface: Any,
    inputs: dict[str, Any],
    sample_files: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fill each file input the test run did not pass with its attached test file."""
    substituted: list[dict[str, Any]] = []
    for row in getattr(interface, "expected_inputs", None) or []:
        sample = getattr(row, "sample_file", None)
        if row.type != "file" or not isinstance(sample, dict) or not _missing(inputs.get(row.name)):
            continue
        ref = sample_files(row.name, dict(sample))
        inputs[row.name] = ref
        substituted.append(dict(ref))
    return substituted


def materialize_test_run_envelope(
    *,
    workflow_id: str,
    project_id: str,
    task_envelope: Mapping[str, Any],
    workflow_interface: Any = None,
    workflow_interface_service: Any = None,
    sample_files: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    step_run: bool = False,
    step_contract_preview: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate and replace any plaintext secret inputs before durable use.

    ``sample_files`` supplies the attached test file of each file input the run
    did not pass; the files it returns are also listed under ``files``.

    A step run tests one step in isolation: the values typed for the step's own
    inputs go straight to the state paths they are bound to (kept under
    ``metadata.input_binding_values``) instead of being judged as workflow
    inputs, and the workflow's required inputs are not demanded of it.
    """
    if workflow_interface_service is None:
        from core.application.registry.workflow_contract_service import WorkflowInterfaceService

        workflow_interface_service = WorkflowInterfaceService()
    envelope = copy.deepcopy(dict(task_envelope))
    raw_inputs = envelope.get("inputs")
    normalized_inputs = dict(raw_inputs) if isinstance(raw_inputs, Mapping) else {}
    interface = workflow_interface or workflow_interface_service.interface_for_workflow_id(
        _text(workflow_id)
    )
    if step_run:
        from core.application.workflows.test_run_input_bindings import materialize_step_input_values

        errors = materialize_step_input_values(
            envelope, normalized_inputs, interface, step_contract_preview
        )
        if errors:
            raise TestRunInputRejected(errors)
    if sample_files is not None and bool(interface.enabled):
        substituted = _substitute_sample_files(interface, normalized_inputs, sample_files)
        if substituted:
            existing_files = envelope.get("files")
            envelope["files"] = [
                *(existing_files if isinstance(existing_files, list) else []),
                *substituted,
            ]
    validation = workflow_interface_service.validate_inputs(
        interface, normalized_inputs, channel=""
    )
    errors = [dict(row) for row in (validation.errors or []) if isinstance(row, Mapping)]
    if step_run:
        errors = [row for row in errors if row.get("code") != "workflow.input.required"]
    if errors:
        raise TestRunInputRejected(errors)
    if bool(interface.enabled):
        normalized_inputs = dict(validation.inputs)
    normalized_inputs.setdefault("project_id", _text(project_id))
    envelope["inputs"] = normalized_inputs
    from core.application.registry.workflow_secret_defaults import workflow_secret_launch_snapshot

    snapshot_inputs = {"inputs": normalized_inputs}
    if step_run:
        snapshot_inputs["bindings"] = (envelope.get("metadata") or {}).get(
            "input_binding_values", {}
        )
    return envelope, workflow_secret_launch_snapshot(interface, inputs=snapshot_inputs)


def submit_test_run(
    *,
    enqueue: Callable[..., Any],
    job_store: Any,
    kind: TestRunKind,
    workflow_id: str,
    project_id: str,
    task_id: str,
    task_summary: str,
    task_envelope: Mapping[str, Any],
    project_name: str,
    project_config: Mapping[str, Any],
    mode: str = "",
    step: Mapping[str, Any] | None = None,
    caller: Mapping[str, Any] | None = None,
    workflow_interface: Any = None,
    workflow_interface_service: Any = None,
    sample_files: Any = None,
    expected_revision: int | None = None,
    step_contract_preview: Mapping[str, Any] | None = None,
    kept_test_outputs: Mapping[str, Mapping[str, Any]] | None = None,
    step_validation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit a test-run job/outbox and return its durable public identity.

    ``expected_revision`` is the revision the caller loaded; the job is
    refused in its creating transaction when the workflow moved on.

    ``sample_files`` (a ``TestRunSampleFiles``) copies the attached test file of
    each file input the run did not pass; the copies are bound to the job, or
    deleted when the run is not launched.

    ``kept_test_outputs`` (step id → output target → value) is the snapshot of
    kept test outputs a workflow run returns instead of running those steps; it
    rides in trusted job meta and a step run never takes it.

    A step run starts its cursor at the selected step of the job's pinned
    definition; the job pins the definition because ``workflow_id`` is its
    selected workflow, and the cursor's execution id is derived from the run's
    execution id so the worker restoring the job names the same execution.
    """

    normalized_kind = _text(kind).lower()
    if normalized_kind not in {"workflow", "step"}:
        raise ValueError("test-run kind must be 'workflow' or 'step'")
    normalized_workflow_id = _text(workflow_id)
    normalized_project_id = _text(project_id)
    normalized_task_id = _text(task_id) or uuid.uuid4().hex
    step_payload = dict(step or {})
    normalized_step_id = _text(step_payload.get("step_id"))
    if normalized_kind == "step" and not normalized_step_id:
        raise ValueError("step test runs require a step")

    task_source = f"{normalized_kind}_test_run"
    execution_id = uuid.uuid4().hex
    envelope = copy.deepcopy(dict(task_envelope))
    metadata = envelope.get("metadata")
    normalized_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    normalized_metadata["source"] = task_source
    normalized_metadata.setdefault("requested_workflow_id", normalized_workflow_id)
    if normalized_kind == "step":
        normalized_metadata.setdefault("step_id", normalized_step_id)
    envelope["metadata"] = normalized_metadata
    try:
        envelope, secret_snapshot = materialize_test_run_envelope(
            workflow_id=normalized_workflow_id,
            project_id=normalized_project_id,
            task_envelope=envelope,
            workflow_interface=workflow_interface,
            workflow_interface_service=workflow_interface_service,
            sample_files=sample_files.copy if sample_files is not None else None,
            step_run=normalized_kind == "step",
            step_contract_preview=step_contract_preview,
        )
        step_values = (
            _validated_step_values(envelope, step_contract_preview)
            if normalized_kind == "step"
            else {}
        )
        if normalized_kind == "step" and step_validation is not None:
            _validate_supplied_step_readiness(step_validation, normalized_step_id, step_values)
    except BaseException:
        if sample_files is not None:
            sample_files.discard()
        raise
    normalized_inputs = dict(envelope.get("inputs") or {})

    extra_state: dict[str, Any] = {
        "workflow_id": normalized_workflow_id,
        "requested_workflow_id": normalized_workflow_id,
        "task_envelope": envelope,
        "inputs": dict(normalized_inputs),
        "materialized_inputs": {**normalized_inputs, **step_values},
        "debug_test_run": True,
        "test_mode": True,
        "workflow_test_run_execution_id": execution_id,
        "_workflow_secret_ref_fingerprints": secret_snapshot,
    }
    if isinstance(envelope.get("files"), list) and envelope["files"]:
        extra_state["files"] = list(envelope["files"])
    normalized_mode = _text(mode)
    if normalized_mode:
        extra_state["mode"] = normalized_mode
    if normalized_kind == "step":
        from core.application.orchestration.run_cursor import seeded_step_cursor

        extra_state.update(
            {
                "run_cursor": seeded_step_cursor(normalized_step_id, seed=execution_id).dump(),
                "current_step_id": normalized_step_id,
                "status": "in_progress",
                "step_test_run_context": {
                    "workflow_id": normalized_workflow_id,
                    "step_id": normalized_step_id,
                    "execution_id": execution_id,
                },
            }
        )

    meta = {
        "selected_workflow_id": normalized_workflow_id,
        "workflow_test_run": normalized_kind == "workflow",
        "step_test_run": normalized_kind == "step",
        "test_mode": True,
        "execution_id": execution_id,
        "workflow_id": normalized_workflow_id,
        "project_id": normalized_project_id,
        "step_id": normalized_step_id or None,
        "step_kind": _text(step_payload.get("step_kind")) or None,
        **_bounded_caller_meta(caller),
    }
    if normalized_kind == "workflow" and kept_test_outputs:
        meta["kept_test_outputs"] = {
            str(step_id): dict(outputs)
            for step_id, outputs in kept_test_outputs.items()
            if str(step_id).strip() and isinstance(outputs, Mapping) and outputs
        }
    if expected_revision is not None:
        from core.application.jobs.workflow_definition_snapshot_repository import (
            EXPECTED_REVISION_META_KEY,
        )

        meta[EXPECTED_REVISION_META_KEY] = int(expected_revision)
    try:
        job_id = enqueue(
            task_id=normalized_task_id,
            task_summary=_text(task_summary),
            task_source=task_source,
            project_id=normalized_project_id,
            project_name=_text(project_name),
            project_config=dict(project_config),
            mode=normalized_mode or None,
            test_mode=True,
            extra_state=extra_state,
            meta=meta,
        )
    except BaseException:
        if sample_files is not None:
            sample_files.discard()
        raise
    normalized_job_id = _text(job_id)
    if sample_files is not None and normalized_job_id:
        sample_files.bind_to_job(normalized_job_id)
    job_row = job_store.get_job(normalized_job_id) if normalized_job_id else None
    thread_id = _text((job_row or {}).get("thread_id")) if isinstance(job_row, Mapping) else ""
    if not normalized_job_id or not thread_id:
        raise RuntimeError("Test-run submission did not expose a durable thread identity")

    result: dict[str, Any] = {
        "status": "queued",
        "final_status": "queued",
        "job_id": normalized_job_id,
        "thread_id": thread_id,
        "execution_id": execution_id,
        "workflow_id": normalized_workflow_id,
        "project_id": normalized_project_id,
        "project_name": _text(project_name),
        "task_id": normalized_task_id,
        "task_summary": _text(task_summary),
        "mode": normalized_mode,
        "test_mode": True,
        "needs_user_approval": False,
    }
    if normalized_kind == "step":
        result.update(
            {
                "step_id": normalized_step_id,
                "step_kind": _text(step_payload.get("step_kind")),
            }
        )
    return result


__all__ = [
    "TestRunInputRejected",
    "TestRunReadinessRejected",
    "materialize_test_run_envelope",
    "submit_test_run",
]
