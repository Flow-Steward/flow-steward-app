from __future__ import annotations

import copy
from typing import Any

from core.infrastructure.extension_runtime.extension_invocation_audit import (
    log_host_extension_invocation,
)

from .operation_context import _ActionExecutionContext
from .operation_projection import _AUDIT_REDACTED, _redact_action_audit_payload


def _log_action_invocation(
    context: _ActionExecutionContext,
    invocation: dict[str, Any],
) -> None:
    logged_target = copy.deepcopy(context.target)
    runtime_connection = logged_target.get("connection")
    if isinstance(runtime_connection, dict):
        logged_target["connection"] = {
            key: copy.deepcopy(value)
            for key, value in runtime_connection.items()
            if key not in {"config", "secret_refs", "secrets", "session"}
        }
        config = runtime_connection.get("config")
        if isinstance(config, dict):
            logged_target["connection"]["config"] = dict.fromkeys(config, _AUDIT_REDACTED)
        secret_refs = runtime_connection.get("secret_refs")
        refs = secret_refs.get("refs") if isinstance(secret_refs, dict) else None
        if isinstance(refs, dict):
            logged_target["connection"]["secret_refs"] = {
                "refs": dict.fromkeys(refs, _AUDIT_REDACTED)
            }
        secrets = runtime_connection.get("secrets")
        if isinstance(secrets, dict):
            logged_target["connection"]["secrets"] = dict.fromkeys(secrets, _AUDIT_REDACTED)
        if runtime_connection.get("session"):
            logged_target["connection"]["session"] = {"present": True}
    project_state = logged_target.get("project_state")
    if isinstance(project_state, dict):
        logged_target["project_state"] = {
            "item_count": len(project_state.get("items") or []),
        }
    logged_request = context.body.model_dump()
    logged_request.pop("values", None)
    logged_request.pop("data", None)
    logged_request["target"] = logged_target
    logged_request["input"] = context.input_payload
    logged_request = _redact_action_audit_payload(context, logged_request)
    log_host_extension_invocation(
        bl=context.bl,
        extension=context.extension,
        extension_id=context.extension_id,
        invocation_type=f"action:{context.action_id}",
        request=logged_request,
        invocation=_redact_action_audit_payload(context, invocation, invocation=True),
        runtime_context=context.runtime_ctx,
        request_context={"source": "extension_binding_service.execute_action"},
        action_id=context.action_id,
        external_effect_id=context.external_effect_id or None,
    )
