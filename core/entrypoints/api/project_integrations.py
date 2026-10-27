"""Project integration API routes (providers + notifications)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.application.extensions.connection_partition_validation import (
    ConnectionPartitionsRejected,
    require_valid_connection_partitions,
)
from core.application.extensions.external_effects import (
    declared_features,
    notification_target_kind,
)
from core.application.management import ServiceError, ServiceForbiddenError
from core.application.management.command_models import (
    ProjectNotificationDispatchInput,
    ProjectNotificationRoutesUpdateInput,
)
from core.application.management.guards import require_project_access
from core.application.management.requests import (
    NotificationDispatchRequest,
    NotificationRoutesRequest,
    ProjectProviderBindingRequest,
    ProviderRequest,
    ProviderSecretRequest,
)
from core.application.workflows.notification_recipients import notification_recipient_format

_MESSAGE_EXACT_INSTALLED_CONNECTION_CONTRACT_IS_UNAVAILABLE = (
    "exact installed connection contract is unavailable"
)


def create_project_integrations_router(
    *,
    get_logger_only: Callable[[], Any],
    project_query_service: Callable[[], Any],
    project_command_service: Callable[[], Any],
    notification_command_service: Callable[[], Any] | None = None,
    request_scope: Callable[..., Any],
    require_authenticated_user: Callable[..., dict],
    to_json_obj: Callable[[Any], Any],
    api_error_response: Callable[[Exception], JSONResponse],
    service_error_response: Callable[[Exception], JSONResponse],
    secret_storage_config_error: Callable[[Exception], bool],
    auth_admin_roles: set[str] | list[str] | tuple[str, ...],
    err_database_not_configured: str,
    err_project_not_found: str,
    err_admin_role_required: str,
) -> APIRouter:
    router = APIRouter(tags=["project-integrations"])
    notification_commands = notification_command_service or project_command_service

    @router.get("/api/providers")
    async def api_providers(
        request: Request,
        enabled_only: Annotated[bool, Query()] = True,
        provider_type_key: Annotated[str, Query()] = "",
        project_id: Annotated[str | None, Query()] = None,
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)

        normalized_type = str(provider_type_key or "").strip().lower() or None
        normalized_project_id = str(project_id or "").strip() or None
        if normalized_project_id:
            project = bl.get_project(normalized_project_id) if hasattr(bl, "get_project") else None
            if not project:
                return JSONResponse({"error": err_project_not_found}, status_code=404)
            scope = request_scope(request, project_id=normalized_project_id)
            try:
                require_project_access(scope, project)
            except ServiceForbiddenError as e:
                return service_error_response(e)
        try:
            provider_types = (
                bl.list_provider_types(enabled_only=enabled_only)
                if hasattr(bl, "list_provider_types")
                else []
            )
            providers = (
                bl.list_providers(enabled_only=enabled_only, provider_type_key=normalized_type)
                if hasattr(bl, "list_providers")
                else []
            )
            provider_health = (
                bl.list_latest_provider_health(project_id=normalized_project_id)
                if normalized_project_id and hasattr(bl, "list_latest_provider_health")
                else {}
            )
        except Exception as e:
            return api_error_response(e)

        # The providers table is a global catalog with no account/project column, and
        # provider mutation (POST /api/providers) is admin-only. Mirror that on read:
        # only admins see the raw provider ``config`` blob; other authenticated callers
        # get the catalog metadata without configuration internals.
        caller_is_admin = True
        try:
            require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            caller_is_admin = False

        hydrated = []
        for row in providers:
            item = dict(row or {})
            if not caller_is_admin:
                item.pop("config", None)
            provider_key = str(item.get("provider_key") or "").strip()
            item["health"] = dict(provider_health.get(provider_key) or {})
            hydrated.append(item)

        return {
            "provider_types": provider_types,
            "providers": hydrated,
        }

    @router.post("/api/providers")
    async def api_upsert_provider(request: Request, body: ProviderRequest):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        try:
            require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)
        try:
            if not hasattr(bl, "save_provider"):
                raise RuntimeError("Provider persistence is not available")
            bl.save_provider(body.model_dump())
            provider = bl.get_provider(body.provider_key) if hasattr(bl, "get_provider") else None
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except RuntimeError as e:
            if secret_storage_config_error(e):
                return JSONResponse({"error": str(e)}, status_code=503)
            return api_error_response(e)
        except Exception as e:
            return api_error_response(e)

        return provider or {"provider_key": body.provider_key, "status": "saved"}

    @router.post("/api/providers/{provider_key}/secrets")
    async def api_upsert_provider_secret(
        request: Request, provider_key: str, body: ProviderSecretRequest
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not hasattr(bl, "save_provider_secret"):
            return JSONResponse(
                {"error": "Provider secret storage is not available"}, status_code=503
            )
        target_project_id = str(body.project_id).strip() if body.project_id is not None else ""
        if target_project_id:
            if not hasattr(bl, "get_project"):
                return JSONResponse({"error": "Project storage is not available"}, status_code=503)
            project = bl.get_project(target_project_id)
            if not project:
                return JSONResponse({"error": err_project_not_found}, status_code=404)
            try:
                require_project_access(
                    request_scope(request, project_id=target_project_id), project
                )
            except ServiceForbiddenError as e:
                return service_error_response(e)
        else:
            try:
                require_authenticated_user(request, required_roles=auth_admin_roles)
            except PermissionError:
                return JSONResponse({"error": err_admin_role_required}, status_code=403)

        try:
            saved = bl.save_provider_secret(
                provider_key=provider_key,
                secret_key=str(body.secret_key or "").strip(),
                secret_value=str(body.secret_value or ""),
                project_id=target_project_id or None,
                active=bool(body.active),
                metadata=body.metadata,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except RuntimeError as e:
            if secret_storage_config_error(e):
                return JSONResponse({"error": str(e)}, status_code=503)
            return api_error_response(e)
        except Exception as e:
            return api_error_response(e)

        return {
            "status": "saved" if saved else "skipped",
            "provider_key": provider_key,
            "secret_key": str(body.secret_key or "").strip(),
            "project_id": target_project_id or None,
            "active": bool(body.active),
        }

    @router.get("/api/projects/{project_id}/providers")
    async def api_project_providers(
        request: Request,
        project_id: str,
        enabled_only: Annotated[bool, Query()] = False,
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        scope = request_scope(request, project_id=project_id)
        project = bl.get_project(project_id)
        if not project:
            return JSONResponse({"error": err_project_not_found}, status_code=404)
        try:
            require_project_access(scope, project)
        except ServiceForbiddenError as e:
            return service_error_response(e)

        try:
            project_providers: list[dict[str, Any]] = []
            if hasattr(bl, "list_project_connections"):
                connection_rows = (
                    bl.list_project_connections(project_id, enabled_only=enabled_only) or []
                )
                if not isinstance(connection_rows, list):
                    connection_rows = []
                for row in connection_rows:
                    payload = to_json_obj(row) or {}
                    extension_id = str(payload.get("extension_id") or "").strip()
                    # `to_json_obj(None)` is None, and an extension-backed project
                    # connection legitimately has no provider row. Every access
                    # below assumed a mapping, so one such connection raised
                    # AttributeError and took the whole provider inventory to a
                    # 500 — which is what blocked the entire notification-route
                    # UI path (AUDIT-060). Normalize once, here.
                    provider_row = (
                        to_json_obj(bl.get_provider(extension_id))
                        if extension_id and hasattr(bl, "get_provider")
                        else {}
                    ) or {}
                    extension_row = (
                        to_json_obj(bl.get_extension(extension_id))
                        if extension_id and hasattr(bl, "get_extension")
                        else {}
                    ) or {}
                    provider_enabled = (
                        bool(provider_row.get("enabled", True)) if provider_row else True
                    )
                    if enabled_only and not provider_enabled:
                        continue
                    provider_type_key = str(provider_row.get("provider_type_key") or "").strip()
                    if not provider_type_key:
                        provider_type_key = str(
                            extension_row.get("kind")
                            or extension_row.get("provider_type_key")
                            or "task_provider"
                        ).strip()
                    project_providers.append(
                        {
                            "provider_key": extension_id,
                            "provider_type_key": provider_type_key,
                            "display_name": str(
                                provider_row.get("display_name")
                                or payload.get("extension_id")
                                or extension_id
                            ).strip(),
                            "implementation_kind": str(
                                provider_row.get("implementation_kind") or "extension"
                            ).strip(),
                            "integration_key": str(
                                provider_row.get("integration_key") or extension_id
                            ).strip(),
                            # Both read from the extension's own typed manifest, so the
                            # UI can tell a mail provider from a chat one without the
                            # platform knowing which extension is which. Without these
                            # the client had no capability to read and treated every
                            # provider as channel-addressed.
                            "features": (
                                [
                                    str(item).strip()
                                    for item in (provider_row.get("features") or [])
                                    if str(item or "").strip()
                                ]
                                or declared_features(extension_row)
                            ),
                            "notification_target_kind": notification_target_kind(extension_row),
                            "notification_recipient_format": notification_recipient_format(
                                extension_row
                            ),
                            "provider_config": to_json_obj(provider_row.get("config")) or {},
                            "binding_config": to_json_obj(payload.get("connection_config")) or {},
                            "enabled": bool(payload.get("enabled", True)),
                            "provider_enabled": provider_enabled,
                            "binding_source": "project_connection",
                            "connection_id": payload.get("connection_id"),
                        }
                    )
        except Exception as e:
            return api_error_response(e)

        return {
            "project_id": project_id,
            "project_providers": project_providers,
        }

    @router.post("/api/projects/{project_id}/providers/bind")
    async def api_bind_project_provider(
        request: Request, project_id: str, body: ProjectProviderBindingRequest
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        scope = request_scope(request, project_id=project_id)
        project = bl.get_project(project_id)
        if not project:
            return JSONResponse({"error": err_project_not_found}, status_code=404)
        try:
            require_project_access(scope, project)
        except ServiceForbiddenError as e:
            return service_error_response(e)

        try:
            provider_row = (
                to_json_obj(bl.get_provider(body.provider_key))
                if hasattr(bl, "get_provider")
                else {}
            ) or {}
            if not provider_row:
                return JSONResponse(
                    {"error": f"Provider '{body.provider_key}' not found"},
                    status_code=404,
                )
            provider_config = to_json_obj(provider_row.get("config")) or {}
            extension_id = str(
                provider_config.get("extension_key")
                or provider_row.get("integration_key")
                or body.provider_key
                or ""
            ).strip()
            if not extension_id:
                raise ValueError("provider_key is required")
            existing = (
                bl.get_project_connection(
                    project_id=project_id, extension_id=extension_id, enabled_only=False
                )
                if hasattr(bl, "get_project_connection")
                else None
            ) or {}
            account_id = str(project.get("account_id") or scope.account_id or "").strip()
            install_id = str(existing.get("extension_install_id") or "").strip()
            connection_type = str(existing.get("connection_type") or "").strip()
            runtime_lookup = getattr(bl, "get_extension_by_install_id_for_runtime_gate", None)
            if (
                not account_id
                or not install_id
                or not connection_type
                or not callable(runtime_lookup)
            ):
                raise ConnectionPartitionsRejected(
                    _MESSAGE_EXACT_INSTALLED_CONNECTION_CONTRACT_IS_UNAVAILABLE
                )
            extension_row = runtime_lookup(install_id, account_id=account_id)
            if not isinstance(extension_row, dict):
                raise ConnectionPartitionsRejected(
                    _MESSAGE_EXACT_INSTALLED_CONNECTION_CONTRACT_IS_UNAVAILABLE
                )
            resolved_extension_id = str(
                extension_row.get("extension_key") or extension_row.get("extension_id") or ""
            ).strip()
            resolved_install_id = str(
                extension_row.get("extension_install_id") or extension_row.get("install_id") or ""
            ).strip()
            if resolved_extension_id != extension_id or resolved_install_id != install_id:
                raise ConnectionPartitionsRejected(
                    _MESSAGE_EXACT_INSTALLED_CONNECTION_CONTRACT_IS_UNAVAILABLE
                )
            require_valid_connection_partitions(
                extension_row,
                connection_type=connection_type,
                connection_config=body.config,
                ingress_config=to_json_obj(existing.get("ingress_config")) or {},
            )
            bl.upsert_project_connection(
                project_id=project_id,
                extension_id=extension_id,
                account_id=account_id or None,
                connection_kind=str(existing.get("connection_kind") or "extension").strip()
                or "extension",
                enabled=body.enabled,
                connection_config=body.config,
                secret_refs=list(existing.get("secret_refs") or []),
                connection_routes=list(existing.get("connection_routes") or []),
                ingress_config=to_json_obj(existing.get("ingress_config")) or {},
                workflow_mappings=to_json_obj(existing.get("workflow_mappings")) or {},
            )
        except ConnectionPartitionsRejected as e:
            return JSONResponse(
                {"error": e.code, "code": e.code},
                status_code=400,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return api_error_response(e)

        return await api_project_providers(request, project_id, enabled_only=False)

    @router.get("/api/projects/{project_id}/notification-routes")
    async def api_project_notification_routes(
        request: Request,
        project_id: str,
        enabled_only: Annotated[bool, Query()] = False,
    ):
        try:
            payload = project_query_service().list_notification_routes(
                project_id,
                enabled_only=enabled_only,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

        return payload.dump()

    @router.get("/api/projects/{project_id}/notification-overview")
    async def api_project_notification_overview(request: Request, project_id: str):
        try:
            payload = project_query_service().get_notification_overview(
                project_id,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

        return payload.dump()

    @router.post("/api/projects/{project_id}/notification-routes")
    async def api_update_project_notification_routes(
        request: Request, project_id: str, body: NotificationRoutesRequest
    ):
        try:
            payload = notification_commands().update_notification_routes(
                project_id,
                ProjectNotificationRoutesUpdateInput(
                    routes=[item.model_dump() for item in (body.routes or [])],
                ),
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

        return payload.dump()

    @router.get("/api/projects/{project_id}/notification-deliveries")
    async def api_project_notification_deliveries(
        request: Request,
        project_id: str,
        job_id: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query()] = 200,
    ):
        try:
            payload = project_query_service().list_notification_deliveries(
                project_id,
                job_id=str(job_id) if job_id else None,
                limit=limit,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

        return payload.dump()

    @router.post("/api/projects/{project_id}/notifications/dispatch")
    async def api_dispatch_project_notification(
        request: Request, project_id: str, body: NotificationDispatchRequest
    ):
        try:
            payload = notification_commands().dispatch_project_notification(
                project_id,
                ProjectNotificationDispatchInput(
                    event_type=body.event_type,
                    severity=body.severity,
                    workflow_id=body.workflow_id,
                    job_id=str(body.job_id) if body.job_id is not None else None,
                    payload=body.payload,
                ),
                scope=request_scope(
                    request,
                    project_id=project_id,
                    workflow_id=body.workflow_id or None,
                ),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

        return payload.dump()

    return router
