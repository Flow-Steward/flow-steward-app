# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from core.application.extensions import runtime_grants

from .command_services_common import *  # NOSONAR

_DEFAULT_ARTIFACT_OUTPUT_MAX_SIZE_BYTES = runtime_grants.DEFAULT_GRANT_OUTPUT_MAX_SIZE_BYTES
_trusted_local_fs_artifact_grants_enabled = runtime_grants.trusted_local_fs_artifact_grants_enabled
_require_safe_artifact_grant_url = runtime_grants.require_safe_grant_url
_artifact_output_max_size = runtime_grants.output_max_size
_artifact_output_max_size_from_spec = runtime_grants.output_max_size_from_spec
_positive_int = runtime_grants.positive_int


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _require_artifact_ownership(
    plane: Any, artifact_ids: list[str], *, scope: ScopeContext
) -> None:
    runtime_grants.require_grant_ownership(
        plane,
        artifact_ids,
        kind="artifact",
        scope=scope,
    )


def _normalize_artifact_id_for_grant(value: Any) -> str:
    return runtime_grants.normalize_grant_id(value, kind="artifact")


def _require_disjoint_artifact_grants(
    artifact_input_ids: list[str],
    artifact_output_ids: list[str],
) -> None:
    runtime_grants.require_disjoint_grants(
        artifact_input_ids,
        artifact_output_ids,
        kind="artifact",
    )


def _artifact_meta_for_grant(plane: Any, artifact_id: str) -> dict[str, Any]:
    return runtime_grants.meta_for_grant(plane, artifact_id, kind="artifact")


def _require_empty_artifact_output_target(
    plane: Any,
    artifact_id: str,
    meta: dict[str, Any] | None = None,
) -> None:
    runtime_grants.require_empty_output_target(
        plane,
        artifact_id,
        kind="artifact",
        meta=meta,
    )


def _artifact_presigned_url(plane: Any, artifact_id: str, *, method: str) -> str:
    return runtime_grants.presigned_url(
        plane,
        artifact_id,
        kind="artifact",
        method=method,
    )


def _build_artifact_descriptor_for_invocation(
    plane: Any,
    artifact_id: str,
    *,
    role: str,
    binding_key: str,
    audit: dict[str, Any],
    max_size_bytes: int | None = None,
) -> dict[str, Any]:
    return runtime_grants.build_descriptor_for_invocation(
        plane,
        artifact_id,
        kind="artifact",
        role=role,
        binding_key=binding_key,
        audit=audit,
        max_size_bytes=max_size_bytes,
    )


def _artifact_upload_error(code: str, message: str, *, artifact_id: str) -> dict[str, Any]:
    return runtime_grants.grant_upload_error(
        code,
        message,
        object_id=artifact_id,
        kind="artifact",
    )


