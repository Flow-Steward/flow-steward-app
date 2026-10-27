# ruff: noqa: F403, F405, RUF022
# mypy: ignore-errors
from __future__ import annotations

from core.application.extensions.host_contract import (
    EXECUTION_SCOPE_FIELDS,
    normalize_execution_scope,
)
from core.application.extensions.runtime_host_grants import trusted_resource_grant

from .extension_runtime_common import *  # NOSONAR
from .extension_runtime_invocation import (
    _declared_operation_ids,
    _declared_operation_row,
    _declared_operation_rows,
    _operation_handler_mode,
    extension_runtime_availability_failure,
    run_extension_invocation,
)
from .extension_runtime_platform import _run_platform_action, _run_platform_query
from .extension_runtime_validation import (
    _as_dict,
    _as_list,
    _event_type_allowed_for_extension,
    _normalize_event_subscriptions,
    _normalize_host_context,
    _normalize_string_list,
)


def _operation_runtime_resources(
    extension: dict, *, operation_type: str, operation_id: str
) -> dict[str, Any]:
    row = _declared_operation_row(
        extension,
        operation_type=operation_type,
        operation_id=operation_id,
    )
    handler = _as_dict(_as_dict(row).get("handler"))
    return _as_dict(_as_dict(handler.get("config")).get("runtime_resources"))


def _hydrate_declared_runtime_resources(
    extension: dict,
    *,
    operation_type: str,
    operation_id: str,
    context: dict | None,
    runtime_context: dict | None,
) -> dict[str, Any]:
    runtime = dict(_as_dict(runtime_context))
    resources = _operation_runtime_resources(
        extension,
        operation_type=operation_type,
        operation_id=operation_id,
    )
    logger = runtime.get("logger")
    if not resources or logger is None:
        return runtime
    project_id = str(_as_dict(context).get("project_id") or runtime.get("project_id") or "").strip()
    extension_id = str(
        extension.get("extension_key") or extension.get("extension_id") or ""
    ).strip()
    snapshots: dict[str, Any] = {}

    connection_grant = trusted_resource_grant(
        extension, "project_connection", extension_id=extension_id
    )
    if resources.get("project_connection") and connection_grant and project_id:
        getter = getattr(logger, "get_project_connection", None)
        if callable(getter):
            lookup = {
                "project_id": project_id,
                "extension_id": extension_id,
                "enabled_only": False,
            }
            connection_id = str(runtime.get("connection_id") or "").strip()
            extension_install_id = str(runtime.get("extension_install_id") or "").strip()
            if connection_id:
                lookup["connection_id"] = connection_id
            if extension_install_id:
                lookup["extension_install_id"] = extension_install_id
            row = getter(
                **lookup,
            )
            if isinstance(row, dict):
                snapshots["project_connection"] = dict(row)
                connection_id = str(row.get("connection_id") or row.get("id") or "").strip()
                install_id = str(row.get("extension_install_id") or "").strip()
                if connection_id:
                    runtime.setdefault("connection_id", connection_id)
                if install_id:
                    runtime.setdefault("extension_install_id", install_id)

    requested_secret_keys = set(_normalize_string_list(resources.get("provider_secrets")))
    secret_grant = trusted_resource_grant(extension, "provider_secrets", extension_id=extension_id)
    granted_secret_keys = set(_normalize_string_list(_as_dict(secret_grant).get("secret_keys")))
    secret_keys = sorted(requested_secret_keys & granted_secret_keys)
    if secret_keys and secret_grant and project_id:
        getter = getattr(logger, "get_provider_secret", None)
        provider_key = str(secret_grant.get("provider_key") or "").strip()
        if callable(getter) and provider_key:
            snapshots["provider_secrets"] = {
                key: getter(
                    provider_key=provider_key,
                    secret_key=key,
                    project_id=project_id,
                    include_global=False,
                )
                for key in secret_keys
            }

    delivery_grant = trusted_resource_grant(
        extension, "notification_deliveries", extension_id=extension_id
    )
    if resources.get("notification_deliveries") and delivery_grant and project_id:
        lister = getattr(logger, "list_notification_delivery_logs", None)
        if callable(lister):
            provider_key = str(delivery_grant.get("provider_key") or "").strip()
            snapshots["notification_deliveries"] = [
                row
                for row in lister(project_id=project_id, limit=2000) or []
                if str(_as_dict(row).get("provider_key") or "").strip() == provider_key
            ]

    routes_grant = trusted_resource_grant(
        extension, "notification_routes", extension_id=extension_id
    )
    if resources.get("notification_routes") and routes_grant and project_id:
        lister = getattr(logger, "list_notification_routes", None)
        if callable(lister):
            provider_key = str(routes_grant.get("provider_key") or "").strip()
            snapshots["notification_routes"] = [
                row
                for row in lister(project_id=project_id, enabled_only=False) or []
                if str(_as_dict(row).get("provider_key") or "").strip() == provider_key
            ]

    callback_grant = trusted_resource_grant(
        extension, "callback_registrations", extension_id=extension_id
    )
    if resources.get("callback_registrations") and callback_grant and project_id:
        lister = getattr(logger, "list_extension_callback_registrations", None)
        if callable(lister):
            snapshots["callback_registrations"] = list(
                lister(extension_id=extension_id, project_id=project_id) or []
            )

    if snapshots:
        runtime["resources"] = snapshots
    return runtime


