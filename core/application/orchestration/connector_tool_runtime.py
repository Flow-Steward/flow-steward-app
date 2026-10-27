"""Tool connector dispatch and test-mode suppression policy."""

from __future__ import annotations

import logging
from typing import Any

from core.application.extensions.secret_reflection import (
    secret_exposure_invocation,
    value_exposes_secret,
)
from core.application.orchestration.connector_events import (
    _emit_connector_tool_event,
    _runtime_ids_from_state,
    _shared_logger,
)
from core.application.orchestration.connector_extension_runtime import _dispatch_extension_runtime
from core.application.orchestration.connector_mapping import (
    CONNECTOR_PREFIX,
    _to_object,
)

logger = logging.getLogger(__name__)


def _tool_id_candidates(connector_id: str) -> list[str]:
    normalized = str(connector_id or "").strip()
    if not normalized:
        return []
    candidates: list[str] = []
    if normalized.startswith(CONNECTOR_PREFIX) and len(normalized) > len(CONNECTOR_PREFIX):
        candidates.append(normalized[len(CONNECTOR_PREFIX) :])
    candidates.append(normalized)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _dispatch_tool_runtime(
    *,
    connector_id: str,
    connector_install_id: str,
    function_name: str,
    operation_id: str,
    input_payload: dict[str, Any],
    event_input_payload: dict[str, Any] | None,
    protected_input_secrets: frozenset[str],
    protected_input_secret_budget_exceeded: bool,
    connection_binding: dict[str, Any],
    state: dict[str, Any],
    suppression_probe_only: bool = False,
) -> dict[str, Any] | None:
    from core.application.registry.registry_manager import RegistryManager

    try:
        registry = RegistryManager.instance()
    except Exception as exc:
        logger.debug("Connector tool registry is unavailable: %s", exc)
        return None
    tools_registry = getattr(registry, "tools", None)
    if tools_registry is None:
        return None

    for tool_id in _tool_id_candidates(connector_id):
        get_tool = getattr(tools_registry, "get_tool", None)
        manifest = get_tool(tool_id) if callable(get_tool) else None
        if manifest is None or getattr(manifest, "enabled", True) is False:
            continue
        runtime_context = _runtime_ids_from_state(state)
        bl = _shared_logger()
        operations = list(getattr(getattr(manifest, "interface", None), "operations", []) or [])
        declared_function = str(
            getattr(getattr(manifest, "interface", None), "function_name", "") or ""
        ).strip()
        selected_function = (
            str(function_name or "").strip()
            or str(operation_id or "").strip()
            or (
                str((operations[0] or {}).get("name") or "").strip() if len(operations) == 1 else ""
            )
            or declared_function
            or "invoke"
        )
        if protected_input_secret_budget_exceeded:
            return _secret_exposure_tool_result(
                tool_id=tool_id,
                function_name=selected_function,
            )
        implementation = getattr(manifest, "implementation", None)
        implementation_payload = (
            _to_object(implementation.to_dict())
            if implementation is not None and hasattr(implementation, "to_dict")
            else _to_object(implementation)
        )
        implementation_type = str(
            getattr(implementation, "type", "")
            or getattr(implementation, "kind", "")
            or implementation_payload.get("type")
            or implementation_payload.get("kind")
            or ""
        ).strip()
        is_mcp_manifest = implementation_type == "mcp"
        selected_operation = next(
            (
                operation
                for operation in operations
                if str((operation or {}).get("name") or "").strip() == selected_function
            ),
            {},
        )
        artifact_policy_operation_id = str(
            (selected_operation or {}).get("artifact_policy_operation_id") or ""
        ).strip()
        if implementation_type == "extension":
            extension_id = str(
                implementation_payload.get("extension_key")
                or implementation_payload.get("extension_id")
                or ""
            ).strip()
            action_id = str(implementation_payload.get("action") or "").strip() or "invoke_tool"
            if not artifact_policy_operation_id and selected_function == action_id:
                artifact_policy_operation_id = action_id
            extension_input = dict(input_payload or {})
            extension_event_input = dict(event_input_payload or {})
            if selected_function != action_id:
                extension_input = {
                    "tool_name": selected_function,
                    "arguments": extension_input,
                }
                extension_event_input = {
                    "tool_name": selected_function,
                    "arguments": extension_event_input,
                }
            extension_result = _dispatch_extension_runtime(
                connector_id=connector_id,
                extension_id=extension_id,
                connector_install_id=connector_install_id,
                action_id=action_id,
                operation_id=action_id,
                artifact_policy_operation_id=artifact_policy_operation_id,
                input_payload=extension_input,
                event_input_payload=extension_event_input,
                protected_input_secrets=protected_input_secrets,
                protected_input_secret_budget_exceeded=(protected_input_secret_budget_exceeded),
                connection_binding=connection_binding,
                state=state,
                suppression_probe_only=suppression_probe_only,
            )
            if extension_result and extension_result.get("handled"):
                extension_result["tool_id"] = tool_id
                extension_result["function_name"] = selected_function
                return extension_result
        if suppression_probe_only:
            if _tool_manifest_operation_is_test_mode_suppressed(
                manifest=manifest,
                implementation_type=implementation_type,
                selected_operation=selected_operation,
                runtime_context=runtime_context,
            ):
                return _suppressed_tool_manifest_result(
                    tool_id=tool_id,
                    function_name=selected_function,
                )
            return {
                "handled": False,
                "runtime": "tool_manifest",
                "tool_id": tool_id,
                "function_name": selected_function,
                "manifest_matched": True,
            }
        try:
            from core.application.orchestration.tools.tool_resolver import invoke_manifest_tool
            from core.infrastructure.security.prompt_safety import redact_sensitive_structure

            runtime_context["source"] = "connector_step_runtime"
            if bl is not None:
                runtime_context["logger"] = bl
            event_input = input_payload
            if isinstance(event_input_payload, dict):
                event_input = event_input_payload
            _emit_connector_tool_event(
                bl,
                job_id=runtime_context.get("job_id"),
                group_id=str(runtime_context.get("group_id") or "").strip(),
                step_id=str(runtime_context.get("step_id") or "").strip(),
                tool_id=tool_id,
                function_name=selected_function,
                event_type="tool_call",
                excerpt=""
                if is_mcp_manifest
                else str(dict(redact_sensitive_structure(event_input))),
                include_excerpt=not is_mcp_manifest,
            )
            result = invoke_manifest_tool(
                manifest,
                function_name=selected_function,
                arguments=dict(input_payload or {}),
                runtime_context=runtime_context,
            )
            if value_exposes_secret(result, protected_input_secrets):
                blocked = _secret_exposure_tool_result(
                    tool_id=tool_id,
                    function_name=selected_function,
                )
                _emit_connector_tool_event(
                    bl,
                    job_id=runtime_context.get("job_id"),
                    group_id=str(runtime_context.get("group_id") or "").strip(),
                    step_id=str(runtime_context.get("step_id") or "").strip(),
                    tool_id=tool_id,
                    function_name=selected_function,
                    event_type="tool_result",
                    excerpt=str(secret_exposure_invocation()),
                    is_error=True,
                    include_excerpt=not is_mcp_manifest,
                )
                return blocked
            _emit_connector_tool_event(
                bl,
                job_id=runtime_context.get("job_id"),
                group_id=str(runtime_context.get("group_id") or "").strip(),
                step_id=str(runtime_context.get("step_id") or "").strip(),
                tool_id=tool_id,
                function_name=selected_function,
                event_type="tool_result",
                excerpt="" if is_mcp_manifest else str(result),
                is_error=False,
                include_excerpt=not is_mcp_manifest,
            )
            return {
                "handled": True,
                "ok": True,
                "runtime": "tool_manifest",
                "tool_id": tool_id,
                "function_name": selected_function,
                "result": result,
                "detail": "",
            }
        except Exception as exc:
            secret_exposure = value_exposes_secret(str(exc), protected_input_secrets)
            error_excerpt = str(exc)
            if secret_exposure:
                error_excerpt = str(secret_exposure_invocation())
            _emit_connector_tool_event(
                bl,
                job_id=runtime_context.get("job_id"),
                group_id=str(runtime_context.get("group_id") or "").strip(),
                step_id=str(runtime_context.get("step_id") or "").strip(),
                tool_id=tool_id,
                function_name=selected_function,
                event_type="tool_result",
                excerpt="" if is_mcp_manifest else error_excerpt,
                is_error=True,
                include_excerpt=not is_mcp_manifest,
            )
            if secret_exposure:
                return _secret_exposure_tool_result(
                    tool_id=tool_id,
                    function_name=selected_function,
                )
            return {
                "handled": True,
                "ok": False,
                "runtime": "tool_manifest",
                "tool_id": tool_id,
                "function_name": str(function_name or operation_id or "").strip() or "invoke",
                "result": {},
                "detail": str(exc),
            }
    return None


