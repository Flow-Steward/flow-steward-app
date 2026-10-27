"""Register extension routes on the shared API router."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from core.application.management import ServiceError, ServiceValidationError
from core.application.management.command_models import (
    ExtensionDescriptorImportFileInput,
    ExtensionDescriptorImportUrlInput,
    ExtensionDescriptorValidateInput,
    ExtensionGeneratedJobCreateInput,
    ExtensionReleaseInstallInput,
    ExtensionRuntimeTokenInput,
)
from core.application.management.requests import (
    ExtensionDescriptorImportFileRequest,
    ExtensionDescriptorImportUrlRequest,
    ExtensionDescriptorValidateRequest,
    ExtensionEnableRequest,
    ExtensionGeneratedJobCreateRequest,
    ExtensionInstallRequest,
    ExtensionListQueryRequest,
    ExtensionMarketplaceQueryRequest,
    ExtensionReleaseInstallRequest,
    ExtensionRuntimeTokenRequest,
)
from core.application.settings.ui_i18n import resolve_ui_locale
from core.entrypoints.api.integrations_request_helpers import (
    _parse_bool_query,
    _parse_optional_bool_query,
    _query_csv_values,
    _request_has_admin_role,
)
from core.infrastructure.runtime.serve_request_runtime import validation_errors_without_url


def register_extension_routes(
    *,
    api_error_response: Callable[..., JSONResponse],
    extension_binding_service: Callable[[], Any] | None,
    extension_query_service: Callable[[], Any],
    get_extension_install_store: Callable[[], Any],
    get_extension_runtime_store: Callable[[], Any],
    get_logger: Callable[[], Any],
    request_scope: Callable[..., Any],
    router: APIRouter,
    service_error_response: Callable[..., JSONResponse],
) -> None:
    def _extension_command_service():
        from core.application.extensions.command_service import ExtensionCommandService

        return ExtensionCommandService(
            get_logger=get_logger,
            get_install_store=get_extension_install_store,
            get_runtime_store=get_extension_runtime_store,
        )

    def _extension_lifecycle_service():
        if extension_binding_service is not None:
            return extension_binding_service()
        return _extension_command_service()

    def _reload_extension_registries() -> None:
        # Extension projections are loaded into process-local registries.  A
        # successful lifecycle mutation must become visible to the very next
        # workflow/catalog request without requiring an application restart.
        from core.application.registry.registry_manager import RegistryManager

        RegistryManager.instance().reload_all()

    def _refresh_extension_browser_permissions() -> None:
        from core.infrastructure.extension_runtime.browser_permissions import (
            refresh_browser_permissions_from_stores,
        )

        refresh_browser_permissions_from_stores(
            get_install_store=get_extension_install_store,
            get_runtime_store=get_extension_runtime_store,
        )

    @router.post("/api/extensions/descriptors/validate")
    async def api_extension_descriptor_validate(
        request: Request, body: ExtensionDescriptorValidateRequest
    ):
        try:
            payload = _extension_command_service().validate_descriptor(
                ExtensionDescriptorValidateInput(
                    source_type=body.source_type,
                    descriptor_path=body.descriptor_path,
                    descriptor_url=body.descriptor_url,
                    descriptor_payload=body.descriptor_payload,
                    source_root=body.source_root,
                    platform_version=body.platform_version,
                    allow_insecure_remote=body.allow_insecure_remote,
                ),
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/extensions/descriptors/import/file")
    async def api_extension_descriptor_import_file(
        request: Request, body: ExtensionDescriptorImportFileRequest
    ):
        try:
            payload = _extension_command_service().import_descriptor_file(
                ExtensionDescriptorImportFileInput(
                    descriptor_path=body.descriptor_path,
                    install_after_import=body.install_after_import,
                    runtime_mode=body.runtime_mode,
                    remote_base_url=body.remote_base_url,
                ),
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/extensions/descriptors/import/url")
    async def api_extension_descriptor_import_url(
        request: Request, body: ExtensionDescriptorImportUrlRequest
    ):
        try:
            payload = _extension_command_service().import_descriptor_url(
                ExtensionDescriptorImportUrlInput(
                    descriptor_url=body.descriptor_url,
                    install_after_import=body.install_after_import,
                    runtime_mode=body.runtime_mode,
                    remote_base_url=body.remote_base_url,
                ),
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/extensions")
    async def api_extensions_list(request: Request):
        try:
            raw_filters = ExtensionListQueryRequest(
                q=str(request.query_params.get("q") or ""),
                state=",".join(_query_csv_values(request, "state")),
                status_group=",".join(_query_csv_values(request, "status_group")),
                runtime_mode=",".join(_query_csv_values(request, "runtime_mode")),
                entitlement_status=",".join(_query_csv_values(request, "entitlement_status")),
                origin=",".join(_query_csv_values(request, "origin")),
                source=",".join(_query_csv_values(request, "source")),
                visibility=",".join(_query_csv_values(request, "visibility")),
                has_runtime_module=_parse_optional_bool_query(
                    request.query_params.get("has_runtime_module"),
                    field_name="has_runtime_module",
                ),
                connected=_parse_optional_bool_query(
                    request.query_params.get("connected"),
                    field_name="connected",
                ),
            )
            payload = extension_query_service().list_extensions(
                filters={
                    "q": raw_filters.q,
                    "state": _query_csv_values(request, "state"),
                    "status_group": _query_csv_values(request, "status_group"),
                    "runtime_mode": _query_csv_values(request, "runtime_mode"),
                    "entitlement_status": _query_csv_values(request, "entitlement_status"),
                    "origin": _query_csv_values(request, "origin"),
                    "source": _query_csv_values(request, "source"),
                    "visibility": _query_csv_values(request, "visibility"),
                    "has_runtime_module": raw_filters.has_runtime_module,
                    "connected": raw_filters.connected,
                },
                scope=request_scope(request),
                locale=resolve_ui_locale(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except ValidationError as e:
            return service_error_response(
                ServiceValidationError(
                    "Invalid extension filters",
                    # AC17-R94: e.errors() carries the rejected value in `input`, and
                    # this payload is serialized into the response body. Filters are not
                    # secrets today, but the shape is the same one that returned a
                    # password, and whether a value is safe to echo is not something
                    # this call site can know.
                    payload={
                        "errors": validation_errors_without_url(
                            e, err_validation="Invalid extension filters"
                        )
                    },
                )
            )
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/extensions/marketplace")
    async def api_extensions_marketplace(request: Request):
        try:
            raw_filters = ExtensionMarketplaceQueryRequest(
                q=str(request.query_params.get("q") or ""),
                category=",".join(_query_csv_values(request, "category")),
                tags=",".join(_query_csv_values(request, "tags")),
                vendor=",".join(_query_csv_values(request, "vendor")),
                contains=",".join(_query_csv_values(request, "contains")),
                channel=",".join(_query_csv_values(request, "channel")),
                execution_mode=",".join(_query_csv_values(request, "execution_mode")),
                allows_self_hosted=_parse_optional_bool_query(
                    request.query_params.get("allows_self_hosted"),
                    field_name="allows_self_hosted",
                ),
                requires_connected_control_plane=_parse_optional_bool_query(
                    request.query_params.get("requires_connected_control_plane"),
                    field_name="requires_connected_control_plane",
                ),
                import_source=",".join(_query_csv_values(request, "import_source")),
                compatibility=",".join(_query_csv_values(request, "compatibility")),
                origin=",".join(_query_csv_values(request, "origin")),
                billing_mode=",".join(_query_csv_values(request, "billing_mode")),
                plan_id=",".join(_query_csv_values(request, "plan_id")),
                installed=_parse_optional_bool_query(
                    request.query_params.get("installed"),
                    field_name="installed",
                ),
                page=int(request.query_params.get("page") or 1),
                per_page=int(request.query_params.get("per_page") or 20),
                sort=str(request.query_params.get("sort") or "updated_desc"),
                facets=_parse_bool_query(
                    request.query_params.get("facets"),
                    field_name="facets",
                    default=False,
                ),
            )
            payload = extension_query_service().list_marketplace(
                filters={
                    "q": raw_filters.q,
                    "category": _query_csv_values(request, "category"),
                    "tags": _query_csv_values(request, "tags"),
                    "vendor": _query_csv_values(request, "vendor"),
                    "contains": _query_csv_values(request, "contains"),
                    "channel": _query_csv_values(request, "channel"),
                    "execution_mode": _query_csv_values(request, "execution_mode"),
                    "allows_self_hosted": raw_filters.allows_self_hosted,
                    "requires_connected_control_plane": raw_filters.requires_connected_control_plane,
                    "import_source": _query_csv_values(request, "import_source"),
                    "compatibility": _query_csv_values(request, "compatibility"),
                    "origin": _query_csv_values(request, "origin"),
                    "billing_mode": _query_csv_values(request, "billing_mode"),
                    "plan_id": _query_csv_values(request, "plan_id"),
                    "installed": raw_filters.installed,
                    "page": raw_filters.page,
                    "per_page": raw_filters.per_page,
                    "sort": raw_filters.sort,
                    "facets": raw_filters.facets,
                },
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except ValidationError as e:
            return service_error_response(
                ServiceValidationError(
                    "Invalid marketplace filters",
                    payload={
                        "errors": validation_errors_without_url(
                            e, err_validation="Invalid marketplace filters"
                        )
                    },
                )
            )
        except ValueError as e:
            return service_error_response(ServiceValidationError(str(e)))
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/extensions/installs/{install_id}")
    async def api_extension_install_detail(request: Request, install_id: str):
        try:
            payload = extension_query_service().get_install_detail(
                install_id, scope=request_scope(request)
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/extensions/{extension_id}")
    async def api_extension_detail(request: Request, extension_id: str):
        try:
            query_service = extension_query_service()
            scope = request_scope(request)
            payload = (
                query_service.get_extension(extension_id, scope=scope)
                if _request_has_admin_role(request)
                else query_service.get_extension_safe(extension_id, scope=scope)
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/extensions/releases/{release_id}/install")
    async def api_extension_release_install(
        request: Request, release_id: str, body: ExtensionReleaseInstallRequest
    ):
        try:
            payload = _extension_command_service().install_release(
                ExtensionReleaseInstallInput(
                    release_id=release_id,
                    runtime_mode=body.runtime_mode,
                    remote_base_url=body.remote_base_url,
                    approved_oauth_impact_hash=body.approved_oauth_impact_hash,
                    approved_browser_authority_impact_hash=(
                        body.approved_browser_authority_impact_hash
                    ),
                    entitlement_status=body.entitlement_status,
                ),
                scope=request_scope(request),
            )
            _reload_extension_registries()
            _refresh_extension_browser_permissions()
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/extensions/installs/{install_id}/handshake")
    async def api_extension_install_handshake(request: Request, install_id: str):
        try:
            payload = _extension_command_service().run_install_handshake(
                install_id=install_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/extensions/{extension_id}/install")
    async def api_extension_install(
        request: Request, extension_id: str, body: ExtensionInstallRequest
    ):
        # A release is the only installable, descriptor-validated unit.  Keep
        # the old route as an explicit client error rather than silently
        # creating a release-less install record.
        del request, extension_id, body
        return service_error_response(
            ServiceValidationError(
                "Direct extension install is retired; install a validated release instead"
            )
        )

    @router.post("/api/extensions/runtime/token")
    async def api_extension_runtime_token(request: Request, body: ExtensionRuntimeTokenRequest):
        try:
            payload = _extension_command_service().issue_runtime_token(
                ExtensionRuntimeTokenInput(
                    install_id=body.install_id,
                    connection_id=body.connection_id,
                    project_id=body.project_id,
                    ttl_seconds=body.ttl_seconds,
                ),
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/extensions/generated-jobs")
    async def api_extension_generated_job_create(
        request: Request, body: ExtensionGeneratedJobCreateRequest
    ):
        try:
            payload = _extension_command_service().create_generated_extension_job(
                ExtensionGeneratedJobCreateInput(
                    source_job_id=body.source_job_id,
                    request_payload=body.request_payload,
                    visibility=body.visibility,
                ),
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/extensions/{extension_id}/enable")
    async def api_extension_enable(
        request: Request,
        extension_id: str,
        body: ExtensionEnableRequest | None = None,
    ):
        try:
            payload = _extension_command_service().enable_extension(
                extension_id,
                scope=request_scope(request),
                approved_browser_authority_impact_hash=(
                    body.approved_browser_authority_impact_hash if body is not None else ""
                ),
            )
            _reload_extension_registries()
            _refresh_extension_browser_permissions()
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/extensions/{extension_id}/disable")
    async def api_extension_disable(request: Request, extension_id: str):
        try:
            payload = _extension_command_service().disable_extension(
                extension_id, scope=request_scope(request)
            )
            _reload_extension_registries()
            _refresh_extension_browser_permissions()
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/extensions/{extension_id}/uninstall-plan")
    async def api_extension_uninstall_plan(request: Request, extension_id: str):
        try:
            return _extension_lifecycle_service().preview_extension_uninstall(
                extension_id, scope=request_scope(request)
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.post("/api/extensions/{extension_id}/uninstall")
    def api_extension_uninstall_confirm(request: Request, extension_id: str, body: dict[str, Any]):
        try:
            revision = str(body.get("revision") or "").strip()
            if not revision:
                raise ServiceValidationError("Uninstall plan revision is required")
            payload = _extension_lifecycle_service().uninstall_extension(
                extension_id,
                scope=request_scope(request),
                plan_revision=revision,
            )
            _refresh_extension_browser_permissions()
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()
