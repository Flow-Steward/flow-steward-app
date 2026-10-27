from __future__ import annotations

import contextlib
import copy
import logging
from collections.abc import Callable
from typing import Any, cast

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.principal import coerce_optional_project_id
from core.application.management.command_models import (
    ExtensionActionResultView,
    ExtensionQueryResultView,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.guards import require_project_access
from core.application.management.scope import ScopeContext, normalize_scope, scope_audit_fields
from core.infrastructure.extension_runtime.extension_invocation_audit import (
    log_host_extension_invocation,
)

from .lifecycle_ports import ERR_EXTENSION_NOT_FOUND
from .operation_audit import _log_action_invocation
from .operation_context import ExtensionOperationHandlerMode as ExtensionOperationHandlerMode
from .operation_context import _ActionExecutionContext
from .operation_effects import (
    _claim_action_external_effect,
    _direct_action_intent_value,
    _mark_action_external_effect_running,
)
from .operation_projection import (
    _CLIENT_ACTION_OUTPUT_KEYS,
    _MESSAGE_EXTENSION_ACTION_FAILED,
    _canonical_action_status,
    _client_action_invocation,
)
from .operation_projection import action_audit_policy as action_audit_policy
from .operation_projection import contract_action_input_names as contract_action_input_names
from .operation_support import (
    ExtensionCommandDispatchServiceFactory,
    ExtensionEventCommandServiceFactory,
    extension_declared_scopes,
    extension_operation_contract,
    extension_operation_declared_scopes,
    process_extension_response_envelope,
)
from .operation_targets import (
    _direct_action_connection_target,
    _direct_action_project_id,
    _load_direct_action_connection_row,
    _require_direct_action_project_access,
    _target_connection_id,
    _target_connection_id_for_external_effect,
)
from .secret_reflection import (
    collect_secret_values as _collect_secret_values,
)
from .secret_reflection import (
    secret_exposure_invocation as _secret_exposure_invocation,
)
from .secret_reflection import (
    value_exposes_secret as _invocation_exposes_secret,
)

ERR_EXTENSION_DISABLED = "Extension is disabled"


logger = logging.getLogger(__name__)


_HOST_TEST_MODE_SUPPRESSION_FLAG = "_host_test_mode_suppression"


def _hydrated_action_secret_values(
    context: _ActionExecutionContext,
) -> tuple[frozenset[str], bool]:
    connection = context.target.get("connection")
    if not isinstance(connection, dict):
        return frozenset(), False
    return _collect_secret_values(connection.get("secrets"))


_GRANT_REQUEST_FIELDS = (
    "artifact_inputs",
    "artifact_outputs",
    "artifact_output_specs",
    "dataset_inputs",
    "dataset_outputs",
    "dataset_output_specs",
)


def _action_requests_grants(body: Any) -> bool:
    return any(getattr(body, field, None) for field in _GRANT_REQUEST_FIELDS)


def _is_suppressed_test_mode(context: _ActionExecutionContext) -> bool:
    if context.body.context.get("test_mode") is not True:
        return False
    from core.application.extensions.external_effects import (
        external_effect_declaration_for_operation,
    )

    declaration = external_effect_declaration_for_operation(
        context.extension,
        context.action_id,
    )
    return str(declaration.get("test_mode_behavior") or "").strip().lower() == "suppress"


class ExtensionOperationService:
    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        get_operation_store: Callable[[], Any],
        resolve_extension_runtime_record: Callable[[str], tuple[dict[str, Any] | None, Any]],
        extension_operation_handler_mode: ExtensionOperationHandlerMode,
        extension_operation_failure_status: Callable[[dict[str, Any] | None], int],
        get_project_by_id: Callable[[str], Any] | None = None,
        event_command_service_factory: ExtensionEventCommandServiceFactory | None = None,
        command_dispatch_service_factory: ExtensionCommandDispatchServiceFactory | None = None,
        artifact_plane: Any | None = None,
        get_artifact_plane: Callable[[], Any] | None = None,
    ) -> None:
        self._get_logger: Callable[[], Any] = get_logger
        #: The store operations persist through. Separate from the lifecycle
        #: store on purpose: that one resolves installed extensions and provider
        #: secrets and exposes nothing about project connections, notification
        #: routes or ingress, so every save an action made answered
        #: `project_connection_storage_unavailable` in production.
        self._get_operation_store: Callable[[], Any] = get_operation_store
        self._get_project_by_id = get_project_by_id
        self._resolve_extension_runtime_record: Callable[
            [str], tuple[dict[str, Any] | None, Any]
        ] = resolve_extension_runtime_record
        self._extension_operation_handler_mode: ExtensionOperationHandlerMode = (
            extension_operation_handler_mode
        )
        self._extension_operation_failure_status: Callable[[dict[str, Any] | None], int] = (
            extension_operation_failure_status
        )
        self._event_command_service_factory = event_command_service_factory
        self._command_dispatch_service_factory = command_dispatch_service_factory
        self._artifact_plane: Any | None = artifact_plane
        #: Resolved on the first action that asks for a grant, so composing the
        #: service never builds storage (the local backend creates its root
        #: directory) for commands that never touch an artifact.
        self._get_artifact_plane = get_artifact_plane

    @staticmethod
    def _assert_operation_within_extension_scope(
        extension: dict, *, operation_type: str, operation_id: str
    ) -> None:
        """Enforce that an operation only requires scopes the extension declared.

        ``required_scopes`` on the extension record is the operator-approved scope
        envelope. An operation that declares a scope outside that envelope must not
        execute, so a compromised/over-reaching operation cannot exceed what the
        extension was granted at install time.
        """
        declared = extension_declared_scopes(extension)
        needed = extension_operation_declared_scopes(
            extension, operation_type=operation_type, operation_id=operation_id
        )
        missing = needed - declared
        if missing:
            raise ServiceForbiddenError(
                "Extension operation requires scopes not granted to the extension: "
                + ", ".join(sorted(missing))
            )

    def _prepare_action_execution(
        self,
        extension_id: str,
        action_id: str,
        body: Any,
        scope: ScopeContext | None,
        idempotency_key: str = "",
    ) -> _ActionExecutionContext:
        resolved_scope = normalize_scope(scope)
        extension, bl = self._resolve_extension_runtime_record(extension_id)
        if not extension:
            raise ServiceNotFoundError(ERR_EXTENSION_NOT_FOUND)
        self._assert_operation_within_extension_scope(
            extension, operation_type="action", operation_id=action_id
        )

        requested_project_id = coerce_optional_project_id(resolved_scope.project_id)
        principal_type = str(resolved_scope.principal_type or "").strip().lower()
        if principal_type in {"operator", "viewer", "service_account"} and (
            requested_project_id is None
        ):
            raise ServiceForbiddenError("Project-scoped extension action requires project context")
        authorized_project_ids: set[str] = set()
        if requested_project_id is not None:
            project_getter = self._get_project_by_id or getattr(bl, "get_project", None)
            project = project_getter(requested_project_id) if callable(project_getter) else None
            if not isinstance(project, dict) or not project:
                raise ServiceNotFoundError(f"Project '{requested_project_id}' not found")
            require_project_access(resolved_scope, project)
            authorized_project_ids.add(requested_project_id)

        handler_mode = self._extension_operation_handler_mode(
            extension, operation_type="action", operation_id=action_id
        )
        if extension.get("enabled") is False and handler_mode == "extension":
            raise ServiceConflictError(ERR_EXTENSION_DISABLED)

        target = copy.deepcopy(body.target or {})
        if body.scope and not target.get("scope"):
            target["scope"] = body.scope
        if body.item_ids and not target.get("item_ids"):
            target["item_ids"] = list(body.item_ids)
        contract_input_names = self._contract_action_input_names(extension, action_id)
        input_payload: dict[str, Any] = {}
        input_payload.update(copy.deepcopy(body.values or {}))
        input_payload.update(copy.deepcopy(body.data or {}))
        input_payload.update(copy.deepcopy(body.input or {}))
        if contract_input_names is not None:
            input_payload = {
                key: value for key, value in input_payload.items() if key in contract_input_names
            }
            connection_id = _target_connection_id(target)
            if "connection_ref" in contract_input_names and connection_id:
                input_payload["connection_ref"] = connection_id

        from core.application.extensions.external_effects import (
            external_effect_declaration_for_operation,
        )

        direct_action_intent: dict[str, Any] = {}
        if external_effect_declaration_for_operation(extension, action_id):
            direct_action_intent = _direct_action_intent_value(
                {
                    "page_id": str(body.page_id or ""),
                    "component_id": str(body.component_id or ""),
                    "context": copy.deepcopy(body.context or {}),
                    "target": copy.deepcopy(target),
                    "input": copy.deepcopy(input_payload),
                    "artifact_inputs": copy.deepcopy(body.artifact_inputs or []),
                    "artifact_outputs": copy.deepcopy(body.artifact_outputs or []),
                    "artifact_output_specs": copy.deepcopy(body.artifact_output_specs or []),
                    "dataset_inputs": copy.deepcopy(body.dataset_inputs or []),
                    "dataset_outputs": copy.deepcopy(body.dataset_outputs or []),
                    "dataset_output_specs": copy.deepcopy(body.dataset_output_specs or []),
                }
            )

        audit = scope_audit_fields(scope)
        runtime_ctx: dict[str, Any] = {**audit}
        tenant_id = str(resolved_scope.tenant_id or "").strip()
        if tenant_id:
            runtime_ctx["tenant_id"] = tenant_id
        if bl is not None:
            runtime_ctx["logger"] = bl
        extension_install_id = str(
            extension.get("extension_install_id") or extension.get("install_id") or ""
        ).strip()
        if extension_install_id:
            runtime_ctx["extension_install_id"] = extension_install_id
        context = _ActionExecutionContext(
            extension_id=extension_id,
            action_id=action_id,
            body=body,
            scope=scope,
            resolved_scope=resolved_scope,
            extension=extension,
            bl=bl,
            target=target,
            input_payload=input_payload,
            audit=audit,
            runtime_ctx=runtime_ctx,
            direct_action_idempotency_key=str(idempotency_key or "").strip(),
            direct_action_intent=direct_action_intent,
            authorized_project_ids=authorized_project_ids,
        )
        from .browser_authority_approval import browser_bootstrap_action_ids

        compiled_manifest = extension.get("compiled_manifest")
        trusted_manifest = compiled_manifest if isinstance(compiled_manifest, dict) else {}
        context.browser_bootstrap = action_id in browser_bootstrap_action_ids(trusted_manifest)
        runtime_extension_id = str(
            extension.get("extension_key") or extension.get("extension_id") or extension_id
        ).strip()
        operation_contract = extension_operation_contract(
            context.extension,
            operation_id=action_id,
        )
        context.required_connection_type_ids = operation_contract.connection_type_ids
        context.required_oauth_scopes = operation_contract.required_oauth_scopes
        context.suppressed_test_mode = _is_suppressed_test_mode(context)
        if context.suppressed_test_mode:
            if not extension_install_id:
                raise ServiceValidationError(
                    "extension_install_id is required for extension actions"
                )
            connection_id = _target_connection_id_for_external_effect(context.target)
            if not connection_id:
                raise ServiceValidationError(
                    "target.connection_id is required for external-effect actions"
                )
            context.target = {"connection_id": connection_id}
            context.audit = {**context.audit, "connection_id": connection_id}
            context.runtime_ctx.update(
                {
                    "connection_id": connection_id,
                    "test_mode": True,
                    _HOST_TEST_MODE_SUPPRESSION_FLAG: True,
                }
            )
            connection = _load_direct_action_connection_row(
                context.bl,
                connection_id=connection_id,
                account_id=str(context.audit.get("account_id") or DEFAULT_ACCOUNT_ID).strip()
                or DEFAULT_ACCOUNT_ID,
                project_id=_direct_action_project_id(
                    scope=context.resolved_scope,
                    context=dict(context.body.context or {}),
                    target=context.target,
                ),
                extension_id=runtime_extension_id,
                extension_install_id=extension_install_id,
            )
            if not connection:
                raise ServiceValidationError("connection_runtime_requirement_failed")
            granted_scopes = connection.get("oauth_granted_scopes")
            normalized_granted_scopes = (
                {
                    str(value).strip()
                    for value in granted_scopes
                    if isinstance(value, str) and str(value).strip()
                }
                if isinstance(granted_scopes, list)
                else set()
            )
            if (
                context.required_connection_type_ids
                and str(connection.get("connection_type") or "").strip()
                not in context.required_connection_type_ids
            ) or not set(context.required_oauth_scopes).issubset(normalized_granted_scopes):
                raise ServiceValidationError("connection_runtime_requirement_failed")
            return context
        self._resolve_action_connection(context)
        self._hydrate_action_project_state(context)
        return context

    @staticmethod
    def _contract_action_input_names(
        extension: dict[str, Any], action_id: str
    ) -> frozenset[str] | None:
        return contract_action_input_names(extension, action_id)

    def _hydrate_action_project_state(self, context: _ActionExecutionContext) -> None:
        from core.application.extensions.project_state_hydration import (
            load_extension_project_state_for_target,
        )

        project_id = _direct_action_project_id(
            scope=context.resolved_scope,
            context=dict(context.body.context or {}),
            target=context.target,
        )
        if not project_id:
            return
        self._require_action_project_access(context, project_id)
        context.audit = {**context.audit, "project_id": project_id}
        context.runtime_ctx["project_id"] = project_id
        extension_install_id = str(
            context.extension.get("extension_install_id")
            or context.extension.get("install_id")
            or ""
        ).strip()
        if not extension_install_id:
            return
        project_state = load_extension_project_state_for_target(
            context.bl,
            account_id=str(context.audit.get("account_id") or DEFAULT_ACCOUNT_ID).strip()
            or DEFAULT_ACCOUNT_ID,
            project_id=project_id,
            extension_id=context.extension_id,
            extension_install_id=extension_install_id,
            connection_id=_target_connection_id(context.target),
            extension=context.extension,
            input_payload=context.input_payload,
        )
        if project_state:
            context.target["project_state"] = project_state

    def _resolve_action_connection(self, context: _ActionExecutionContext) -> None:
        from core.application.extensions import external_effects

        declaration = external_effects.external_effect_declaration_for_operation(
            context.extension, context.action_id
        )
        connection_id = _target_connection_id(context.target)
        if not declaration and not connection_id:
            return

        project_id = _direct_action_project_id(
            scope=context.resolved_scope,
            context=dict(context.body.context or {}),
            target=context.target,
        )
        if not project_id:
            message = (
                "target.connection_id for an external-effect action requires project context"
                if declaration
                else "target.connection_id for an extension action requires project context"
            )
            raise ServiceValidationError(message)
        self._require_action_project_access(context, project_id)
        extension_install_id = str(
            context.extension.get("extension_install_id")
            or context.extension.get("install_id")
            or ""
        ).strip()
        if not extension_install_id:
            raise ServiceValidationError("extension_install_id is required for extension actions")
        if declaration:
            connection_id = _target_connection_id_for_external_effect(context.target)
            if not connection_id:
                raise ServiceValidationError(
                    "target.connection_id is required for external-effect actions"
                )
        runtime_connection = _direct_action_connection_target(
            context.bl,
            connection_id=connection_id,
            account_id=str(context.audit.get("account_id") or DEFAULT_ACCOUNT_ID).strip()
            or DEFAULT_ACCOUNT_ID,
            project_id=project_id,
            extension_id=context.extension_id,
            extension_install_id=extension_install_id,
            required_connection_type_ids=context.required_connection_type_ids,
            required_oauth_scopes=context.required_oauth_scopes,
            browser_safe=context.browser_bootstrap,
            get_project_by_id=self._get_project_by_id,
        )
        context.target = {
            **context.target,
            "connection_id": runtime_connection["connection_id"],
            "connection": runtime_connection,
        }
        context.audit = {
            **context.audit,
            "project_id": project_id,
            "connection_id": runtime_connection["connection_id"],
        }
        context.runtime_ctx.update(
            {
                "project_id": project_id,
                "connection_id": runtime_connection["connection_id"],
                "extension_install_id": extension_install_id,
                "oauth_configuration_id": str(
                    runtime_connection.get("oauth_configuration_id") or ""
                ).strip(),
                "oauth_provider_id": str(runtime_connection.get("oauth_provider_id") or "").strip(),
            }
        )

    def _require_action_project_access(
        self,
        context: _ActionExecutionContext,
        project_id: str,
    ) -> None:
        if project_id in context.authorized_project_ids:
            return
        _require_direct_action_project_access(
            context.bl,
            project_id,
            context.resolved_scope,
            get_project_by_id=self._get_project_by_id,
        )
        context.authorized_project_ids.add(project_id)

    def _prepare_action_provider_invocation(self, context: _ActionExecutionContext) -> None:
        from core.application.extensions import runtime_grants

        body = context.body
        if self._artifact_plane is not None and not context.suppressed_test_mode:

            def register_marker(prepared_grants: Any) -> None:
                if not context.external_effect_id:
                    return
                from core.application.extensions.external_effects import (
                    register_external_effect_output_reconciliation,
                )

                register_external_effect_output_reconciliation(
                    context.bl,
                    effect_id=context.external_effect_id,
                    account_id=str(context.audit.get("account_id") or "").strip(),
                    claim_token=context.external_effect_claim_token,
                    prepared_grants=prepared_grants,
                )

            context.prepared_grants = runtime_grants.prepare_extension_grants(
                plane=self._artifact_plane,
                scope=context.resolved_scope,
                audit=context.audit,
                extension_id=context.extension_id,
                artifact_inputs=list(getattr(body, "artifact_inputs", None) or []),
                artifact_outputs=list(getattr(body, "artifact_outputs", None) or []),
                artifact_output_specs=list(getattr(body, "artifact_output_specs", None) or []),
                dataset_inputs=list(getattr(body, "dataset_inputs", None) or []),
                dataset_outputs=list(getattr(body, "dataset_outputs", None) or []),
                dataset_output_specs=list(getattr(body, "dataset_output_specs", None) or []),
                before_materialize_outputs=register_marker,
            )
        if (
            self._extension_operation_handler_mode(
                context.extension,
                operation_type="action",
                operation_id=context.action_id,
            )
            == "platform"
        ):
            from core.application.extensions.platform_connection_operations import (
                install_platform_connection_operation,
            )

            install_platform_connection_operation(context.runtime_ctx)

    def _require_artifact_plane(self) -> Any:
        if self._artifact_plane is None and self._get_artifact_plane is not None:
            self._artifact_plane = self._get_artifact_plane()
        if self._artifact_plane is None:
            # Accepting the grant fields and dropping them made the extension
            # fail later with "Artifact descriptor not found", far from the cause.
            raise ServiceUnavailableError("Artifact grants are not available for extension actions")
        return self._artifact_plane

    def _invoke_action_with_grants(self, context: _ActionExecutionContext) -> dict[str, Any]:
        from core.infrastructure.extension_runtime import extension_runtime

        runtime_facade = cast(Any, extension_runtime)
        run_extension_action = cast(
            Callable[..., dict[str, Any]], runtime_facade.run_extension_action
        )

        body = context.body
        prepared_grants = context.prepared_grants

        def invoke() -> dict[str, Any]:
            return run_extension_action(
                context.extension,
                action_id=context.action_id,
                page_id=body.page_id,
                component_id=body.component_id,
                context=body.context,
                target=context.target,
                input_payload=context.input_payload,
                timeout_seconds=body.timeout_seconds,
                runtime_context=context.runtime_ctx,
                scope=context.scope,
                artifact_inputs=(prepared_grants.artifact_inputs if prepared_grants else None)
                or None,
                artifact_outputs=(prepared_grants.artifact_outputs if prepared_grants else None)
                or None,
                dataset_inputs=(prepared_grants.dataset_inputs if prepared_grants else None)
                or None,
                dataset_outputs=(prepared_grants.dataset_outputs if prepared_grants else None)
                or None,
            )

        return invoke()

    def _finalize_action_grants(
        self,
        context: _ActionExecutionContext,
        invocation: dict[str, Any],
    ) -> dict[str, Any]:
        from core.application.extensions import runtime_grants

        prepared_grants = context.prepared_grants
        if not (
            self._artifact_plane is not None
            and prepared_grants is not None
            and (prepared_grants.artifact_output_ids or prepared_grants.dataset_output_ids)
            and invocation.get("status") == "succeeded"
        ):
            return invocation
        finalized = runtime_grants.finalize_prepared_extension_grants(
            self._artifact_plane,
            prepared_grants,
        )
        if finalized.errors:
            return {
                **invocation,
                "status": "failed",
                "detail": str(finalized.errors[0].get("code") or "grant_upload_failed"),
                "errors": finalized.errors,
            }

        grant_outputs: dict[str, Any] = {}
        for descriptor in prepared_grants.artifact_outputs:
            binding_key = str(descriptor.get("binding_key") or "").strip()
            handle = str(descriptor.get("artifact_handle") or "").strip()
            if binding_key and handle:
                grant_outputs[binding_key] = handle
        for descriptor in prepared_grants.dataset_outputs:
            binding_key = str(descriptor.get("binding_key") or "").strip()
            handle = str(descriptor.get("dataset_handle") or "").strip()
            if binding_key and handle:
                grant_outputs[binding_key] = handle
        invocation["produced_artifacts"] = finalized.produced_artifacts
        invocation["produced_datasets"] = finalized.produced_datasets
        invocation["grant_outputs"] = grant_outputs
        return invocation

    def _process_action_response(
        self,
        context: _ActionExecutionContext,
        invocation: dict[str, Any],
    ) -> dict[str, Any]:
        if invocation.get("status") != "succeeded":
            return invocation
        try:
            process_extension_response_envelope(
                invocation,
                context.extension,
                context.extension_id,
                scope=context.scope,
                audit=context.audit,
                bl=context.bl,
                get_logger=self._get_operation_store,
                event_command_service_factory=self._event_command_service_factory,
                command_dispatch_service_factory=self._command_dispatch_service_factory,
            )
        except Exception as exc:
            return {
                **invocation,
                "status": "failed",
                "detail": "response_envelope_processing_failed",
                "errors": [
                    *list(invocation.get("errors") or []),
                    {
                        "code": "response_envelope_processing_failed",
                        "message": str(exc),
                    },
                ],
            }
        return invocation

    def _cleanup_prepared_action_grants(
        self,
        context: _ActionExecutionContext,
        *,
        log_message: str,
    ) -> None:
        if self._artifact_plane is None or context.prepared_grants is None:
            return
        try:
            from core.application.extensions import runtime_grants

            runtime_grants.cleanup_prepared_extension_grants(
                self._artifact_plane,
                context.prepared_grants,
            )
        except Exception as exc:
            logger.warning(log_message, exc)

    def _cleanup_action_resources(
        self,
        context: _ActionExecutionContext,
        invocation: dict[str, Any],
        *,
        confirm_external_effect: bool = True,
        protected_values: frozenset[str] = frozenset(),
    ) -> None:
        if (
            self._artifact_plane is not None
            and context.prepared_grants is not None
            and invocation.get("status") != "succeeded"
        ):
            self._cleanup_prepared_action_grants(
                context,
                log_message="failed to cleanup prepared extension output grants: %s",
            )
            invocation.pop("produced_artifacts", None)
            invocation.pop("produced_datasets", None)
            invocation.pop("grant_outputs", None)

        if not confirm_external_effect or not context.external_effect_id:
            return
        try:
            from core.application.extensions.external_effects import (
                apply_external_effect_confirmation,
                confirm_external_effect_from_invocation,
                public_confirmed_external_effect,
            )

            prepared_grants = context.prepared_grants
            has_host_finalized_outputs = bool(
                invocation.get("status") == "succeeded"
                and self._artifact_plane is not None
                and prepared_grants is not None
                and (prepared_grants.artifact_output_ids or prepared_grants.dataset_output_ids)
            )
            host_outputs = (
                {key: copy.deepcopy(invocation.get(key)) for key in _CLIENT_ACTION_OUTPUT_KEYS}
                if has_host_finalized_outputs
                else {}
            )
            confirmed_effect = confirm_external_effect_from_invocation(
                context.bl,
                effect_id=context.external_effect_id,
                invocation=invocation,
                claim_token=context.external_effect_claim_token,
                account_id=str(context.audit.get("account_id") or "").strip(),
                extension=context.extension,
                operation_id=context.action_id,
                protected_values=protected_values,
                host_outputs=host_outputs,
                expected_project_id=str(context.audit.get("project_id") or "").strip() or None,
                expected_connection_id=str(
                    context.audit.get("connection_id")
                    or _target_connection_id(context.target)
                    or ""
                ).strip()
                or None,
            )
            if confirmed_effect:
                checked_invocation = apply_external_effect_confirmation(
                    invocation,
                    confirmed_effect,
                )
                context.external_effect = public_confirmed_external_effect(confirmed_effect)
                if checked_invocation is not invocation:
                    invocation.clear()
                    invocation.update(checked_invocation)
        except Exception as exc:
            logger.warning("failed to confirm external effect: %s", exc)
            if getattr(exc, "commit_outcome", "unknown") == "rolled_back":
                self._cleanup_prepared_action_grants(
                    context,
                    log_message=(
                        "failed to cleanup rollback-confirmed extension output grants: %s"
                    ),
                )
            invocation.pop("produced_artifacts", None)
            invocation.pop("produced_datasets", None)
            invocation.pop("grant_outputs", None)
            from core.application.extensions.external_effects import (
                external_effect_confirmation_failure_invocation,
            )

            invocation.clear()
            invocation.update(external_effect_confirmation_failure_invocation(exc))

    def _prepare_action_invocation(self, context: _ActionExecutionContext) -> None:
        try:
            self._prepare_action_provider_invocation(context)
        except Exception as exc:
            self._cleanup_prepared_action_grants(
                context,
                log_message="failed to cleanup pre-spawn extension output grants: %s",
            )
            if context.external_effect_id:
                from core.application.extensions.external_effects import (
                    fail_external_effect_before_spawn,
                )

                with contextlib.suppress(Exception):
                    fail_external_effect_before_spawn(
                        context.bl,
                        effect_id=context.external_effect_id,
                        claim_token=context.external_effect_claim_token,
                        code="external_effect_pre_spawn_setup_failed",
                    )
            raise ServiceError(
                "Extension action failed before provider invocation",
                status_code=503,
                payload={
                    "extension_id": context.extension_id,
                    "action_id": context.action_id,
                    "action": {
                        "status": "failed",
                        "detail": "external_effect_pre_spawn_setup_failed",
                    },
                },
            ) from exc

    def _mark_action_running(self, context: _ActionExecutionContext) -> None:
        if context.suppressed_test_mode:
            return
        try:
            _mark_action_external_effect_running(context)
        except Exception:
            self._cleanup_prepared_action_grants(
                context,
                log_message="failed to cleanup lease-rejected extension output grants: %s",
            )
            if context.external_effect_id:
                from core.application.extensions.external_effects import (
                    fail_external_effect_before_spawn,
                )

                with contextlib.suppress(Exception):
                    fail_external_effect_before_spawn(
                        context.bl,
                        effect_id=context.external_effect_id,
                        claim_token=context.external_effect_claim_token,
                        code="external_effect_pre_spawn_setup_failed",
                    )
            raise

    def execute_action(
        self,
        extension_id: str,
        action_id: str,
        body: Any,
        *,
        scope: ScopeContext | None = None,
        idempotency_key: str = "",
    ) -> ExtensionActionResultView:
        context = self._prepare_action_execution(
            extension_id,
            action_id,
            body,
            scope,
            idempotency_key,
        )
        if not context.suppressed_test_mode and _action_requests_grants(context.body):
            # Before any external effect is claimed, so the caller sees why.
            self._require_artifact_plane()
        hydrated_secrets, secret_budget_exceeded = _hydrated_action_secret_values(context)
        if secret_budget_exceeded:
            invocation = _secret_exposure_invocation()
            raise ServiceError(
                _MESSAGE_EXTENSION_ACTION_FAILED,
                status_code=self._extension_operation_failure_status(invocation),
                payload={
                    "extension_id": extension_id,
                    "action_id": action_id,
                    "action": _client_action_invocation(invocation),
                },
            )
        if not context.suppressed_test_mode:
            deduped_result = _claim_action_external_effect(context)
            if deduped_result is not None:
                return deduped_result
        self._prepare_action_invocation(context)
        self._mark_action_running(context)

        try:
            invocation = self._invoke_action_with_grants(context)
        except Exception:
            if not context.external_effect_id:
                raise
            from core.application.extensions.external_effects import (
                external_effect_spawn_exception_invocation,
            )

            invocation = external_effect_spawn_exception_invocation()
        invocation["status"] = _canonical_action_status(invocation.get("status"))
        secret_exposure = _invocation_exposes_secret(invocation, hydrated_secrets)
        if secret_exposure:
            invocation = _secret_exposure_invocation()
        else:
            try:
                invocation = self._finalize_action_grants(context, invocation)
            except Exception:
                from core.application.extensions.external_effects import (
                    external_effect_output_finalization_exception_invocation,
                )

                invocation = external_effect_output_finalization_exception_invocation()
        if not context.suppressed_test_mode and not secret_exposure:
            invocation = self._process_action_response(context, invocation)
        self._cleanup_action_resources(
            context,
            invocation,
            confirm_external_effect=True,
            protected_values=hydrated_secrets,
        )
        _log_action_invocation(context, invocation)
        client_invocation = _client_action_invocation(
            invocation, host_external_effect=context.external_effect
        )
        if invocation.get("status") != "succeeded":
            raise ServiceError(
                _MESSAGE_EXTENSION_ACTION_FAILED,
                status_code=self._extension_operation_failure_status(invocation),
                payload={
                    "extension_id": extension_id,
                    "action_id": action_id,
                    "action": client_invocation,
                },
            )

        return ExtensionActionResultView(
            extension_id=extension_id, action_id=action_id, action=client_invocation
        )

    def execute_query(
        self,
        extension_id: str,
        query_id: str,
        body: Any,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionQueryResultView:
        _ = normalize_scope(scope)
        extension, bl = self._resolve_extension_runtime_record(extension_id)
        if not extension:
            raise ServiceNotFoundError(ERR_EXTENSION_NOT_FOUND)
        self._assert_operation_within_extension_scope(
            extension, operation_type="query", operation_id=query_id
        )

        handler_mode = self._extension_operation_handler_mode(
            extension, operation_type="query", operation_id=query_id
        )
        if extension.get("enabled") is False and handler_mode == "extension":
            raise ServiceConflictError(ERR_EXTENSION_DISABLED)

        from core.infrastructure.extension_runtime import extension_runtime

        runtime_facade = cast(Any, extension_runtime)
        run_extension_query = cast(
            Callable[..., dict[str, Any]], runtime_facade.run_extension_query
        )

        audit = scope_audit_fields(scope)
        runtime_ctx: dict[str, Any] = {**audit}
        if bl is not None:
            runtime_ctx["logger"] = bl
        invocation = run_extension_query(
            extension,
            query_id=query_id,
            page_id=body.page_id,
            component_id=body.component_id,
            context=body.context,
            params=body.params,
            timeout_seconds=body.timeout_seconds,
            runtime_context=runtime_ctx,
            scope=scope,
        )
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
            raise ServiceError(
                "Extension query failed",
                status_code=self._extension_operation_failure_status(invocation),
                payload={"extension_id": extension_id, "query_id": query_id, "query": invocation},
            )

        # Host contract v1.1 — normalize response and process commands/events
        process_extension_response_envelope(
            invocation,
            extension,
            extension_id,
            scope=scope,
            audit=audit,
            bl=bl,
            get_logger=self._get_operation_store,
            event_command_service_factory=self._event_command_service_factory,
            command_dispatch_service_factory=self._command_dispatch_service_factory,
        )

        return ExtensionQueryResultView(
            extension_id=extension_id, query_id=query_id, query=invocation
        )


__all__ = ["ExtensionOperationService"]
