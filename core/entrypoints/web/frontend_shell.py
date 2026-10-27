"""Routes for serving the React migration shell under /app/*."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol, TypeVar

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

from core.entrypoints.web.template_preview_frame import (
    TEMPLATE_PREVIEW_FRAME_HEADERS,
    TEMPLATE_PREVIEW_FRAME_HTML,
    TEMPLATE_PREVIEW_FRAME_PATH,
)

PUBLIC_ROUTE_GROUPS = frozenset(
    {
        "auth",
        "dashboard",
        "jobs",
        "projects",
        "extensions",
        "security",
        "settings",
        "monitoring",
        "trash",
        "registry",
    }
)


class _FrontendArtifacts(Protocol):
    @property
    def index_file(self) -> Path: ...


_TArtifacts = TypeVar("_TArtifacts", bound=_FrontendArtifacts)


class _ResolveFrontendAsset(Protocol[_TArtifacts]):
    def __call__(
        self,
        artifacts: _TArtifacts,
        asset_path: str,
        *,
        manifest_paths: set[str] | None = None,
    ) -> Path | None: ...


def _normalized_shell_path(path: str) -> str:
    normalized = "/" + str(path or "").strip().strip("/")
    return "/" if normalized == "/" else normalized


def build_frontend_shell_response(
    *,
    build_artifacts: Callable[[], _TArtifacts],
    frontend_shell_available: Callable[[_TArtifacts], bool],
) -> HTMLResponse | FileResponse:
    artifacts = build_artifacts()
    if not frontend_shell_available(artifacts):
        return HTMLResponse(
            (
                "React frontend build is not available. "
                "Run `cd assets/frontend && npm ci && npm run build` to create the shell bundle."
            ),
            status_code=503,
        )
    return FileResponse(
        artifacts.index_file,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


def create_frontend_shell_router(
    *,
    build_artifacts: Callable[[], _TArtifacts],
    frontend_shell_available: Callable[[_TArtifacts], bool],
    load_frontend_manifest: Callable[[_TArtifacts], dict[str, Any]],
    manifest_asset_paths: Callable[[dict[str, Any]], set[str]],
    resolve_frontend_asset: _ResolveFrontendAsset[_TArtifacts],
) -> APIRouter:
    router = APIRouter(tags=["frontend-shell"])

    def _shell_response():
        return build_frontend_shell_response(
            build_artifacts=build_artifacts,
            frontend_shell_available=frontend_shell_available,
        )

    @router.get("/app/assets/{asset_path:path}", include_in_schema=False)
    async def app_assets(asset_path: str):
        artifacts = build_artifacts()
        manifest_paths = manifest_asset_paths(load_frontend_manifest(artifacts))
        asset = resolve_frontend_asset(artifacts, asset_path, manifest_paths=manifest_paths)
        if asset is None:
            return HTMLResponse("Frontend asset not found.", status_code=404)

        return FileResponse(
            asset,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # Registered before the /app catch-all, which would otherwise serve the shell.
    @router.get(TEMPLATE_PREVIEW_FRAME_PATH, include_in_schema=False)
    async def template_preview_frame():
        return HTMLResponse(TEMPLATE_PREVIEW_FRAME_HTML, headers=TEMPLATE_PREVIEW_FRAME_HEADERS)

    @router.get("/app", include_in_schema=False)
    @router.get("/app/{path:path}", include_in_schema=False)
    async def app_shell(path: str = ""):
        return _shell_response()

    enabled_public_groups = set(PUBLIC_ROUTE_GROUPS)

    if "auth" in enabled_public_groups:

        @router.get("/", include_in_schema=False)
        @router.get("/login", include_in_schema=False)
        @router.get("/bootstrap", include_in_schema=False)
        @router.get("/forgot-password", include_in_schema=False)
        @router.get("/reset-password", include_in_schema=False)
        async def public_auth_shell():
            return _shell_response()

    if "dashboard" in enabled_public_groups:

        @router.get("/dashboard", include_in_schema=False)
        async def public_dashboard_shell():
            return _shell_response()

    if "jobs" in enabled_public_groups:

        @router.get("/jobs", include_in_schema=False)
        @router.get("/jobs/{job_id}", include_in_schema=False)
        async def public_jobs_shell(job_id: str = ""):
            _ = job_id
            return _shell_response()

    if "projects" in enabled_public_groups:

        @router.get("/projects", include_in_schema=False)
        @router.get("/projects/{project_id}", include_in_schema=False)
        async def public_projects_shell(project_id: str = ""):
            _ = project_id
            return _shell_response()

    if "extensions" in enabled_public_groups:

        @router.get("/extensions", include_in_schema=False)
        @router.get("/extensions/{extension_id}", include_in_schema=False)
        async def public_extensions_shell(extension_id: str = ""):
            _ = extension_id
            return _shell_response()

    if "security" in enabled_public_groups:

        @router.get("/security", include_in_schema=False)
        @router.get("/security/projects/{security_project_id}", include_in_schema=False)
        async def public_security_shell(security_project_id: str = ""):
            _ = security_project_id
            return _shell_response()

    if "settings" in enabled_public_groups:

        @router.get("/settings", include_in_schema=False)
        @router.get("/settings/general", include_in_schema=False)
        @router.get("/settings/account", include_in_schema=False)
        @router.get("/settings/users", include_in_schema=False)
        @router.get("/settings/providers", include_in_schema=False)
        @router.get("/settings/service-accounts", include_in_schema=False)
        @router.get("/settings/security", include_in_schema=False)
        @router.get("/settings/retention", include_in_schema=False)
        @router.get("/settings/notifications", include_in_schema=False)
        async def public_settings_shell():
            return _shell_response()

    if "monitoring" in enabled_public_groups:

        @router.get("/monitoring", include_in_schema=False)
        async def public_monitoring_shell():
            return _shell_response()

    if "trash" in enabled_public_groups:

        @router.get("/trash", include_in_schema=False)
        async def public_trash_shell():
            return _shell_response()

    if "registry" in enabled_public_groups:

        @router.get("/registry", include_in_schema=False)
        @router.get("/registry/changelog", include_in_schema=False)
        @router.get("/registry/workflows", include_in_schema=False)
        @router.get("/registry/workflows/{workflow_id}", include_in_schema=False)
        @router.get("/registry/workflows/{workflow_id}/builder", include_in_schema=False)
        @router.get("/registry/workflows/{workflow_id}/projects", include_in_schema=False)
        @router.get("/registry/agents", include_in_schema=False)
        @router.get("/registry/agents/{agent_id}", include_in_schema=False)
        @router.get("/registry/mcp", include_in_schema=False)
        @router.get("/registry/mcp/{connector_id}", include_in_schema=False)
        @router.get("/registry/tools", include_in_schema=False)
        @router.get("/registry/tools/{tool_id}", include_in_schema=False)
        @router.get("/registry/skills", include_in_schema=False)
        @router.get("/registry/skills/{skill_id}", include_in_schema=False)
        @router.get("/registry/guardrails", include_in_schema=False)
        @router.get("/registry/guardrails/{guardrail_id}", include_in_schema=False)
        @router.get("/registry/data-connections", include_in_schema=False)
        @router.get("/registry/datasets", include_in_schema=False)
        @router.get("/registry/datasets/{dataset_id}", include_in_schema=False)
        @router.get("/registry/workspaces", include_in_schema=False)
        @router.get("/registry/workspaces/{workspace_id}", include_in_schema=False)
        @router.get("/registry/data-contracts", include_in_schema=False)
        @router.get("/registry/data-contracts/{contract_id}", include_in_schema=False)
        @router.get("/registry/fields", include_in_schema=False)
        @router.get("/registry/fields/{field_key}", include_in_schema=False)
        @router.get("/registry/published-blocks", include_in_schema=False)
        @router.get("/registry/published-blocks/{block_key}", include_in_schema=False)
        async def public_registry_shell(
            workflow_id: str = "",
            agent_id: str = "",
            connector_id: str = "",
            tool_id: str = "",
            skill_id: str = "",
            guardrail_id: str = "",
            dataset_id: str = "",
            workspace_id: str = "",
            contract_id: str = "",
            field_key: str = "",
            block_key: str = "",
        ):
            _ = (
                workflow_id,
                agent_id,
                connector_id,
                tool_id,
                skill_id,
                guardrail_id,
                dataset_id,
                workspace_id,
                contract_id,
                field_key,
                block_key,
            )
            return _shell_response()

    return router


def create_registered_frontend_shell_router(
    *,
    registered_paths: Iterable[str],
    build_artifacts: Callable[[], _TArtifacts],
    frontend_shell_available: Callable[[_TArtifacts], bool],
) -> APIRouter:
    router = APIRouter(tags=["frontend-shell"])
    paths = sorted(
        {
            _normalized_shell_path(path)
            for path in registered_paths
            if _normalized_shell_path(path) not in {"", "/"}
        }
    )

    def registered_shell():
        return build_frontend_shell_response(
            build_artifacts=build_artifacts,
            frontend_shell_available=frontend_shell_available,
        )

    for path in paths:
        router.add_api_route(
            path,
            registered_shell,
            methods=["GET"],
            include_in_schema=False,
        )
    return router