def _host_envelope_scope(
    host_scope: dict[str, Any], context: dict | None, runtime_context: dict | None
) -> dict[str, Any]:
    """The host scope an envelope carries: account/project/workflow, job and step.

    The four execution fields are always present: a root step has both owners
    null, a call outside any run has all four null. Trusted runtime context wins
    over the caller-supplied operation context.
    """
    runtime = _as_dict(runtime_context)
    caller = _as_dict(context)
    merged = {
        key: runtime[key] if key in runtime else caller.get(key)
        for key in ("job_id", *EXECUTION_SCOPE_FIELDS)
        if key in runtime or key in caller
    }
    job_id = str(merged.get("job_id") or "").strip()
    return {
        **host_scope,
        "job_id": job_id or None,
        **normalize_execution_scope(merged),
    }


def extension_required_scopes(extension: dict) -> list[str]:
    compiled_manifest = _as_dict(extension.get("compiled_manifest"))
    return _normalize_string_list(
        list(_normalize_string_list(extension.get("required_scopes")))
        + list(_normalize_string_list(compiled_manifest.get("required_scopes")))
    )


def operation_required_scopes(
    extension: dict, *, operation_type: str, operation_id: str
) -> list[str]:
    normalized_operation_id = str(operation_id or "").strip()
    normalized_operation_type = str(operation_type or "").strip().lower()
    if not normalized_operation_id or normalized_operation_type not in {"query", "action"}:
        return []
    key = "query_definitions" if normalized_operation_type == "query" else "action_definitions"
    id_field = "query_id" if normalized_operation_type == "query" else "action_id"
    matched_scopes: list[str] = []
    for row in _declared_operation_rows(extension, key):
        row_id = str(row.get(id_field) or row.get("id") or "").strip()
        if row_id != normalized_operation_id:
            continue
        matched_scopes.extend(_normalize_string_list(row.get("required_scopes")))
    return _normalize_string_list(matched_scopes)


def _external_effect_claim_missing(
    extension: dict, operation_id: str, runtime_context: dict | None
) -> bool:
    from core.application.extensions.external_effects import (
        external_effect_declaration_for_operation,
    )

    declaration = external_effect_declaration_for_operation(extension, operation_id)
    if not declaration:
        return False
    runtime = _as_dict(runtime_context)
    if (
        runtime.get("test_mode") is True
        and runtime.get("_host_test_mode_suppression") is True
        and str(declaration.get("test_mode_behavior") or "").strip().lower() == "suppress"
    ):
        return False
    return not str(runtime.get("external_effect_id") or "").strip()


