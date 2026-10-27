"""Reaction, extension, and command dispatch API routes extracted from serve.py."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from core.application.extensions.browser_assets import resolve_authorized_browser_asset
from core.application.extensions.direct_effect_recovery_models import DirectEffectDecisionRequest
from core.application.management import ServiceError
from core.application.management.command_models import (
    CommandBatchInput,
    CommandDispatchInput,
    ReactionEventIngestInput,
    WorkflowReactionsUpdateInput,
)
from core.application.management.requests import (
    CommandBatchRequest,
    CommandDispatchRequest,
    ExtensionActionRequest,
    ExtensionInvokeRequest,
    ExtensionQueryRequest,
    ExtensionScanRequest,
    ReactionIngestRequest,
    WorkflowReactionsRequest,
)

# DoS hardening: bound the extension archive upload at read time so an oversized
# multipart body is rejected BEFORE the whole archive is buffered in memory.
_MAX_EXTENSION_ARCHIVE_BYTES = 50 * 1024 * 1024
_EXTENSION_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


def _request_has_admin_role(request: Request) -> bool:
    auth_user = getattr(request.state, "auth_user", None)
    roles = (
        {str(role or "").strip().lower() for role in (auth_user.get("roles") or [])}
        if isinstance(auth_user, Mapping)
        else set()
    )
    return "admin" in roles


async def _read_extension_upload_with_limit(upload: UploadFile, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_EXTENSION_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _extension_request_project_id(body: Any) -> str | None:
    context = getattr(body, "context", None)
    if isinstance(context, dict):
        project_id = str(context.get("project_id") or "").strip()
        if project_id:
            return project_id

    request_payload = getattr(body, "request", None)
    if isinstance(request_payload, dict):
        request_context = request_payload.get("context")
        if isinstance(request_context, dict):
            project_id = str(request_context.get("project_id") or "").strip()
            if project_id:
                return project_id
        project_id = str(request_payload.get("project_id") or "").strip()
        if project_id:
            return project_id
    return None


def create_reactions_extensions_router(
    *,
    event_query_service: Callable[[], Any],
    event_command_service: Callable[[], Any],
    job_query_service: Callable[[], Any],
    extension_query_service: Callable[[], Any],
    extension_binding_service: Callable[[], Any],
    tool_execution_service: Callable[[], Any],
    command_dispatch_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> APIRouter:
    router = APIRouter(tags=["reactions-extensions"])

    @router.get("/api/workflows/{workflow_id}/reactions")
    async def api_workflow_reactions(
        request: Request,
        workflow_id: str,
        scope_kind: Annotated[str, Query()] = "workflow",
        scope_id: Annotated[str, Query()] = "",
        project_id: Annotated[str | None, Query()] = None,
    ):
        try:
            payload = event_query_service().list_workflow_reactions(
                workflow_id,
                scope_kind=scope_kind,
                scope_id=scope_id,
                scope=request_scope(request, workflow_id=workflow_id, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.post("/api/workflows/{workflow_id}/reactions")
    async def api_update_workflow_reactions(
        request: Request,
        workflow_id: str,
        body: WorkflowReactionsRequest,
        scope_kind: Annotated[str, Query()] = "workflow",
        scope_id: Annotated[str, Query()] = "",
        project_id: Annotated[str | None, Query()] = None,
    ):
        try:
            payload = event_command_service().update_workflow_reactions(
                workflow_id,
                WorkflowReactionsUpdateInput.model_validate(body.model_dump()),
                scope_kind=scope_kind,
                scope_id=scope_id,
                scope=request_scope(request, workflow_id=workflow_id, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.post("/api/reactions/ingest")
    async def api_ingest_reaction_event(request: Request, body: ReactionIngestRequest):
        try:
            payload = event_command_service().ingest_reaction_event(
                ReactionEventIngestInput(
                    event_type=body.event_type,
                    job_id=body.job_id,
                    workflow_id=body.workflow_id,
                    source=body.source,
                    source_id=body.source_id,
                    event_key=body.event_key,
                    dedupe_key=body.dedupe_key,
                    payload=body.payload,
                ),
                scope=request_scope(request, workflow_id=body.workflow_id or None),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.get("/api/jobs/{job_id}/reactions")
    async def api_job_reactions(request: Request, job_id: str):
        try:
            payload = job_query_service().list_reactions(
                job_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.post("/api/extensions/install")
    async def api_install_extension_from_file(
        request: Request,
        file: Annotated[UploadFile, File(...)],
        approved_browser_authority_impact_hash: Annotated[
            str,
            Form(max_length=64, pattern=r"^(?:|[a-f0-9]{64})$"),
        ] = "",
    ):
        try:
            lifecycle_service = extension_binding_service()
            lifecycle_service.require_archive_lifecycle_available(operation="install_update")
            try:
                content = await _read_extension_upload_with_limit(
                    file,
                    max_bytes=_MAX_EXTENSION_ARCHIVE_BYTES,
                )
            except ValueError:
                return JSONResponse(
                    {
                        "error": (
                            "Extension archive is too large — "
                            f"maximum {_MAX_EXTENSION_ARCHIVE_BYTES} bytes"
                        )
                    },
                    status_code=413,
                )
            payload = lifecycle_service.install_extension_archive(
                filename=str(file.filename or "").strip(),
                content=content,
                scope=request_scope(request),
                approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        finally:
            await file.close()
        return payload.dump()

    @router.get("/api/extensions/capabilities/archive-lifecycle")
    async def api_extension_archive_lifecycle_capability(request: Request):
        try:
            payload = extension_binding_service().archive_lifecycle_availability()
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.post("/api/extensions/scan")
    async def api_extensions_scan(request: Request, body: ExtensionScanRequest):
        try:
            payload = extension_binding_service().scan_extensions(
                root=body.root,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.get("/api/extensions/{extension_id}/preview")
    async def api_extension_preview(request: Request, extension_id: str):
        try:
            return extension_query_service().get_preview(
                extension_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)

    @router.get("/api/extensions/{extension_id}/compiled-manifest")
    async def api_extension_compiled_manifest(request: Request, extension_id: str):
        try:
            query_service = extension_query_service()
            scope = request_scope(request)
            return (
                query_service.get_compiled_manifest(extension_id, scope=scope)
                if _request_has_admin_role(request)
                else query_service.get_compiled_manifest_safe(extension_id, scope=scope)
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)

    @router.get("/api/extensions/{extension_id}/icon")
    async def api_extension_icon(request: Request, extension_id: str):
        """The installed extension's logo (its bundle's icon.png), for whoever may see it."""
        try:
            data = extension_query_service().get_extension_icon(
                extension_id, scope=request_scope(request)
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return Response(
            content=data,
            media_type="image/png",
            headers={
                # The URL the projections hand out names the file's identity.
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            },
        )

    @router.get("/api/extensions/{extension_id}/assets/{asset_ref:path}")
    async def api_extension_browser_asset(
        request: Request,
        extension_id: str,
        asset_ref: str,
        extension_install_id: Annotated[str, Query(min_length=1, max_length=128)],
        release_id: Annotated[str, Query(min_length=1, max_length=128)],
        descriptor_sha256: Annotated[str, Query(min_length=64, max_length=64)],
        project_id: Annotated[str, Query(min_length=1, max_length=128)],
    ):
        try:
            scope = request_scope(request, project_id=project_id)
            manifest = extension_query_service().get_browser_asset_manifest(
                extension_id,
                install_id=extension_install_id,
                release_id=release_id,
                descriptor_sha256=descriptor_sha256,
                project_id=project_id,
                scope=scope,
            )
            asset_path = resolve_authorized_browser_asset(
                manifest,
                asset_ref=asset_ref,
                extension_id=extension_id,
                install_id=extension_install_id,
                release_id=release_id,
                descriptor_sha256=descriptor_sha256,
                project_id=project_id,
                scope=scope,
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return FileResponse(
            asset_path,
            media_type="application/javascript",
            headers={
                "Cache-Control": "private, max-age=300",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/api/extensions/{extension_id}/health")
    async def api_extension_health(request: Request, extension_id: str):
        try:
            payload = extension_query_service().get_health(
                extension_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.get("/api/projects/{project_id}/extensions/{install_id}/external-effects")
    async def api_direct_external_effects(request: Request, project_id: str, install_id: str):
        try:
            payload = extension_binding_service().list_external_effects(
                install_id,
                project_id=project_id,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.post(
        "/api/projects/{project_id}/extensions/{install_id}/external-effects/{effect_id}/resolution"
    )
    async def api_direct_external_effect_resolution(
        request: Request,
        project_id: str,
        install_id: str,
        effect_id: str,
        body: DirectEffectDecisionRequest,
    ):
        try:
            payload = extension_binding_service().resolve_external_effect(
                install_id,
                effect_id,
                body,
                project_id=project_id,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.post("/api/extensions/{extension_id}/query/{query_id}")
    async def api_extension_query(
        request: Request, extension_id: str, query_id: str, body: ExtensionQueryRequest
    ):
        try:
            payload = extension_binding_service().execute_query(
                extension_id,
                query_id,
                body,
                scope=request_scope(request, project_id=_extension_request_project_id(body)),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.post("/api/extensions/{extension_id}/action/{action_id}")
    async def api_extension_action(
        request: Request, extension_id: str, action_id: str, body: ExtensionActionRequest
    ):
        try:
            payload = extension_binding_service().execute_action(
                extension_id,
                action_id,
                body,
                scope=request_scope(request, project_id=_extension_request_project_id(body)),
                idempotency_key=str(request.headers.get("Idempotency-Key") or ""),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.post("/api/extensions/{extension_id}/invoke")
    async def api_extension_invoke(
        request: Request, extension_id: str, body: ExtensionInvokeRequest
    ):
        try:
            payload = tool_execution_service().invoke_extension(
                extension_id,
                body,
                scope=request_scope(request, project_id=_extension_request_project_id(body)),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.post("/api/commands")
    async def api_command_dispatch(request: Request, body: CommandDispatchRequest):
        try:
            payload = command_dispatch_service().dispatch(
                CommandDispatchInput(
                    command_type=body.command_type,
                    target=body.target,
                    payload=body.payload,
                    source=body.source,
                    correlation_id=body.correlation_id,
                ),
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.post("/api/commands/batch")
    async def api_command_batch_dispatch(request: Request, body: CommandBatchRequest):
        try:
            payload = command_dispatch_service().dispatch_batch(
                CommandBatchInput(
                    commands=[
                        CommandDispatchInput(
                            command_type=c.command_type,
                            target=c.target,
                            payload=c.payload,
                            source=c.source,
                            correlation_id=c.correlation_id,
                        )
                        for c in body.commands
                    ]
                ),
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    return router
