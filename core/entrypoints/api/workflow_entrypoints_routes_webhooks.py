from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from core.application.workflows import (
    workflow_entrypoint_repository,
    workflow_provider_trigger_repository,
)
from core.application.workflows.workflow_entrypoint_input_service import (
    WorkflowEntrypointInputValidationError,
)
from core.application.workflows.workflow_trigger_catalog import list_workflow_trigger_catalog
from core.application.workflows.workflow_webhook_creation_service import (
    WorkflowWebhookCreateCommand,
    WorkflowWebhookCreationError,
    WorkflowWebhookUpdateCommand,
    create_workflow_webhook_creation_service,
    create_workflow_webhook_mutation_service,
    create_workflow_webhook_update_service,
)

from .workflow_entrypoints_common import (
    DUPLICATE_KEY_VALUE,
    WORKFLOW_ENTRYPOINT_NOT_FOUND_MESSAGE,
    WORKFLOW_IS_NOT_LINKED_TO_THE_SELECTED_PROJECT_VALUE,
    WorkflowInterfaceService,
    _build_workflow_inputs,
    _expected_input_names,
    _load_workflow_entrypoint,
    _require_project_scope,
    _require_workflow_scope,
    _resolve_project_run_config_for_workflow,
)
from .workflow_entrypoints_registry import (
    _absolute_url_for_request,
    _entrypoint_project_binding_id,
    _entrypoint_secret_reveal,
    _generate_entry_key,
    _load_workflow_manifest,
    _normalize_mapping,
    _public_workflow_webhook_path,
    _resolve_provider_trigger_project_binding,
)
from .workflow_entrypoints_serializers import (
    WorkflowEntrypointCreateRequest,
    WorkflowEntrypointDryRunRequest,
    WorkflowEntrypointUpdateRequest,
    _serialize_delivery,
    _serialize_provider_trigger,
    _serialize_workflow_entrypoint,
)