def run_extension_query(
    extension: dict,
    *,
    query_id: str,
    page_id: str = "",
    component_id: str = "",
    context: dict | None = None,
    target: dict | None = None,
    params: dict | None = None,
    timeout_seconds: int | None = None,
    runtime_context: dict | None = None,
    scope: ScopeContext | None = None,
) -> dict:
    availability_failure = extension_runtime_availability_failure(extension)
    if availability_failure is not None:
        return availability_failure

    normalized_query_id = str(query_id or "").strip()
    if not normalized_query_id:
        return {
            "status": "failed",
            "detail": QUERY_ID_REQUIRED_MESSAGE,
            "response": {},
        }
    declared_ids = _declared_operation_ids(extension, "query_definitions", "query_id")
    if declared_ids and normalized_query_id not in declared_ids:
        return {
            "status": "failed",
            "detail": f"Unknown query_id '{normalized_query_id}' for extension",
            "response": {},
        }

    normalized_context, host_scope, principal = _normalize_host_context(context, scope=scope)
    runtime_context = _hydrate_declared_runtime_resources(
        extension,
        operation_type="query",
        operation_id=normalized_query_id,
        context=normalized_context,
        runtime_context=runtime_context,
    )
    request_payload = {
        "contract_version": HOST_CONTRACT_VERSION,
        "mode": "query",
        "host": {
            "contract_version": HOST_CONTRACT_VERSION,
            "operation": "query",
            "scope": _host_envelope_scope(host_scope, normalized_context, runtime_context),
            "principal": principal,
        },
        "query": {
            "query_id": normalized_query_id,
            "page_id": str(page_id or "").strip() or None,
            "component_id": str(component_id or "").strip() or None,
            "context": normalized_context,
            "params": _as_dict(params),
        },
        "target": _as_dict(target),
    }
    handler_mode = _operation_handler_mode(
        extension,
        operation_type="query",
        operation_id=normalized_query_id,
    )
    if handler_mode == "platform":
        return _run_platform_query(
            extension,
            query_id=normalized_query_id,
            page_id=page_id,
            component_id=component_id,
            context=normalized_context,
            params=params,
            timeout_seconds=timeout_seconds,
            runtime_context=runtime_context,
        )
    if handler_mode != "extension":
        return {
            "status": "failed",
            "detail": f"Unsupported query handler mode '{handler_mode}'",
            "response": {},
        }
    from core.infrastructure.db.remote_extension_runtime import (
        is_remote_runtime_enabled,
        remote_query,
    )

    if is_remote_runtime_enabled(extension):
        query_payload = {
            "page_id": str(page_id or "").strip() or None,
            "component_id": str(component_id or "").strip() or None,
            "context": normalized_context,
            "params": _as_dict(params),
            "target": _as_dict(target),
        }
        return remote_query(
            extension,
            query_id=normalized_query_id,
            query_payload=query_payload,
            context=normalized_context,
            runtime_context=_as_dict(runtime_context),
            timeout_seconds=timeout_seconds,
        )
    invocation = run_extension_invocation(
        extension,
        request_payload=request_payload,
        timeout_seconds=timeout_seconds,
        runtime_context=runtime_context,
    )
    invocation["operation_type"] = "query"
    invocation["operation_id"] = normalized_query_id
    return invocation


