"""Terminal audit and result projection for connector extension actions."""

from __future__ import annotations

from typing import Any

from core.application.orchestration.connector_mapping import _project_extension_output_aliases


def complete_connector_extension_action(
    *,
    bl: Any,
    extension: dict[str, Any],
    resolved_extension_id: str,
    ext_id: str,
    install_id: str,
    op_id: str,
    event_input_payload: Any,
    logged_target: Any,
    connection_binding: Any,
    invocation: dict[str, Any],
    runtime_context: dict[str, Any],
    dataset_page_context: Any,
    external_effect_id: str | None,
    operation_kind: Any,
    external_effect: Any,
    produced_artifacts: list[dict[str, Any]],
    produced_datasets: list[dict[str, Any]],
    grant_outputs: dict[str, Any],
) -> dict[str, Any]:
    from core.infrastructure.extension_runtime.extension_invocation_audit import (
        log_host_extension_invocation,
    )

    log_host_extension_invocation(
        bl=bl,
        extension=extension,
        extension_id=resolved_extension_id or ext_id or install_id,
        invocation_type=f"action:{op_id}",
        request={
            "action_id": op_id,
            "input_payload": dict(event_input_payload or {}),
            "target": logged_target,
            "connector_install_id": install_id or None,
            "connection_binding": dict(connection_binding or {}),
        },
        invocation=invocation,
        runtime_context=runtime_context,
        request_context={
            "source": "connector_step_runtime",
            "connector_install_id": install_id or None,
            "external_effect_id": external_effect_id or None,
            **({"dataset_page": dataset_page_context} if dataset_page_context is not None else {}),
        },
        action_id=op_id,
        external_effect_id=external_effect_id or None,
    )
    ok = str(invocation.get("status") or "").strip().lower() == "succeeded"
    visible_produced_artifacts = produced_artifacts if ok else []
    visible_produced_datasets = produced_datasets if ok else []
    visible_grant_outputs = grant_outputs if ok else {}
    result = {
        "handled": True,
        "ok": ok,
        "runtime": "extension_action",
        "extension_id": resolved_extension_id or ext_id or install_id,
        "action_id": op_id,
        # Operation semantics come from the installed immutable manifest, not
        # from an extension-controlled response envelope.
        "operation_kind": operation_kind,
        "invocation": invocation,
        "external_effect": external_effect,
        "produced_artifacts": visible_produced_artifacts,
        "produced_datasets": visible_produced_datasets,
        "grant_outputs": visible_grant_outputs,
        "detail": ""
        if ok
        else str(invocation.get("detail") or "Connector extension action failed"),
    }
    return _project_extension_output_aliases(
        result,
        visible_grant_outputs=visible_grant_outputs,
        visible_produced_artifacts=visible_produced_artifacts,
        visible_produced_datasets=visible_produced_datasets,
    )
