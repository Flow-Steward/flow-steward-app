"""Shared workflow start helper for canonical enqueue metadata and state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from core.application.idempotency.service import RequestIdempotencyService

_REDACTED = "[REDACTED]"
_MAX_INVOCATION_BYTES = 16 * 1024
_MAX_STRING_BYTES = 4 * 1024
WORKFLOW_RUN_SUMMARY_KEY = "react.jobs.generated_workflow_run_summary"
_REDACT_KEYS = {
    "apikey",
    "token",
    "secret",
    "password",
    "passphrase",
    "privatekey",
    "dsn",
    "authorization",
}


def workflow_run_summary(workflow_id: str) -> str:
    """Describe a direct workflow launch using its canonical workflow identity."""
    return f"Workflow run: {str(workflow_id or '').strip()}"


def workflow_run_summary_metadata(workflow_id: str) -> dict[str, Any]:
    """Mark a platform-generated summary without classifying owner-written text."""
    return {
        "task_summary_key": WORKFLOW_RUN_SUMMARY_KEY,
        "task_summary_params": {"workflow_id": str(workflow_id or "").strip()},
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _idempotency_key_digest(value: Any) -> str | None:
    clean_value = str(value or "").strip()
    return RequestIdempotencyService.digest_key(clean_value) if clean_value else None


def _normalize_channel(
    *,
    entry_path: str,
    task_source: str,
    override: str = "",
) -> str:
    normalized_override = str(override or "").strip().lower()
    if normalized_override:
        return normalized_override
    normalized_entry = str(entry_path or "").strip().lower()
    normalized_source = str(task_source or "").strip().lower()
    if normalized_source == "mcp_workflow":
        return "mcp"
    if normalized_source in {"schedule", "scheduled"} or normalized_entry == "schedule":
        return "schedule"
    if normalized_entry == "webhook":
        return "webhook"
    if normalized_entry == "provider_webhook" or normalized_source == "provider":
        return "provider_webhook"
    if normalized_entry == "manual_ui":
        return "manual_ui"
    if normalized_entry == "manual_api":
        return "manual_api"
    return "manual_api"


def _normalize_trigger_family(
    *,
    channel: str,
    entry_path: str,
    task_source: str,
    override: str = "",
) -> str:
    normalized_override = str(override or "").strip().lower()
    if normalized_override:
        return normalized_override
    if channel == "job_restart":
        return "restart"
    if channel == "webhook":
        return "incoming_webhook"
    if channel == "provider_webhook":
        return "provider_trigger"
    if channel == "schedule" or str(task_source or "").strip().lower() in {"schedule", "scheduled"}:
        return "schedule"
    if channel == "mcp":
        return "mcp"
    if str(entry_path or "").strip().lower() in {"manual_ui", "manual_api"}:
        return "manual"
    return "manual"


def _redact_key(name: str) -> bool:
    text = str(name or "").strip().lower()
    if not text:
        return False
    parts = [
        "".join(ch for ch in chunk if ch.isalnum()) for chunk in text.replace("-", "_").split("_")
    ]
    normalized = "".join(parts)
    if normalized in _REDACT_KEYS:
        return True
    return any(part in _REDACT_KEYS for part in parts if part)


def _truncate_string(value: str) -> tuple[str, bool]:
    text = str(value or "")
    if len(text.encode("utf-8")) <= _MAX_STRING_BYTES:
        return text, False
    encoded = text.encode("utf-8")[: _MAX_STRING_BYTES - 3]
    truncated = encoded.decode("utf-8", errors="ignore").rstrip()
    return f"{truncated}...", True


def _sanitize_invocation_value(value: Any, *, parent_key: str = "") -> tuple[Any, bool]:
    truncated = False
    if _redact_key(parent_key):
        return _REDACTED, False
    from core.infrastructure.security.secret_reference import is_secret_reference

    if is_secret_reference(value):
        return _REDACTED, False
    if isinstance(value, str):
        return _truncate_string(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value, False
    if isinstance(value, dict):
        payload: dict[str, Any] = {}
        for key, raw in value.items():
            sanitized, child_truncated = _sanitize_invocation_value(raw, parent_key=str(key))
            payload[str(key)] = sanitized
            truncated = truncated or child_truncated
        return payload, truncated
    if isinstance(value, list):
        items: list[Any] = []
        for raw in value:
            sanitized, child_truncated = _sanitize_invocation_value(raw, parent_key=parent_key)
            items.append(sanitized)
            truncated = truncated or child_truncated
        return items, truncated
    coerced, child_truncated = _truncate_string(str(value))
    return coerced, child_truncated


def _redact_secret_references(value: Any) -> Any:
    from core.infrastructure.security.secret_reference import (
        REDACTED_SECRET_REFERENCE_MARKER,
        as_secret_reference,
    )

    reference = as_secret_reference(value)
    if reference is not None:
        return {
            "__type__": REDACTED_SECRET_REFERENCE_MARKER,
            "kind": "inline" if reference.is_inline else "configured",
        }
    if isinstance(value, dict):
        return {str(key): _redact_secret_references(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret_references(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_secret_references(item) for item in value]
    return value


def _contains_inline_secret_reference(value: Any) -> bool:
    from core.infrastructure.security.secret_reference import as_secret_reference

    reference = as_secret_reference(value)
    if reference is not None:
        return reference.is_inline
    if isinstance(value, dict):
        return any(_contains_inline_secret_reference(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_inline_secret_reference(item) for item in value)
    return False


def safe_workflow_job_metadata(value: Any) -> dict[str, Any]:
    """Project durable job metadata without executable secret references."""
    metadata = dict(value) if isinstance(value, dict) else {}
    if _contains_inline_secret_reference(metadata):
        metadata["rerun_requires_secret_inputs"] = True
    projected = _redact_secret_references(metadata)
    return projected if isinstance(projected, dict) else {}


def _shrink_invocation_payload(value: Any) -> tuple[Any, bool]:
    truncated = False
    if isinstance(value, dict):
        payload = dict(value)
        for key in ("mapped_inputs", "files", "request_context_summary"):
            candidate = payload.get(key)
            encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str).encode(
                "utf-8"
            )
            if len(encoded) > 16 * 1024:
                payload[key] = f"{key!s} truncated"
                truncated = True
        while (
            len(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            )
            > _MAX_INVOCATION_BYTES
        ):
            removable_keys = [
                key
                for key in ("mapped_inputs", "files", "request_context_summary")
                if key in payload
            ]
            if removable_keys:
                payload.pop(removable_keys[-1], None)
                truncated = True
                continue
            break
        return payload, truncated
    return value, truncated


def _request_context_summary_from_task_envelope(task_envelope: dict[str, Any]) -> dict[str, Any]:
    inputs = (
        dict(task_envelope.get("inputs") or {})
        if isinstance(task_envelope.get("inputs"), dict)
        else {}
    )
    files = (
        list(task_envelope.get("files") or [])
        if isinstance(task_envelope.get("files"), list)
        else []
    )
    return {
        "body_keys": sorted(str(key) for key in inputs),
        "form_keys": [],
        "query_keys": [],
        "file_count": len(files),
        "file_names": [
            str(item.get("filename") or "").strip()
            for item in files
            if isinstance(item, dict) and str(item.get("filename") or "").strip()
        ],
    }


def _invocation_file_summaries(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for raw in files:
        if not isinstance(raw, dict):
            continue
        source_ref = str(raw.get("source_ref") or "").strip()
        field_name = ""
        if source_ref.startswith("multipart:"):
            field_name = source_ref.split(":", 1)[1].strip()
        summaries.append(
            {
                "field_name": field_name or str(raw.get("field_name") or "").strip() or None,
                "filename": str(raw.get("filename") or "").strip() or None,
                "content_type": str(raw.get("content_type") or "").strip() or None,
                "size_bytes": int(raw.get("size_bytes") or 0)
                if raw.get("size_bytes") not in (None, "")
                else None,
                "artifact_id": str(raw.get("artifact_id") or "").strip() or None,
            }
        )
    return summaries


def build_invocation_snapshot(
    *,
    task_source: str,
    entry_path: str,
    source_system: str,
    selected_workflow_id: str,
    workflow_selection_source: str,
    source_event_id: str = "",
    idempotency_key: str = "",
    inputs: dict[str, Any] | None = None,
    task_envelope: dict[str, Any] | None = None,
    files: list[dict[str, Any]] | None = None,
    invocation_patch: dict[str, Any] | None = None,
    declared_secret_inputs: set[str] | None = None,
) -> dict[str, Any]:
    patch = dict(invocation_patch or {}) if isinstance(invocation_patch, dict) else {}
    normalized_entry_path = str(entry_path or "").strip()
    normalized_task_source = str(task_source or "").strip()
    channel = _normalize_channel(
        entry_path=normalized_entry_path,
        task_source=normalized_task_source,
        override=str(patch.get("channel") or ""),
    )
    trigger_family = _normalize_trigger_family(
        channel=channel,
        entry_path=normalized_entry_path,
        task_source=normalized_task_source,
        override=str(patch.get("trigger_family") or ""),
    )
    normalized_inputs = dict(inputs or {}) if isinstance(inputs, dict) else {}
    normalized_task_envelope = dict(task_envelope or {}) if isinstance(task_envelope, dict) else {}
    normalized_files = files if isinstance(files, list) else []
    from core.application.jobs.job_invocation_artifacts import build_effective_input_projection

    input_projection = build_effective_input_projection(
        inputs=normalized_inputs,
        files=_invocation_file_summaries(normalized_files),
        declared_secret_inputs=declared_secret_inputs,
        max_bytes=12 * 1024,
    )
    invocation: dict[str, Any] = {
        "channel": channel,
        "trigger_family": trigger_family,
        "entry_path": str(patch.get("entry_path") or normalized_entry_path or channel).strip(),
        "source_system": str(patch.get("source_system") or source_system or "").strip() or None,
        "request_id": str(patch.get("request_id") or source_event_id or "").strip() or None,
        "received_at": str(patch.get("received_at") or _now_iso()).strip(),
        "source_ip": str(patch.get("source_ip") or "").strip() or None,
        "content_type": str(patch.get("content_type") or "").strip() or None,
        "idempotency_key_digest": str(patch.get("idempotency_key_digest") or "").strip()
        or _idempotency_key_digest(patch.get("idempotency_key") or idempotency_key),
        "selected_workflow_id": str(
            patch.get("selected_workflow_id") or selected_workflow_id or ""
        ).strip()
        or None,
        "workflow_selection_source": str(
            patch.get("workflow_selection_source") or workflow_selection_source or ""
        ).strip()
        or None,
        "entry_key": str(patch.get("entry_key") or "").strip() or None,
        "trigger_id": str(patch.get("trigger_id") or "").strip() or None,
        "origin_job_id": str(patch.get("origin_job_id") or "").strip() or None,
        "mapped_inputs": input_projection["mapped_inputs"],
        "files": input_projection["files"],
        "source_keys": input_projection["source_keys"],
        "size_bytes": input_projection["size_bytes"],
        "sha256": input_projection["sha256"],
        "request_context_summary": (
            dict(patch.get("request_context_summary") or {})
            if isinstance(patch.get("request_context_summary"), dict)
            else _request_context_summary_from_task_envelope(normalized_task_envelope)
        ),
        "truncated": bool(input_projection["truncated"]),
    }
    if patch.get("existing_job_id") is not None:
        invocation["existing_job_id"] = str(patch.get("existing_job_id") or "").strip() or None
    sanitized, truncated = _sanitize_invocation_value(invocation)
    if not isinstance(sanitized, dict):
        sanitized = invocation
    shrunk, shrunk_truncated = _shrink_invocation_payload(sanitized)
    if not isinstance(shrunk, dict):
        shrunk = sanitized
    shrunk["truncated"] = bool(input_projection["truncated"] or truncated or shrunk_truncated)
    return shrunk


def normalize_entry_path(
    entry_path: str | None,
    *,
    task_source: str | None = None,
    default: str = "manual_api",
) -> str:
    normalized = str(entry_path or "").strip().lower()
    if normalized:
        return normalized

    source = str(task_source or "").strip().lower()
    if source in {"web", "ui", "chat", "manual_ui"}:
        return "manual_ui"
    if source in {"api", "manual_api"}:
        return "manual_api"
    if source in {"schedule", "scheduled"}:
        return "schedule"
    if source in {"webhook"}:
        return "webhook"
    if source in {"provider", "task_ingress"}:
        return "task_ingress"
    return str(default or "manual_api").strip().lower() or "manual_api"


def enqueue_workflow_start(
    *,
    enqueue_task_fn,
    task_id: str,
    task_summary: str,
    task_source: str,
    project_id: Any,
    project_name: str,
    project_config: dict[str, Any],
    selected_workflow_id: str,
    workflow_selection_source: str,
    entry_path: str,
    input_contract: str,
    source_system: str,
    requested_workflow_id: str = "",
    source_event_id: str = "",
    source_object_id: str = "",
    idempotency_key: str = "",
    mode: str = "",
    test_mode: bool = False,
    task_envelope: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    files: list[dict[str, Any]] | None = None,
    extra_state: dict[str, Any] | None = None,
    workflow_secret_ref_fingerprints: list[str] | None = None,
    job_meta: dict[str, Any] | None = None,
    declared_secret_inputs: set[str] | None = None,
    pre_publish_fn=None,
    preparation_additional_heartbeat=None,
    fail_on_pre_publish_error: bool = False,
) -> str:
    clean_task_id = str(task_id or "").strip()
    clean_summary = str(task_summary or "").strip()
    clean_task_source = str(task_source or "").strip()
    clean_selected_workflow_id = str(selected_workflow_id or "").strip()
    clean_selection_source = str(workflow_selection_source or "").strip()
    clean_entry_path = str(entry_path or "").strip()
    clean_input_contract = str(input_contract or "").strip() or "direct_inputs"
    clean_source_system = str(source_system or "").strip()
    clean_requested_workflow_id = str(requested_workflow_id or "").strip()
    clean_source_event_id = str(source_event_id or "").strip()
    clean_source_object_id = str(source_object_id or "").strip()
    clean_idempotency_key = str(idempotency_key or "").strip()
    clean_mode = str(mode or "").strip()

    normalized_files: list[dict[str, Any]] = files if isinstance(files, list) else []
    normalized_envelope = dict(task_envelope or {}) if isinstance(task_envelope, dict) else {}
    normalized_inputs = dict(inputs or {}) if isinstance(inputs, dict) else {}

    from core.application.orchestration.state_path_guards import strip_reserved_runtime_state

    effective_extra_state = strip_reserved_runtime_state(extra_state or {})
    if clean_selected_workflow_id:
        effective_extra_state["workflow_id"] = clean_selected_workflow_id
    if clean_selection_source:
        effective_extra_state["workflow_selection_source"] = clean_selection_source
    if clean_entry_path:
        effective_extra_state["entry_path"] = clean_entry_path
    if clean_source_system and not str(effective_extra_state.get("source_system") or "").strip():
        effective_extra_state["source_system"] = clean_source_system
    if clean_source_event_id:
        effective_extra_state["source_event_id"] = clean_source_event_id
    if clean_source_object_id:
        effective_extra_state["source_object_id"] = clean_source_object_id

    if clean_input_contract == "task_envelope" and normalized_envelope:
        normalized_envelope["files"] = normalized_files
        effective_extra_state["task_envelope"] = normalized_envelope
        envelope_inputs = (
            dict(normalized_envelope.get("inputs") or {})
            if isinstance(normalized_envelope.get("inputs"), dict)
            else {}
        )
        if envelope_inputs:
            effective_extra_state.setdefault("inputs", envelope_inputs)
    elif clean_input_contract == "direct_inputs":
        from core.application.orchestration.task_envelope import TaskEnvelope

        direct_task_envelope = TaskEnvelope(
            entry_path=clean_entry_path
            or normalize_entry_path(None, task_source=clean_source_system),
            source_system=clean_source_system or clean_task_source or "manual",
            source_event_id=clean_source_event_id,
            source_object_id=clean_source_object_id,
            task_type=str(normalized_inputs.get("task_type") or "").strip(),
            title=clean_summary,
            description=str(normalized_inputs.get("description") or clean_summary).strip(),
            priority=str(
                normalized_inputs.get("priority") or effective_extra_state.get("priority") or ""
            ),
            project_id=str(project_id or "").strip(),
            requested_workflow_id=clean_requested_workflow_id or clean_selected_workflow_id,
            inputs=normalized_inputs,
            files=normalized_files,
            entry_context={
                "workflow_selection_source": clean_selection_source,
            },
        ).to_dict()
        # The workflow contract validated this exact mapping. TaskEnvelope's
        # general-purpose presentation defaults must not add undeclared inputs.
        direct_task_envelope["inputs"] = dict(normalized_inputs)
        effective_extra_state["task_envelope"] = direct_task_envelope
        effective_extra_state.setdefault("inputs", normalized_inputs)
    if normalized_files is not None:
        effective_extra_state["files"] = normalized_files

    effective_meta = dict(job_meta or {})
    for execution_only_key in (
        "_workflow_secret_ref_fingerprints",
        "execution_envelope",
        "files",
        "idempotency_key",
        "initial_requirements",
        "inputs",
        "original_request",
        "request",
        "task_envelope",
        "workflow_interface",
    ):
        effective_meta.pop(execution_only_key, None)
    if clean_selected_workflow_id and workflow_secret_ref_fingerprints is not None:
        effective_extra_state["_workflow_secret_ref_fingerprints"] = [
            str(item).strip() for item in workflow_secret_ref_fingerprints if str(item).strip()
        ]
    elif clean_selected_workflow_id:
        try:
            from core.application.registry.workflow_contract_service import (
                WorkflowInterfaceService,
            )
            from core.application.registry.workflow_secret_defaults import (
                workflow_secret_launch_snapshot,
            )

            effective_extra_state["_workflow_secret_ref_fingerprints"] = (
                workflow_secret_launch_snapshot(
                    WorkflowInterfaceService().interface_for_workflow_id(
                        clean_selected_workflow_id
                    ),
                    inputs=(
                        normalized_envelope.get("inputs")
                        if clean_input_contract == "task_envelope"
                        else normalized_inputs
                    ),
                )
            )
        except Exception:
            # Missing launch authority fails closed at the connector boundary.
            effective_extra_state["_workflow_secret_ref_fingerprints"] = []
    if clean_entry_path:
        effective_meta["entry_path"] = clean_entry_path
    effective_meta["input_contract"] = clean_input_contract
    if clean_selected_workflow_id:
        effective_meta["selected_workflow_id"] = clean_selected_workflow_id
    if clean_selection_source:
        effective_meta["workflow_selection_source"] = clean_selection_source
    if clean_source_system:
        effective_meta["source_system"] = clean_source_system
    if clean_requested_workflow_id:
        effective_meta["requested_workflow_id"] = clean_requested_workflow_id
    if clean_source_event_id:
        effective_meta["source_event_id"] = clean_source_event_id
    if clean_source_object_id:
        effective_meta["source_object_id"] = clean_source_object_id
    if clean_input_contract == "task_envelope":
        envelope_id = str(normalized_envelope.get("envelope_id") or "").strip()
        if envelope_id:
            effective_meta["task_envelope_id"] = envelope_id
    if clean_idempotency_key:
        effective_meta["idempotency_key_digest"] = _idempotency_key_digest(clean_idempotency_key)
    if clean_mode:
        effective_meta["mode"] = clean_mode
    execution_envelope = (
        dict(normalized_envelope)
        if clean_input_contract == "task_envelope" and normalized_envelope
        else dict(effective_extra_state.get("task_envelope") or {})
    )
    execution_inputs = (
        dict(execution_envelope.get("inputs") or {})
        if clean_input_contract == "task_envelope"
        else dict(normalized_inputs)
    )
    execution_envelope["inputs"] = execution_inputs
    execution_envelope["files"] = [
        dict(item) for item in normalized_files if isinstance(item, dict)
    ]
    invocation_patch = (
        dict(effective_meta.get("invocation") or {})
        if isinstance(effective_meta.get("invocation"), dict)
        else {}
    )
    effective_meta["invocation"] = build_invocation_snapshot(
        task_source=clean_task_source,
        entry_path=clean_entry_path,
        source_system=clean_source_system,
        selected_workflow_id=clean_selected_workflow_id,
        workflow_selection_source=clean_selection_source,
        source_event_id=clean_source_event_id,
        idempotency_key=clean_idempotency_key,
        inputs=execution_inputs,
        task_envelope=normalized_envelope,
        files=normalized_files,
        invocation_patch=invocation_patch,
        declared_secret_inputs=declared_secret_inputs,
    )
    effective_meta = safe_workflow_job_metadata(effective_meta)
    replay_snapshot = {
        "workflow_id": clean_selected_workflow_id,
        "inputs": execution_inputs,
        "files": [dict(item) for item in normalized_files if isinstance(item, dict)],
        "test_mode": bool(test_mode),
        "input_contract": clean_input_contract,
        "task_envelope": dict(effective_extra_state.get("task_envelope") or {}),
        "_workflow_secret_ref_fingerprints": list(
            effective_extra_state.get("_workflow_secret_ref_fingerprints") or []
        ),
    }

    return str(
        enqueue_task_fn(
            task_id=clean_task_id,
            task_summary=clean_summary,
            task_source=clean_task_source,
            project_id=str(project_id or "").strip(),
            project_name=str(project_name or "").strip(),
            project_config=dict(project_config or {}),
            mode=clean_mode or None,
            test_mode=bool(test_mode),
            extra_state=effective_extra_state or None,
            meta=effective_meta or None,
            replay_snapshot=replay_snapshot,
            pre_publish_fn=pre_publish_fn,
            preparation_additional_heartbeat=preparation_additional_heartbeat,
            fail_on_pre_publish_error=bool(fail_on_pre_publish_error),
        )
    )