def run_extension_action(
    extension: dict,
    *,
    action_id: str,
    page_id: str = "",
    component_id: str = "",
    context: dict | None = None,
    target: dict | None = None,
    input_payload: dict | None = None,
    timeout_seconds: float | None = None,
    runtime_context: dict | None = None,
    scope: ScopeContext | None = None,
    artifact_inputs: list[dict] | None = None,
    artifact_outputs: list[dict] | None = None,
    dataset_inputs: list[dict] | None = None,
    dataset_outputs: list[dict] | None = None,
) -> dict:
    availability_failure = extension_runtime_availability_failure(extension)
    if availability_failure is not None:
        return availability_failure

    normalized_action_id = str(action_id or "").strip()
    if not normalized_action_id:
        return {
            "status": "failed",
            "detail": ACTION_ID_REQUIRED_MESSAGE,
            "response": {},
        }
    declared_ids = _declared_operation_ids(extension, "action_definitions", "action_id")
    if declared_ids and normalized_action_id not in declared_ids:
        return {
            "status": "failed",
            "detail": f"Unknown action_id '{normalized_action_id}' for extension",
            "response": {},
        }
    if _external_effect_claim_missing(extension, normalized_action_id, runtime_context):
        return {
            "status": "failed",
            "detail": "external_effect_claim_required",
            "response": {"definitely_no_external_effect": True},
            "errors": [
                {
                    "code": "external_effect_claim_required",
                    "message": "Declared external-effect actions require a host claim",
                }
            ],
        }

    normalized_context, host_scope, principal = _normalize_host_context(context, scope=scope)
    runtime_context = _hydrate_declared_runtime_resources(
        extension,
        operation_type="action",
        operation_id=normalized_action_id,
        context=normalized_context,
        runtime_context=runtime_context,
    )
    request_payload = {
        "contract_version": HOST_CONTRACT_VERSION,
        "mode": "action",
        "host": {
            "contract_version": HOST_CONTRACT_VERSION,
            "operation": "command",
            "scope": _host_envelope_scope(host_scope, normalized_context, runtime_context),
            "principal": principal,
        },
        "action": {
            "action_id": normalized_action_id,
            "page_id": str(page_id or "").strip() or None,
            "component_id": str(component_id or "").strip() or None,
            "context": normalized_context,
            "target": _as_dict(target),
            "input": _as_dict(input_payload),
        },
    }
    handler_mode = _operation_handler_mode(
        extension,
        operation_type="action",
        operation_id=normalized_action_id,
    )
    if handler_mode == "platform":
        return _run_platform_action(
            extension,
            action_id=normalized_action_id,
            page_id=page_id,
            component_id=component_id,
            context=normalized_context,
            target=target,
            input_payload=input_payload,
            timeout_seconds=timeout_seconds,
            runtime_context=runtime_context,
        )
    if handler_mode != "extension":
        return {
            "status": "failed",
            "detail": f"Unsupported action handler mode '{handler_mode}'",
            "response": {},
        }
    from core.infrastructure.db.remote_extension_runtime import (
        is_remote_runtime_enabled,
        remote_action,
    )

    runtime_values = _as_dict(runtime_context)
    from core.infrastructure.extension_runtime.extension_operation_lock import (
        extension_operation_lock_status,
    )

    logger = runtime_values.get("logger")
    serialization = extension_operation_lock_status(
        connection=getattr(logger, "conn", None),
        extension=extension,
        action_id=normalized_action_id,
        input_payload=_as_dict(input_payload),
        runtime_context=runtime_values,
    )

    with serialization as serialization_status:
        if serialization_status != "acquired":
            detail = (
                "operation_serialization_busy"
                if serialization_status == "busy"
                else "operation_serialization_unavailable"
            )
            return {
                "status": "failed",
                "detail": detail,
                "response": {"definitely_no_external_effect": True},
                "errors": [
                    {
                        "code": detail,
                        "message": (
                            "A conflicting extension operation is in progress; retry later."
                            if serialization_status == "busy"
                            else "Extension operation serialization is unavailable; retry later."
                        ),
                    }
                ],
            }
        if is_remote_runtime_enabled(extension):
            action_payload = {
                "page_id": str(page_id or "").strip() or None,
                "component_id": str(component_id or "").strip() or None,
                "context": normalized_context,
                "target": _as_dict(target),
                "input": _as_dict(input_payload),
            }
            return remote_action(
                extension,
                action_id=normalized_action_id,
                input_payload=action_payload,
                context=normalized_context,
                runtime_context=runtime_values,
                timeout_seconds=timeout_seconds,
                artifact_inputs=artifact_inputs,
                artifact_outputs=artifact_outputs,
                dataset_inputs=dataset_inputs,
                dataset_outputs=dataset_outputs,
            )
        invocation = run_extension_invocation(
            extension,
            request_payload=request_payload,
            timeout_seconds=timeout_seconds,
            runtime_context=runtime_context,
            artifact_inputs=artifact_inputs,
            artifact_outputs=artifact_outputs,
            dataset_inputs=dataset_inputs,
            dataset_outputs=dataset_outputs,
        )
        invocation["operation_type"] = "action"
        invocation["operation_id"] = normalized_action_id
        return invocation