def _finalize_artifact_uploads(
    plane: Any,
    artifact_ids: list[str],
    *,
    max_size_by_id: dict[str, int] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    return runtime_grants.finalize_uploads(
        plane,
        artifact_ids,
        kind="artifact",
        max_size_by_id=max_size_by_id,
    )


def _direct_invoke_operation_id(payload: dict[str, Any]) -> str:
    payload_obj = _as_dict(payload)
    for key in ("operation_id", "action_id"):
        value = str(payload_obj.get(key) or "").strip()
        if value:
            return value
    action = payload_obj.get("action")
    if isinstance(action, dict):
        for key in ("action_id", "operation_id", "id"):
            value = str(action.get(key) or "").strip()
            if value:
                return value
    else:
        value = str(action or "").strip()
        if value:
            return value
    nested_action = _as_dict(payload_obj.get("action_payload"))
    for key in ("operation_id", "action_id"):
        value = str(nested_action.get(key) or "").strip()
        if value:
            return value
    nested_action_value = nested_action.get("action")
    if isinstance(nested_action_value, dict):
        for key in ("action_id", "operation_id", "id"):
            value = str(nested_action_value.get(key) or "").strip()
            if value:
                return value
    return str(nested_action_value or "").strip()


class ToolExecutionService:
    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        get_registry_manager: Callable[[], Any] | None = None,
        artifact_plane: Any | None = None,
        event_command_service_factory: Callable[[], Any] | None = None,
        get_extension_store: Callable[[], Any] | None = None,
    ):
        self._get_logger = get_logger
        self._get_registry_manager = get_registry_manager
        self._artifact_plane = artifact_plane
        self._event_command_service_factory = event_command_service_factory
        # Extension lookup needs a store that can actually resolve extensions.
        # get_logger here is the job store, which cannot, so invoke_extension
        # answered 503 "Extension discovery is not available" on every
        # deployment. Kept as a separate accessor rather than swapping
        # get_logger, because execute_tool passes that same object into the tool
        # runtime as its logger and must keep the store it was given.
        self._get_extension_store = get_extension_store

    def _registry_manager(self):
        if self._get_registry_manager is not None:
            return self._get_registry_manager()
        from core.application.registry.registry_manager import RegistryManager

        return RegistryManager.instance()

    def execute_tool(
        self,
        tool_id: str,
        body: ToolInvocationInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ToolInvocationResultView:
        resolved_scope = _service_scope(scope)
        body = ToolInvocationInput.model_validate(body)
        registry_manager = self._registry_manager()

        manifest = registry_manager.tools.get_tool(tool_id)
        if manifest is None:
            raise ServiceNotFoundError(f"Tool '{tool_id}' not found")
        if not manifest.enabled:
            raise ServiceConflictError("Tool is disabled")
        operations = list(getattr(manifest.interface, "operations", []) or [])
        requested_function_name = str(body.function_name or "").strip() or None
        if requested_function_name:
            function_name = requested_function_name
        elif len(operations) == 1:
            function_name = str((operations[0] or {}).get("name") or "").strip() or "invoke"
        else:
            raise ServiceValidationError("function_name is required for multi-function tools")

        from core.application.orchestration.tools.tool_resolver import invoke_manifest_tool

        audit = _scope_audit_fields(resolved_scope)
        runtime_context = {
            **audit,
            "tenant_id": resolved_scope.tenant_id,
            "source": "tool_execution_service.execute_tool",
        }
        bl = self._get_logger()
        if bl is not None:
            runtime_context["logger"] = bl
        try:
            result = invoke_manifest_tool(
                manifest,
                function_name=function_name,
                arguments=dict(body.arguments or {}),
                runtime_context=runtime_context,
            )
        except RuntimeError as exc:
            raise ServiceUnavailableError(str(exc)) from exc

        return ToolInvocationResultView(
            tool_id=tool_id,
            implementation_kind=manifest.implementation.kind,
            function_name=function_name,
            result=result,
            catalog_entry=registry_manager.tools.get_catalog_entry(tool_id)
            or manifest.catalog_entry(),
        )

    def invoke_extension(
        self,
        extension_id: str,
        body: Any,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionInvocationResultView:
        resolved_scope = _service_scope(scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        extension_store = (
            self._get_extension_store() if self._get_extension_store is not None else bl
        )
        if extension_store is None or not hasattr(extension_store, "get_extension"):
            raise ServiceUnavailableError("Extension discovery is not available")

        extension = extension_store.get_extension(
            extension_id, account_id=resolved_scope.account_id
        )
        if not extension:
            raise ServiceNotFoundError(ERR_EXTENSION_NOT_FOUND)
        if not bool(extension.get("enabled")):
            raise ServiceConflictError(ERR_EXTENSION_DISABLED)

        # Fail-closed scope envelope for the generic /invoke entrypoint: an extension
        # may only be invoked directly when it declares the ``extension:invoke`` scope
        # in its approved required_scopes (mirrors the action/query operation gate).
        if "extension:invoke" not in _extension_declared_scopes(extension):
            raise ServiceForbiddenError(
                "Extension does not declare the 'extension:invoke' scope required for "
                "direct invocation"
            )

        from core.infrastructure.extension_runtime.extension_runtime import run_extension_invocation

        audit = _scope_audit_fields(scope)
        runtime_ctx: dict[str, Any] = {**audit}
        if bl is not None:
            runtime_ctx["logger"] = bl

        direct_operation_id = _direct_invoke_operation_id(body.request)
        if direct_operation_id:
            from core.application.extensions.external_effects import (
                external_effect_declaration_for_operation,
            )

            if external_effect_declaration_for_operation(extension, direct_operation_id):
                raise ServiceForbiddenError(
                    "Direct extension invoke cannot bypass declared external_effects; "
                    "use the action endpoint for side-effect operations"
                )

        prepared_grants: runtime_grants.PreparedExtensionGrants | None = None
        produced_artifacts: list[dict[str, Any]] = []
        produced_datasets: list[dict[str, Any]] = []
        if self._artifact_plane is not None:
            prepared_grants = runtime_grants.prepare_extension_grants(
                plane=self._artifact_plane,
                scope=resolved_scope,
                audit=audit,
                extension_id=extension_id,
                artifact_inputs=list(getattr(body, "artifact_inputs", None) or []),
                artifact_outputs=list(getattr(body, "artifact_outputs", None) or []),
                artifact_output_specs=list(getattr(body, "artifact_output_specs", None) or []),
                dataset_inputs=list(getattr(body, "dataset_inputs", None) or []),
                dataset_outputs=list(getattr(body, "dataset_outputs", None) or []),
                dataset_output_specs=list(getattr(body, "dataset_output_specs", None) or []),
            )

        invocation = run_extension_invocation(
            extension,
            request_payload=body.request,
            timeout_seconds=body.timeout_seconds,
            runtime_context=runtime_ctx,
            artifact_inputs=(prepared_grants.artifact_inputs if prepared_grants else None) or None,
            artifact_outputs=(prepared_grants.artifact_outputs if prepared_grants else None)
            or None,
            dataset_inputs=(prepared_grants.dataset_inputs if prepared_grants else None) or None,
            dataset_outputs=(prepared_grants.dataset_outputs if prepared_grants else None) or None,
        )
        if (
            self._artifact_plane is not None
            and prepared_grants is not None
            and (prepared_grants.artifact_output_ids or prepared_grants.dataset_output_ids)
            and invocation.get("status") == "succeeded"
        ):
            finalized = runtime_grants.finalize_prepared_extension_grants(
                self._artifact_plane,
                prepared_grants,
            )
            if finalized.errors:
                invocation = {
                    **invocation,
                    "status": "failed",
                    "detail": str(finalized.errors[0].get("code") or "grant_upload_failed"),
                    "errors": finalized.errors,
                }
            else:
                produced_artifacts = finalized.produced_artifacts
                produced_datasets = finalized.produced_datasets

        if invocation.get("status") == "succeeded":
            # Host contract v1.1 — normalize response and process commands/events.
            _process_extension_response_envelope(
                invocation,
                extension,
                extension_id,
                scope=scope,
                audit=audit,
                bl=bl,
                get_logger=self._get_logger,
                event_command_service_factory=self._event_command_service_factory,
            )
        if (
            self._artifact_plane is not None
            and prepared_grants is not None
            and invocation.get("status") != "succeeded"
        ):
            try:
                runtime_grants.cleanup_prepared_extension_grants(
                    self._artifact_plane,
                    prepared_grants,
                )
            except Exception as exc:
                logger.warning("failed to cleanup prepared extension output grants: %s", exc)
            produced_artifacts = []
            produced_datasets = []
            invocation.pop("produced_artifacts", None)
            invocation.pop("produced_datasets", None)
            invocation.pop("grant_outputs", None)
        log_host_extension_invocation(
            bl=bl,
            extension=extension,
            extension_id=extension_id,
            invocation_type="invoke",
            request={"request": body.request, "timeout_seconds": body.timeout_seconds},
            invocation=invocation,
            runtime_context=runtime_ctx,
            request_context={"source": "tool_execution_service.invoke_extension"},
        )
        if invocation.get("status") != "succeeded":
            produced_artifacts = []
            produced_datasets = []
            detail = str((invocation or {}).get("detail") or "").strip().lower()
            if "disabled" in detail:
                status_code = 409
            elif any(token in detail for token in ("unknown", "requires", "unsupported")):
                status_code = 400
            else:
                status_code = 502
            raise ServiceError(
                "Extension invocation failed",
                status_code=status_code,
                payload={"extension_id": extension_id, "invocation": invocation},
            )

        return ExtensionInvocationResultView(
            extension_id=extension_id,
            invocation=invocation,
            produced_artifacts=produced_artifacts,
            produced_datasets=produced_datasets,
        )


__all__ = [name for name in globals() if not name.startswith("__")]
