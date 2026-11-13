"""Run one extension page query, with its project connection when it declares one (ADR 0067)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.principal import coerce_optional_project_id
from core.application.management.command_models import ExtensionQueryResultView
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext, normalize_scope, scope_audit_fields
from core.infrastructure.extension_runtime.extension_invocation_audit import (
    log_host_extension_invocation,
)

from .lifecycle_ports import ERR_EXTENSION_NOT_FOUND
from .operation_support import process_extension_response_envelope
from .operation_targets import (
    _require_direct_action_project_access,
    resolve_operation_connection,
)
from .query_connection_contract import query_connection_type_ids
from .secret_reflection import (
    SecretExposureFinding,
    collect_secret_values,
    find_secret_exposure,
    secret_exposure_invocation,
)

if TYPE_CHECKING:
    from .operation_service import ExtensionOperationService

logger = logging.getLogger(__name__)

#: The longest a query that holds a connection may run, below any timeout_policy.
QUERY_CONNECTION_TIMEOUT_SECONDS = 30
_PROJECT_SCOPED_PRINCIPALS = frozenset({"operator", "viewer", "service_account"})
_PLATFORM_EFFECT_KEYS = ("commands", "events", "project_state_updates")


def _refusal(error: type[ServiceError], code: str, message: str) -> ServiceError:
    return error(message, payload={"error": message, "code": code})


def _query_project_ids(scope: ScopeContext, body: Any) -> list[str]:
    """Every project a query request names, through its scope or its context."""
    context = getattr(body, "context", None)
    named = (
        coerce_optional_project_id(scope.project_id),
        coerce_optional_project_id(
            context.get("project_id") if isinstance(context, dict) else None
        ),
    )
    return sorted({project_id for project_id in named if project_id is not None})


def _declared_query(extension: dict[str, Any], query_id: str) -> dict[str, Any]:
    compiled = extension.get("compiled_manifest")
    compiled = compiled if isinstance(compiled, dict) else {}
    for source in (extension.get("query_definitions"), compiled.get("query_definitions")):
        for row in source if isinstance(source, list) else []:
            if isinstance(row, dict) and str(row.get("query_id") or "").strip() == query_id:
                return row
    return {}


def requested_query_connection_id(target: Any, *, connection_types: frozenset[str]) -> str:
    """The connection a query request names; refuses a target it may not carry."""
    target = target if isinstance(target, dict) else {}
    if not target:
        if connection_types:
            raise _refusal(
                ServiceValidationError,
                "query_connection_required",
                "This query runs with a connection: send target.connection_id",
            )
        return ""
    connection_id = target.get("connection_id")
    if (
        not connection_types
        or set(target) != {"connection_id"}
        or not isinstance(connection_id, str)
        or not connection_id.strip()
    ):
        raise _refusal(
            ServiceValidationError,
            "query_target_invalid",
            "A query target is only {connection_id} and only for a query that declares a connection",
        )
    return connection_id.strip()


def _platform_effects(invocation: dict[str, Any]) -> list[str]:
    response = invocation.get("response")
    response = response if isinstance(response, dict) else {}
    result = response.get("result")
    result = result if isinstance(result, dict) else {}
    return [
        key
        for key in _PLATFORM_EFFECT_KEYS
        if response.get(key) or (key == "project_state_updates" and result.get(key))
    ]


def _effects_refused_invocation(invocation: dict[str, Any], effects: list[str]) -> dict[str, Any]:
    message = "A query that runs with a connection only reads; its response asked for " + (
        ", ".join(effects)
    )
    return {
        "status": "failed",
        "detail": "query_connection_effects_forbidden",
        "operation_type": invocation.get("operation_type"),
        "operation_id": invocation.get("operation_id"),
        "response": {
            "ok": False,
            "error_code": "query_connection_effects_forbidden",
            "error": message,
            "errors": [{"code": "query_connection_effects_forbidden", "message": message}],
        },
    }


def _invocation_error_code(invocation: dict[str, Any]) -> str:
    response = invocation.get("response")
    response = response if isinstance(response, dict) else {}
    return str(response.get("error_code") or invocation.get("detail") or "").strip()


def _is_remote_runtime(extension: dict[str, Any]) -> bool:
    from core.infrastructure.db.remote_extension_runtime import is_remote_runtime_enabled

    return bool(is_remote_runtime_enabled(extension))


def execute_extension_query(
    service: ExtensionOperationService,
    extension_id: str,
    query_id: str,
    body: Any,
    *,
    scope: ScopeContext | None = None,
) -> ExtensionQueryResultView:
    resolved_scope = normalize_scope(scope)
    extension, bl = service._resolve_extension_runtime_record(extension_id)
    if not extension:
        raise ServiceNotFoundError(ERR_EXTENSION_NOT_FOUND)
    service._assert_operation_within_extension_scope(
        extension, operation_type="query", operation_id=query_id
    )
    handler_mode = service._extension_operation_handler_mode(
        extension, operation_type="query", operation_id=query_id
    )
    if extension.get("enabled") is False and handler_mode == "extension":
        from .operation_service import ERR_EXTENSION_DISABLED

        raise ServiceConflictError(ERR_EXTENSION_DISABLED)

    # The project rule of actions: a project-scoped principal names its project,
    # and every project the request names is one the caller reaches, before any
    # runtime resource (a connection row, provider secrets) is resolved for it.
    project_ids = _query_project_ids(resolved_scope, body)
    principal_type = str(resolved_scope.principal_type or "").strip().lower()
    if principal_type in _PROJECT_SCOPED_PRINCIPALS and not project_ids:
        raise _refusal(
            ServiceForbiddenError,
            "project_context_required",
            "Project-scoped extension query requires project context",
        )
    authorized_project_ids: set[str] = set()
    for project_id in project_ids:
        _require_direct_action_project_access(
            bl, project_id, resolved_scope, get_project_by_id=service._get_project_by_id
        )
        authorized_project_ids.add(project_id)

    connection_types = query_connection_type_ids(_declared_query(extension, query_id))
    connection_id = requested_query_connection_id(
        getattr(body, "target", None), connection_types=connection_types
    )

    audit = scope_audit_fields(scope)
    runtime_ctx: dict[str, Any] = {**audit}
    if bl is not None:
        runtime_ctx["logger"] = bl
    target: dict[str, Any] = {}
    hydrated_secrets: frozenset[str] = frozenset()
    if connection_types:
        if handler_mode != "extension" or _is_remote_runtime(extension):
            raise _refusal(
                ServiceConflictError,
                "query_connection_runtime_unsupported",
                "A query that runs with a connection needs the extension's local runtime",
            )
        if len(project_ids) != 1:
            raise _refusal(
                ServiceValidationError,
                "project_context_required",
                "A query that runs with a connection needs one project context",
            )
        resolved = resolve_operation_connection(
            bl,
            connection_id=connection_id,
            project_id=project_ids[0],
            scope=resolved_scope,
            extension=extension,
            extension_id=extension_id,
            account_id=str(audit.get("account_id") or DEFAULT_ACCOUNT_ID).strip()
            or DEFAULT_ACCOUNT_ID,
            required_connection_type_ids=connection_types,
            get_project_by_id=service._get_project_by_id,
            authorized_project_ids=authorized_project_ids,
        )
        target = {"connection_id": resolved.connection_id, "connection": resolved.connection}
        audit = {**audit, **resolved.audit_fields()}
        runtime_ctx.update(resolved.runtime_context())
        hydrated_secrets, budget_exceeded = collect_secret_values(
            resolved.connection.get("secrets")
        )
        if budget_exceeded:
            invocation = secret_exposure_invocation(
                SecretExposureFinding.check_incomplete("secret_inventory")
            )
            raise ServiceError(
                "Extension query failed",
                status_code=service._extension_operation_failure_status(invocation),
                payload={
                    "extension_id": extension_id,
                    "query_id": query_id,
                    "code": _invocation_error_code(invocation),
                    "query": invocation,
                },
            )

    from core.infrastructure.extension_runtime import extension_runtime

    run_extension_query = cast(
        Callable[..., dict[str, Any]], cast(Any, extension_runtime).run_extension_query
    )
    invocation = run_extension_query(
        extension,
        query_id=query_id,
        page_id=body.page_id,
        component_id=body.component_id,
        context=body.context,
        target=target or None,
        params=body.params,
        timeout_seconds=body.timeout_seconds,
        timeout_ceiling_seconds=QUERY_CONNECTION_TIMEOUT_SECONDS if connection_types else None,
        runtime_context=runtime_ctx,
        scope=scope,
    )
    if connection_types:
        # Before the invocation is logged or returned: the result may echo a
        # secret of the connection the host just handed to the extension.
        finding = find_secret_exposure(invocation, hydrated_secrets)
        if finding is not None:
            invocation = secret_exposure_invocation(finding)
        elif invocation.get("status") == "succeeded" and (effects := _platform_effects(invocation)):
            invocation = _effects_refused_invocation(invocation, effects)
    log_host_extension_invocation(
        bl=bl,
        extension=extension,
        extension_id=extension_id,
        invocation_type=f"query:{query_id}",
        request=body.model_dump(),
        invocation=invocation,
        runtime_context=runtime_ctx,
        request_context={"source": "extension_binding_service.execute_query"},
        query_id=query_id,
    )
    if invocation.get("status") != "succeeded":
        payload: dict[str, Any] = {
            "extension_id": extension_id,
            "query_id": query_id,
            "query": invocation,
        }
        if connection_types and (code := _invocation_error_code(invocation)):
            payload["code"] = code
        raise ServiceError(
            "Extension query failed",
            status_code=service._extension_operation_failure_status(invocation),
            payload=payload,
        )
    if not connection_types:
        # Host contract v1.1: normalize the response and process its commands and
        # events. A query with a connection was refused above if it asked for any.
        process_extension_response_envelope(
            invocation,
            extension,
            extension_id,
            scope=scope,
            audit=audit,
            bl=bl,
            get_logger=service._get_operation_store,
            event_command_service_factory=service._event_command_service_factory,
            command_dispatch_service_factory=service._command_dispatch_service_factory,
        )
    return ExtensionQueryResultView(extension_id=extension_id, query_id=query_id, query=invocation)


__all__ = [
    "QUERY_CONNECTION_TIMEOUT_SECONDS",
    "execute_extension_query",
    "requested_query_connection_id",
]