def build_runtime_event_envelope(
    *,
    event_type: str,
    account_id: str,
    correlation_id: str,
    severity: str,
    summary: str,
    payload: dict | None = None,
    project_id: int | str | None = None,
    workflow_id: str | None = None,
    job_id: int | str | None = None,
    group_id: str | None = None,
    for_each_id: str | None = None,
    step_id: str | None = None,
    step_execution_id: str | None = None,
    url: str | None = None,
    actor: dict | None = None,
    source: str | None = None,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
    metadata: dict | None = None,
    event_id: str | None = None,
    occurred_at: str | None = None,
) -> dict:
    normalized_urls = {"url": str(url).strip()} if str(url or "").strip() else {}
    return build_event_envelope_v1(
        event_type=event_type,
        account_id=account_id,
        correlation_id=correlation_id,
        severity=severity,
        summary=summary,
        payload=_as_dict(payload),
        project_id=project_id,
        workflow_id=workflow_id,
        job_id=job_id,
        group_id=group_id,
        for_each_id=for_each_id,
        step_id=step_id,
        step_execution_id=step_execution_id,
        source=source,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        actor=_as_dict(actor) or None,
        urls=normalized_urls,
        metadata=_as_dict(metadata),
        event_id=event_id,
        occurred_at=occurred_at,
    )


def build_command_envelope(
    *,
    command_type: str,
    account_id: str,
    actor_type: str,
    actor_id: str,
    target: dict | None = None,
    payload: dict | None = None,
    command_id: str | None = None,
    issued_at: str | None = None,
) -> dict:
    import uuid
    from datetime import datetime

    normalized_command_id = str(command_id or "").strip() or f"cmd_{uuid.uuid4().hex}"
    normalized_issued_at = str(issued_at or "").strip() or datetime.now(UTC).isoformat()
    return {
        "contract_version": HOST_CONTRACT_VERSION,
        "kind": "command",
        "command_id": normalized_command_id,
        "command_type": str(command_type or "").strip(),
        "issued_at": normalized_issued_at,
        "account_id": str(account_id or "").strip(),
        "actor": {
            "type": str(actor_type or "").strip() or "system_actor",
            "id": str(actor_id or "").strip() or "system",
        },
        "target": _as_dict(target),
        "payload": _as_dict(payload),
    }