def _tool_manifest_operation_is_test_mode_suppressed(
    *,
    manifest: Any,
    implementation_type: str,
    selected_operation: dict[str, Any],
    runtime_context: dict[str, Any],
) -> bool:
    """Resolve suppression from the operation contract, never connector identity."""
    if runtime_context.get("test_mode") is not True:
        return False
    config = _to_object(getattr(manifest, "config", {}))
    behavior = (
        str(selected_operation.get("test_mode_behavior") or config.get("test_mode_behavior") or "")
        .strip()
        .lower()
    )
    if behavior:
        return behavior == "suppress"
    if implementation_type != "mcp":
        return False
    annotations = _to_object(selected_operation.get("annotations")) or _to_object(
        config.get("annotations")
    )
    # MCP defines readOnlyHint=false as the default. Test runs may invoke reads,
    # but must never call a remote operation that is not contractually read-only.
    return annotations.get("readOnlyHint") is not True


def _suppressed_tool_manifest_result(*, tool_id: str, function_name: str) -> dict[str, Any]:
    suppressed = {
        "test_mode_status": "suppressed",
        "external_effect_status": "suppressed",
        "definitely_no_external_effect": True,
    }
    return {
        "handled": True,
        "ok": True,
        "runtime": "tool_manifest",
        "tool_id": tool_id,
        "function_name": function_name,
        "result": dict(suppressed),
        "invocation": {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": dict(suppressed),
                "external_effect_status": "suppressed",
                "definitely_no_external_effect": True,
            },
        },
        "operation_kind": "action",
        "detail": "test_mode_suppressed",
    }


def _secret_exposure_tool_result(*, tool_id: str, function_name: str) -> dict[str, Any]:
    blocked = secret_exposure_invocation()
    return {
        "handled": True,
        "ok": False,
        "runtime": "tool_manifest",
        "tool_id": tool_id,
        "function_name": function_name,
        "result": {},
        "detail": blocked["detail"],
        "errors": blocked["errors"],
    }