def _register_workflow_webhook_routes(ctx: dict[str, Any]) -> None:

    router = ctx["router"]
    get_logger_only = ctx["get_logger_only"]
    get_trigger_store = ctx.get("get_trigger_store", get_logger_only)
    scope_resolver = ctx["scope_resolver"]
    logger = ctx["logger"]
    err_database_not_configured = ctx["err_database_not_configured"]

    def _require_existing_entrypoint_project_scope(
        bl: Any,
        scope: Any,
        workflow_id: str,
        entrypoint: dict[str, Any],
    ) -> str | None:
        del workflow_id
        current_project_id = _entrypoint_project_binding_id(entrypoint)
        if not current_project_id:
            raise HTTPException(
                status_code=409,
                detail="Workflow entrypoint has no project binding",
            )
        return _require_project_scope(bl, scope, current_project_id or None)

    def _entrypoint_visible_for_project(
        bl: Any,
        workflow_id: str,
        entrypoint: dict[str, Any],
        project_id: str | None,
    ) -> bool:
        del bl, workflow_id
        resolved_project_id = str(project_id or "").strip()
        if not resolved_project_id:
            return False
        current_project_id = _entrypoint_project_binding_id(entrypoint)
        return bool(current_project_id and current_project_id == resolved_project_id)

    @router.get(
        "/api/workflows/{workflow_id}/triggers/catalog",
        responses={403: {"description": "Forbidden"}, 404: {"description": "Not found"}},
    )
    def get_workflow_trigger_catalog(workflow_id: str, request: Request):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        _load_workflow_manifest(workflow_id)
        scope = scope_resolver(request)
        _require_workflow_scope(scope, workflow_id)
        resolved_project_id, _ = _resolve_project_run_config_for_workflow(bl, workflow_id)
        _require_project_scope(bl, scope, resolved_project_id)
        return {
            "workflow_id": str(workflow_id or "").strip(),
            "triggers": list_workflow_trigger_catalog(get_trigger_store()),
        }

    @router.get(
        "/api/workflows/{workflow_id}/triggers",
        responses={403: {"description": "Forbidden"}, 404: {"description": "Not found"}},
    )
    def get_workflow_triggers(
        workflow_id: str,
        request: Request,
        project_id: str | None = None,
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        try:
            scope = scope_resolver(request)
            _load_workflow_manifest(workflow_id)
            _require_workflow_scope(scope, workflow_id)
            resolved_project_id = _require_project_scope(
                bl,
                scope,
                str(project_id or "").strip()
                or _resolve_project_run_config_for_workflow(bl, workflow_id)[0],
            )
            entrypoints = workflow_entrypoint_repository.list_workflow_entrypoints_for_workflow(
                bl,
                workflow_id,
            )
            provider_triggers = (
                workflow_provider_trigger_repository.list_workflow_provider_triggers(
                    bl,
                    workflow_id=workflow_id,
                    project_id=resolved_project_id,
                )
            )
            catalog = list_workflow_trigger_catalog(get_trigger_store())
            catalog_by_key = {
                f"{str(item.get('provider_key') or '').strip().lower()}:{str(item.get('trigger_type_id') or '').strip()}": item
                for item in catalog
                if item.get("trigger_family") == "provider"
            }
        except HTTPException as exc:
            raise exc
        except Exception:
            logger.exception("Failed to list workflow triggers for '%s'", workflow_id)
            return JSONResponse({"error": "Failed to list workflow triggers"}, status_code=500)
        return {
            "workflow_id": str(workflow_id or "").strip(),
            "catalog": catalog,
            "incoming_webhooks": [
                _serialize_workflow_entrypoint(row, request=request)
                for row in entrypoints
                if str(row.get("entry_kind") or "webhook").strip() == "webhook"
                and _entrypoint_visible_for_project(bl, workflow_id, row, resolved_project_id)
            ],
            "provider_triggers": [
                _serialize_provider_trigger(
                    row,
                    definition=catalog_by_key.get(
                        f"{str(row.get('provider_key') or '').strip().lower()}:{str(row.get('trigger_type_id') or '').strip()}"
                    ),
                    request=request,
                )
                for row in provider_triggers
            ],
        }

    @router.post(
        "/api/workflows/{workflow_id}/triggers/webhooks",
        responses={403: {"description": "Forbidden"}, 404: {"description": "Not found"}},
    )
    def create_workflow_webhook_trigger(
        workflow_id: str,
        body: WorkflowEntrypointCreateRequest,
        request: Request,
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        scope = scope_resolver(request)
        _load_workflow_manifest(workflow_id)
        _require_workflow_scope(scope, workflow_id)
        clean_entry_key = _generate_entry_key()
        try:
            creation_service = create_workflow_webhook_creation_service(
                resolve_project_binding=lambda gateway, workflow, project: (
                    _resolve_provider_trigger_project_binding(
                        gateway,
                        workflow_id=workflow,
                        explicit_project_id=project,
                    )
                ),
            )
            command = WorkflowWebhookCreateCommand.from_request(
                workflow_id=workflow_id,
                project_id=body.project_id,
                entry_key=clean_entry_key,
                request=body.model_dump(),
                expected_input_names=_expected_input_names(workflow_id),
            )
            created, secret_value = creation_service.create(
                bl=bl,
                command=command,
                authorize_project=lambda project_id: _require_project_scope(bl, scope, project_id),
            )
        except WorkflowWebhookCreationError as exc:
            return JSONResponse(
                {"error": str(exc), "code": exc.code},
                status_code=exc.status_code,
            )
        except Exception as exc:
            if DUPLICATE_KEY_VALUE in str(exc).lower() or "entry_key_key" in str(exc).lower():
                return JSONResponse(
                    {"error": "Workflow entrypoint already exists"},
                    status_code=409,
                )
            raise
        payload = _serialize_workflow_entrypoint(created, request=request)
        if secret_value:
            payload["credential_reveal"] = _entrypoint_secret_reveal(
                command.auth_mode,
                command.auth_config,
                secret_value=secret_value,
            )
        else:
            # An imported provider secret is stored but never echoed back.
            payload["credential_configured"] = True
        return JSONResponse(payload, status_code=201)

    @router.patch(
        "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}",
        responses={403: {"description": "Forbidden"}, 404: {"description": "Not found"}},
    )
    def update_workflow_webhook_trigger(
        workflow_id: str,
        entry_key: str,
        body: WorkflowEntrypointUpdateRequest,
        request: Request,
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        scope = scope_resolver(request)
        _load_workflow_manifest(workflow_id)
        _require_workflow_scope(scope, workflow_id)
        existing = _load_workflow_entrypoint(bl, entry_key)
        if (
            not existing
            or str(existing.get("workflow_id") or "").strip() != str(workflow_id).strip()
        ):
            return JSONResponse({"error": WORKFLOW_ENTRYPOINT_NOT_FOUND_MESSAGE}, status_code=404)
        current_project_id = _require_existing_entrypoint_project_scope(
            bl, scope, workflow_id, existing
        )
        if not str(existing.get("project_id") or "").strip() and current_project_id:
            existing = {**existing, "project_id": str(current_project_id)}
        try:
            command = WorkflowWebhookUpdateCommand.from_request(
                workflow_id=workflow_id,
                entry_key=entry_key,
                existing=existing,
                request=body.model_dump(),
                expected_input_names=_expected_input_names(workflow_id),
            )
            update_service = create_workflow_webhook_update_service(
                resolve_project_binding=lambda gateway, workflow, project: (
                    _resolve_provider_trigger_project_binding(
                        gateway,
                        workflow_id=workflow,
                        explicit_project_id=project,
                    )
                ),
            )
            updated, secret_value = update_service.update(
                bl=bl,
                command=command,
                authorize_project=lambda project_id: _require_project_scope(bl, scope, project_id),
            )
        except WorkflowWebhookCreationError as exc:
            return JSONResponse({"error": str(exc), "code": exc.code}, status_code=exc.status_code)
        payload = _serialize_workflow_entrypoint(updated, request=request)
        if secret_value is not None:
            payload["credential_reveal"] = _entrypoint_secret_reveal(
                command.auth_mode,
                command.auth_config,
                secret_value=secret_value,
            )
        elif command.signing_secret:
            # An imported provider secret is stored but never echoed back.
            payload["credential_configured"] = True
        return payload

    @router.post(
        "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/rotate-secret",
        responses={403: {"description": "Forbidden"}, 404: {"description": "Not found"}},
    )
    def rotate_workflow_webhook_secret(
        workflow_id: str,
        entry_key: str,
        request: Request,
        project_id: str | None = None,
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        scope = scope_resolver(request)
        _load_workflow_manifest(workflow_id)
        _require_workflow_scope(scope, workflow_id)
        existing = _load_workflow_entrypoint(bl, entry_key)
        if (
            not existing
            or str(existing.get("workflow_id") or "").strip() != str(workflow_id).strip()
        ):
            return JSONResponse({"error": WORKFLOW_ENTRYPOINT_NOT_FOUND_MESSAGE}, status_code=404)
        current_project_id = _require_existing_entrypoint_project_scope(
            bl, scope, workflow_id, existing
        )
        if not str(existing.get("project_id") or "").strip() and current_project_id:
            existing = {**existing, "project_id": str(current_project_id)}
        try:
            command = WorkflowWebhookUpdateCommand.for_rotation(
                workflow_id=workflow_id,
                entry_key=entry_key,
                existing=existing,
                project_id=project_id,
                expected_input_names=_expected_input_names(workflow_id),
            )
            update_service = create_workflow_webhook_update_service(
                resolve_project_binding=lambda gateway, workflow, project: (
                    _resolve_provider_trigger_project_binding(
                        gateway,
                        workflow_id=workflow,
                        explicit_project_id=project,
                    )
                ),
            )
            existing, secret_value = update_service.update(
                bl=bl,
                command=command,
                authorize_project=lambda bound_id: _require_project_scope(bl, scope, bound_id),
            )
            if secret_value is None:
                raise WorkflowWebhookCreationError(
                    "secret_storage_unavailable",
                    "Secret storage is unavailable",
                    status_code=503,
                )
        except WorkflowWebhookCreationError as exc:
            return JSONResponse({"error": str(exc), "code": exc.code}, status_code=exc.status_code)
        payload = _serialize_workflow_entrypoint(existing, request=request)
        payload["credential_reveal"] = _entrypoint_secret_reveal(
            command.auth_mode,
            command.auth_config,
            secret_value=secret_value,
        )
        return payload

    @router.post(
        "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/regenerate-entry-key",
        responses={403: {"description": "Forbidden"}, 404: {"description": "Not found"}},
    )
    def regenerate_workflow_webhook_entry_key(
        workflow_id: str,
        entry_key: str,
        request: Request,
        project_id: str | None = None,
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        scope = scope_resolver(request)
        _load_workflow_manifest(workflow_id)
        _require_workflow_scope(scope, workflow_id)
        existing = _load_workflow_entrypoint(bl, entry_key)
        if (
            not existing
            or str(existing.get("workflow_id") or "").strip() != str(workflow_id).strip()
        ):
            return JSONResponse({"error": WORKFLOW_ENTRYPOINT_NOT_FOUND_MESSAGE}, status_code=404)
        _require_existing_entrypoint_project_scope(bl, scope, workflow_id, existing)
        if str(project_id or "").strip():
            resolved_project_id, _run_cfg = _resolve_provider_trigger_project_binding(
                bl,
                workflow_id=workflow_id,
                explicit_project_id=project_id,
            )
            if not resolved_project_id:
                return JSONResponse(
                    {"error": WORKFLOW_IS_NOT_LINKED_TO_THE_SELECTED_PROJECT_VALUE},
                    status_code=409,
                )
        _require_project_scope(
            bl,
            scope,
            project_id or _entrypoint_project_binding_id(existing) or None,
        )
        try:
            updated = create_workflow_webhook_mutation_service().regenerate(
                bl=bl,
                entry_key=entry_key,
                next_entry_key=_generate_entry_key(),
            )
        except Exception as exc:
            if DUPLICATE_KEY_VALUE in str(exc).lower():
                return JSONResponse({"error": "Failed to regenerate entry key"}, status_code=409)
            raise
        return _serialize_workflow_entrypoint(updated, request=request)

    @router.delete(
        "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}",
        responses={403: {"description": "Forbidden"}, 404: {"description": "Not found"}},
    )
    def delete_workflow_webhook_trigger(
        workflow_id: str,
        entry_key: str,
        request: Request,
        project_id: str | None = None,
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        scope = scope_resolver(request)
        _load_workflow_manifest(workflow_id, include_trashed=True)
        _require_workflow_scope(scope, workflow_id)
        existing = workflow_entrypoint_repository.load_workflow_entrypoint(
            bl, entry_key, include_trashed=True
        )
        if (
            not existing
            or str(existing.get("workflow_id") or "").strip() != str(workflow_id).strip()
        ):
            return JSONResponse({"error": WORKFLOW_ENTRYPOINT_NOT_FOUND_MESSAGE}, status_code=404)
        _require_existing_entrypoint_project_scope(bl, scope, workflow_id, existing)
        if str(project_id or "").strip():
            resolved_project_id, _run_cfg = _resolve_provider_trigger_project_binding(
                bl,
                workflow_id=workflow_id,
                explicit_project_id=project_id,
            )
            if not resolved_project_id:
                return JSONResponse(
                    {"error": WORKFLOW_IS_NOT_LINKED_TO_THE_SELECTED_PROJECT_VALUE},
                    status_code=409,
                )
        _require_project_scope(
            bl,
            scope,
            project_id or _entrypoint_project_binding_id(existing) or None,
        )
        try:
            deleted = create_workflow_webhook_mutation_service().delete(
                bl=bl,
                entry_key=entry_key,
                workflow_id=workflow_id,
                project_id=str(project_id or _entrypoint_project_binding_id(existing) or ""),
            )
        except WorkflowWebhookCreationError as exc:
            return JSONResponse({"error": str(exc), "code": exc.code}, status_code=exc.status_code)
        return {"status": "deleted" if deleted else "missing", "entry_key": entry_key}

    @router.post(
        "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/dry-run",
        responses={403: {"description": "Forbidden"}, 404: {"description": "Not found"}},
    )
    def dry_run_workflow_webhook_trigger(
        workflow_id: str,
        entry_key: str,
        body: WorkflowEntrypointDryRunRequest,
        request: Request,
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        scope = scope_resolver(request)
        _load_workflow_manifest(workflow_id)
        _require_workflow_scope(scope, workflow_id)
        entrypoint = _load_workflow_entrypoint(bl, entry_key)
        if (
            not entrypoint
            or str(entrypoint.get("workflow_id") or "").strip() != str(workflow_id).strip()
        ):
            return JSONResponse({"error": WORKFLOW_ENTRYPOINT_NOT_FOUND_MESSAGE}, status_code=404)
        _require_existing_entrypoint_project_scope(bl, scope, workflow_id, entrypoint)
        if str(body.project_id or "").strip():
            resolved_project_id, _run_cfg = _resolve_provider_trigger_project_binding(
                bl,
                workflow_id=workflow_id,
                explicit_project_id=body.project_id,
            )
            if not resolved_project_id:
                return JSONResponse(
                    {"error": WORKFLOW_IS_NOT_LINKED_TO_THE_SELECTED_PROJECT_VALUE},
                    status_code=409,
                )
        _require_project_scope(
            bl,
            scope,
            body.project_id or _entrypoint_project_binding_id(entrypoint) or None,
        )
        sources = {
            "body": dict(body.body or {}),
            "form": dict(body.form or {}),
            "query": dict(body.query or {}),
            "headers": {str(k): str(v) for k, v in dict(body.headers or {}).items()},
            "files": list(body.files or []),
        }
        mapping = _normalize_mapping(entrypoint.get("input_mapping_jsonb"))
        try:
            workflow_inputs = _build_workflow_inputs(
                workflow_id=workflow_id,
                mapping=mapping,
                sources=sources,
                fallback_body=dict(body.body or {}),
                file_refs=list(body.files or []),
            )
            validation = WorkflowInterfaceService().validate_inputs_for_workflow_id(
                str(workflow_id or "").strip(),
                workflow_inputs,
                channel="webhook",
            )
        except WorkflowEntrypointInputValidationError as exc:
            workflow_inputs = exc.workflow_inputs
            validation = exc.validation
        return {
            "workflow_id": str(workflow_id or "").strip(),
            "entry_key": str(entry_key or "").strip(),
            "method": "POST",
            "public_url": _absolute_url_for_request(
                request,
                _public_workflow_webhook_path(entry_key),
            ),
            "mapped_inputs": workflow_inputs,
            "validation": {
                "ok": bool(validation.ok),
                "errors": list(validation.errors or []),
                "interface": dict(validation.interface or {}),
                "inputs": dict(validation.inputs or {}) if validation.ok else workflow_inputs,
            },
        }

    @router.get(
        "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/deliveries",
        responses={403: {"description": "Forbidden"}, 404: {"description": "Not found"}},
    )
    def get_workflow_webhook_deliveries(
        workflow_id: str,
        entry_key: str,
        request: Request,
        limit: int = 20,
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        scope = scope_resolver(request)
        _load_workflow_manifest(workflow_id)
        _require_workflow_scope(scope, workflow_id)
        entrypoint = _load_workflow_entrypoint(bl, entry_key)
        if (
            not entrypoint
            or str(entrypoint.get("workflow_id") or "").strip() != str(workflow_id).strip()
        ):
            return JSONResponse({"error": WORKFLOW_ENTRYPOINT_NOT_FOUND_MESSAGE}, status_code=404)
        _require_existing_entrypoint_project_scope(bl, scope, workflow_id, entrypoint)
        deliveries = workflow_entrypoint_repository.list_workflow_entrypoint_deliveries(
            bl,
            workflow_id=workflow_id,
            entry_key=entry_key,
            limit=limit,
        )
        return {
            "workflow_id": str(workflow_id or "").strip(),
            "entry_key": str(entry_key or "").strip(),
            "deliveries": [_serialize_delivery(row) for row in deliveries],
        }


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