def emit_extension_event(
    extension: dict,
    *,
    event_type: str,
    correlation_id: str = "",
    scope: ScopeContext | None = None,
    summary: str = "",
    severity: str = "info",
    payload: dict | None = None,
    project_id: int | str | None = None,
    workflow_id: str | None = None,
    job_id: int | str | None = None,
    group_id: str | None = None,
    for_each_id: str | None = None,
    step_id: str | None = None,
    step_execution_id: str | None = None,
    source: str | None = None,
    metadata: dict | None = None,
    runtime_context: dict | None = None,
) -> dict:
    resolved_scope = normalize_scope(scope, project_id=project_id, workflow_id=workflow_id)
    normalized_event_type = str(event_type or "").strip()
    if not normalized_event_type:
        return {"status": "failed", "detail": "event_type is required", "event": {}}
    if not _event_type_allowed_for_extension(extension, normalized_event_type):
        return {
            "status": "failed",
            "detail": f"Event type '{normalized_event_type}' is not declared by extension",
            "event": {},
        }

    runtime = _as_dict(runtime_context)
    remote_invocation = None
    from core.infrastructure.db.remote_extension_runtime import (
        is_remote_runtime_enabled,
        remote_emit_event,
    )

    remote_enabled = is_remote_runtime_enabled(extension)
    if remote_enabled:
        remote_invocation = remote_emit_event(
            extension,
            event_name=normalized_event_type,
            payload=_as_dict(payload),
            context={
                "account_id": resolved_scope.account_id,
                "project_id": resolved_scope.project_id,
                "workflow_id": resolved_scope.workflow_id,
                "job_id": job_id,
                "group_id": group_id,
                "for_each_id": for_each_id,
                "step_id": step_id,
                "step_execution_id": step_execution_id,
            },
            runtime_context=runtime,
        )
        if remote_invocation.get("status") != "succeeded":
            return {
                "status": "failed",
                "detail": str(remote_invocation.get("detail") or "remote event emission failed"),
                "event": {},
                "remote_event": remote_invocation,
            }

    logger_client = runtime.get("logger")
    if logger_client is None:
        # Remote mode can acknowledge event delivery without host logger persistence.
        if remote_enabled and remote_invocation is not None:
            return {
                "status": "accepted",
                "event": {},
                "handler_mode": "remote_https",
                "remote_event": remote_invocation,
            }
        return {"status": "failed", "detail": "logger runtime_context is required", "event": {}}

    extension_id = (
        str(
            extension.get("extension_key")
            or extension.get("extension_id")
            or _as_dict(extension.get("compiled_manifest")).get("extension_id")
            or ""
        ).strip()
        or "extension"
    )
    envelope = build_runtime_event_envelope(
        event_type=normalized_event_type,
        account_id=resolved_scope.account_id,
        correlation_id=str(correlation_id or "").strip(),
        severity=severity,
        summary=summary or normalized_event_type,
        payload=_as_dict(payload),
        project_id=resolved_scope.project_id,
        workflow_id=resolved_scope.workflow_id,
        job_id=job_id,
        group_id=group_id,
        for_each_id=for_each_id,
        step_id=step_id,
        step_execution_id=step_execution_id,
        actor={"type": "extension", "id": extension_id},
        source=str(source or HOST_CONTRACT_VERSION).strip() or HOST_CONTRACT_VERSION,
        aggregate_type="extension",
        aggregate_id=extension_id,
        metadata=_as_dict(metadata),
    )

    recorded = None
    if hasattr(logger_client, "record_domain_event"):
        recorded = logger_client.record_domain_event(
            event_id=str(envelope.get("event_id") or ""),
            event_type=str(envelope.get("event_type") or ""),
            account_id=str(envelope.get("account_id") or ""),
            severity=str(envelope.get("severity") or "info"),
            summary=str(envelope.get("summary") or normalized_event_type),
            payload=_as_dict(envelope.get("payload")),
            actor=_as_dict(envelope.get("actor")),
            urls=_as_dict(envelope.get("urls")),
            envelope=envelope,
            occurred_at=str(envelope.get("occurred_at") or ""),
            project_id=envelope.get("project_id"),
            workflow_id=envelope.get("workflow_id"),
            job_id=envelope.get("job_id"),
            group_id=envelope.get("group_id"),
            for_each_id=envelope.get("for_each_id"),
            step_id=envelope.get("step_id"),
            step_execution_id=envelope.get("step_execution_id"),
            source=str(envelope.get("source") or ""),
            aggregate_type=str(envelope.get("aggregate_type") or ""),
            aggregate_id=str(envelope.get("aggregate_id") or ""),
            correlation_id=str(envelope.get("correlation_id") or ""),
        )
    if hasattr(logger_client, "enqueue_domain_event"):
        logger_client.enqueue_domain_event(
            event_type=str(envelope.get("event_type") or ""),
            event_id=str(envelope.get("event_id") or ""),
            aggregate_type=str(envelope.get("aggregate_type") or "extension"),
            aggregate_id=str(envelope.get("aggregate_id") or extension_id),
            payload=_as_dict(envelope.get("payload")),
            account_id=str(envelope.get("account_id") or ""),
            project_id=envelope.get("project_id"),
            workflow_id=envelope.get("workflow_id"),
            job_id=envelope.get("job_id"),
            severity=str(envelope.get("severity") or "info"),
            summary=str(envelope.get("summary") or normalized_event_type),
            actor=_as_dict(envelope.get("actor")),
            envelope=envelope,
            correlation_id=str(envelope.get("correlation_id") or ""),
        )

    result = {
        "status": "accepted",
        "event": recorded if isinstance(recorded, dict) else envelope,
        "handler_mode": "host",
    }
    if remote_invocation is not None:
        result["remote_event"] = remote_invocation
    return result


