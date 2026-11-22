"""Summaries and metadata lookups over extension invocation results for external effects."""

from __future__ import annotations

from typing import Any

from core.application.extensions.external_effect_contracts import (
    _as_dict,
    _as_list,
    _safe_external_effect_guidance,
    _text,
)


def _response_summary(
    invocation: dict[str, Any], host_outputs: dict[str, Any] | None = None
) -> dict[str, Any]:
    response = _as_dict(invocation.get("response"))
    result = _as_dict(response.get("result"))
    summary: dict[str, Any] = {}
    for key in ("ok", "delivered", "status", "error_code"):
        if key in response:
            summary[key] = response.get(key)
        elif key in result:
            summary[key] = result.get(key)
    if invocation.get("status"):
        summary["invocation_status"] = invocation.get("status")
    guidance = _safe_external_effect_guidance(result.get("guidance"))
    if guidance:
        summary["guidance"] = guidance
    trusted_outputs = host_outputs or {}
    produced_artifacts = _produced_output_summary(
        trusted_outputs.get("produced_artifacts"), kind="artifact"
    )
    if produced_artifacts:
        summary["produced_artifacts"] = produced_artifacts
    produced_datasets = _produced_output_summary(
        trusted_outputs.get("produced_datasets"), kind="dataset"
    )
    if produced_datasets:
        summary["produced_datasets"] = produced_datasets
    grant_outputs = _as_dict(trusted_outputs.get("grant_outputs"))
    if grant_outputs:
        summary["grant_outputs"] = {
            str(key): str(value)
            for key, value in grant_outputs.items()
            if str(key).strip() and str(value).strip()
        }
    return summary


def _produced_output_summary(value: Any, *, kind: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    id_field = "dataset_id" if kind == "dataset" else "artifact_id"
    handle_field = "dataset_handle" if kind == "dataset" else "artifact_handle"
    rows: list[dict[str, Any]] = []
    for raw in value:
        row = _as_dict(raw)
        object_id = _text(row.get(id_field))
        if not object_id:
            continue
        handle = _text(row.get(handle_field)) or f"{kind}:{object_id}"
        summary = {
            id_field: object_id,
            handle_field: handle,
        }
        binding_key = _text(row.get("binding_key"))
        if binding_key:
            summary["binding_key"] = binding_key
        for key in ("size_bytes", "sha256", "row_count", "content_type"):
            if key in row and row.get(key) not in (None, ""):
                summary[key] = row.get(key)
        rows.append(summary)
    return rows


def _effect_metadata_payloads(invocation: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    response = _as_dict(invocation.get("response"))
    result = _as_dict(response.get("result"))
    return tuple(
        payload
        for payload in (
            _as_dict(response.get("external_effect")),
            _as_dict(result.get("external_effect")),
            response,
            result,
        )
        if payload
    )


def _effect_result_payload(invocation: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical business/effect payload for compatibility callers."""
    response = _as_dict(invocation.get("response"))
    result = _as_dict(response.get("result"))
    for candidate in (
        response.get("external_effect"),
        result.get("external_effect"),
        result,
        response,
    ):
        payload = _as_dict(candidate)
        if payload:
            return payload
    return {}


def _effect_metadata_value(
    payloads: tuple[dict[str, Any], ...],
    *field_names: str,
) -> Any:
    for payload in payloads:
        for field_name in field_names:
            if field_name in payload and payload.get(field_name) not in (None, ""):
                return payload.get(field_name)
    return None


def _effect_status_values(
    response: dict[str, Any],
    result: dict[str, Any],
) -> frozenset[str]:
    response_effect = _as_dict(response.get("external_effect"))
    result_effect = _as_dict(result.get("external_effect"))
    values = (
        response.get("external_effect_status"),
        result.get("external_effect_status"),
        response.get("effect_status"),
        result.get("effect_status"),
        response_effect.get("status"),
        response_effect.get("effect_status"),
        result_effect.get("status"),
        result_effect.get("effect_status"),
    )
    return frozenset(_text(value).lower() for value in values if _text(value))


def _definitive_no_effect_values(
    response: dict[str, Any],
    result: dict[str, Any],
) -> tuple[Any, ...]:
    payloads = (
        _as_dict(response.get("external_effect")),
        _as_dict(result.get("external_effect")),
        response,
        result,
    )
    return tuple(
        payload["definitely_no_external_effect"]
        for payload in payloads
        if "definitely_no_external_effect" in payload
    )


def _project_state_commit_error_code(invocation: dict[str, Any]) -> str:
    for raw in _as_list(invocation.get("errors")):
        row = _as_dict(raw)
        code = _text(row.get("code"))
        if code.startswith("project_state_"):
            return code
    detail = _text(invocation.get("detail"))
    return detail if detail.startswith("project_state_") else ""