def command_scope(command_type: str) -> str:
    normalized = str(command_type or "").strip().lower()
    if not normalized:
        return ""
    return f"command:{normalized}"


def known_command_catalog() -> list[str]:
    return [
        "start_workflow",
        "pause_job",
        "resume_job",
        "cancel_job",
        "retry_step",
        "request_approval",
        "resolve_approval",
        "enqueue_followup_job",
        "invoke_extension_action",
        "update_external_status",
    ]


def normalize_extension_row_for_runtime(row: dict | None) -> dict:
    payload = dict(row or {})
    payload["compiled_manifest"] = _as_dict(
        payload.get("compiled_manifest") or payload.get("compiled_manifest_jsonb")
    )
    payload["query_definitions"] = _as_list(
        payload.get("query_definitions") or payload.get("query_definitions_jsonb")
    )
    payload["action_definitions"] = _as_list(
        payload.get("action_definitions") or payload.get("action_definitions_jsonb")
    )
    payload["compile_errors"] = _as_list(
        payload.get("compile_errors") or payload.get("compile_errors_jsonb")
    )
    payload["compile_warnings"] = _as_list(
        payload.get("compile_warnings") or payload.get("compile_warnings_jsonb")
    )
    payload["validation_errors"] = _as_list(
        payload.get("validation_errors") or payload.get("validation_errors_jsonb")
    )
    payload["validation_warnings"] = _as_list(
        payload.get("validation_warnings") or payload.get("validation_warnings_jsonb")
    )
    payload["required_scopes"] = _normalize_string_list(
        payload.get("required_scopes") or payload.get("required_scopes_jsonb")
    )
    payload["supported_events"] = _normalize_string_list(
        payload.get("supported_events") or payload.get("supported_events_jsonb")
    )
    payload["event_subscriptions"] = _normalize_event_subscriptions(
        payload.get("event_subscriptions")
        or payload.get("event_subscriptions_jsonb")
        or payload["compiled_manifest"].get("event_subscriptions")
    )
    payload["callbacks"] = _as_list(
        payload.get("callbacks")
        or payload.get("callbacks_jsonb")
        or payload["compiled_manifest"].get("callbacks")
    )
    payload["workflow_triggers"] = _as_list(
        payload.get("workflow_triggers")
        or payload.get("workflow_triggers_jsonb")
        or payload["compiled_manifest"].get("workflow_triggers")
    )
    payload["ui_manifest"] = _as_dict(
        payload.get("ui_manifest") or payload.get("ui_manifest_jsonb")
    )
    if not payload["ui_manifest"]:
        payload["ui_manifest"] = _as_dict(payload["compiled_manifest"].get("ui_manifest"))
    payload["machine_auth"] = _as_dict(
        payload.get("machine_auth") or payload.get("machine_auth_jsonb")
    )
    payload["account_scope"] = _as_dict(
        payload.get("account_scope") or payload.get("account_scope_jsonb")
    )
    return payload


__all__ = [
    "extension_required_scopes",
    "operation_required_scopes",
    "run_extension_query",
    "run_extension_action",
    "build_runtime_event_envelope",
    "build_command_envelope",
    "emit_extension_event",
    "command_scope",
    "known_command_catalog",
    "normalize_extension_row_for_runtime",
]
