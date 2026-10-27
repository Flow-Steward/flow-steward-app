from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from core.application.reports.ui_page_registry import register_core_settings_pages
from core.entrypoints.web.frontend_shell import create_frontend_shell_router
from core.infrastructure.runtime.frontend_shell_runtime import (
    build_frontend_shell_artifacts,
    frontend_shell_available,
    load_frontend_manifest,
    manifest_asset_paths,
    resolve_frontend_asset,
)


def _build_client(dist_root: Path) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_frontend_shell_router(
            build_artifacts=lambda: build_frontend_shell_artifacts(dist_root),
            frontend_shell_available=frontend_shell_available,
            load_frontend_manifest=load_frontend_manifest,
            manifest_asset_paths=manifest_asset_paths,
            resolve_frontend_asset=resolve_frontend_asset,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def _build_public_conflict_client(*, dist_root: Path) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_frontend_shell_router(
            build_artifacts=lambda: build_frontend_shell_artifacts(dist_root),
            frontend_shell_available=frontend_shell_available,
            load_frontend_manifest=load_frontend_manifest,
            manifest_asset_paths=manifest_asset_paths,
            resolve_frontend_asset=resolve_frontend_asset,
        )
    )

    template_router = APIRouter()

    @template_router.get("/dashboard")
    async def _template_dashboard():
        return HTMLResponse("TEMPLATE_DASHBOARD")

    @template_router.get("/settings")
    async def _template_settings():
        return HTMLResponse("TEMPLATE_SETTINGS")

    @template_router.get("/registry")
    async def _template_registry():
        return HTMLResponse("TEMPLATE_REGISTRY")

    app.include_router(template_router)
    return TestClient(app, raise_server_exceptions=False)


def _prepare_dist(dist_root: Path) -> None:
    assets_root = dist_root / "assets"
    manifest_root = dist_root / ".vite"
    assets_root.mkdir(parents=True)
    manifest_root.mkdir(parents=True)
    (dist_root / ("index." + "html")).write_text(
        "<!doctype html><div id='root'></div>",
        encoding="utf-8",
    )
    (assets_root / "main.js").write_text("console.log('shell')", encoding="utf-8")
    (manifest_root / "manifest.json").write_text(
        '{"src/main.tsx": {"file": "assets/main.js"}}',
        encoding="utf-8",
    )


def test_frontend_shell_returns_503_when_build_missing() -> None:
    client = _build_client(Path("/tmp/nonexistent-frontend-dist"))

    app_shell = client.get("/app")
    public_root = client.get("/")
    public_dashboard = client.get("/dashboard")
    public_login = client.get("/login")

    assert app_shell.status_code == 503
    assert public_root.status_code == 503
    assert public_dashboard.status_code == 503
    assert public_login.status_code == 503
    assert "npm ci" in app_shell.text


def test_frontend_shell_serves_index_and_assets(tmp_path: Path) -> None:
    dist_root = tmp_path / "dist"
    _prepare_dist(dist_root)

    client = _build_client(dist_root)

    shell = client.get("/app/dashboard")
    root_shell = client.get("/")
    public_shell = client.get("/dashboard")
    workflow_shell = client.get("/registry/workflows/demo")
    asset = client.get("/app/assets/main.js")

    assert shell.status_code == 200
    assert root_shell.status_code == 200
    assert public_shell.status_code == 200
    assert workflow_shell.status_code == 200
    assert shell.headers["cache-control"] == "no-store"
    assert root_shell.headers["cache-control"] == "no-store"
    assert public_shell.headers["cache-control"] == "no-store"
    assert "<div id='root'></div>" in shell.text
    assert "<div id='root'></div>" in public_shell.text
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/login",
        "/bootstrap",
        "/forgot-password",
        "/reset-password",
        "/dashboard",
        "/jobs",
        "/jobs/42",
        "/projects",
        "/projects/42",
        "/extensions",
        "/extensions/demo-connector",
        "/security",
        "/security/projects/42",
        "/settings",
        "/settings/retention",
        "/settings/users",
        "/monitoring",
        "/trash",
        "/registry",
        "/registry/workspaces",
        "/registry/workspaces/00000000-0000-0000-0000-000000000003",
        "/registry/datasets",
        "/registry/datasets/customer-orders",
        "/registry/tools",
        "/registry/tools/demo-tool",
        "/registry/agents",
        "/registry/agents/developer",
        "/registry/changelog",
        "/registry/workflows",
        "/registry/workflows/wf-42",
        "/registry/workflows/wf-42/builder",
        "/registry/workflows/wf-42/projects",
    ],
)
def test_frontend_shell_serves_canonical_public_routes(tmp_path: Path, path: str) -> None:
    dist_root = tmp_path / "dist"
    _prepare_dist(dist_root)

    client = _build_client(dist_root)
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "<div id='root'></div>" in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/projects/42/kb",
        # One-level project page: these sections are tabs of /projects/42 now.
        "/projects/42/edit",
        "/projects/42/history",
        "/projects/42/scheduled-jobs",
        "/projects/42/queue",
    ],
)
def test_frontend_shell_does_not_serve_retired_project_routes(tmp_path: Path, path: str) -> None:
    dist_root = tmp_path / "dist"
    _prepare_dist(dist_root)

    response = _build_client(dist_root).get(path)

    assert response.status_code == 404


def test_frontend_shell_serves_registered_settings_tab_routes(tmp_path: Path) -> None:
    dist_root = tmp_path / "dist"
    _prepare_dist(dist_root)

    registries = {"settings_pages": {}, "navigation_items": {}}
    register_core_settings_pages(registries)
    settings_tab_paths = sorted(
        str(record["path"])
        for record in registries["settings_pages"].values()
        if record.get("settings_tab")
    )

    client = _build_client(dist_root)

    assert settings_tab_paths
    for path in settings_tab_paths:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-store"
        assert "<div id='root'></div>" in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/events",
        "/account",
        "/account-settings",
        "/users",
        "/datasets",
        "/datasets/customer-orders",
        "/system/monitoring",
        "/processes",
        "/workflows",
        "/workflows/wf-42",
        "/jobs/42/logs",
        "/jobs/42/events",
        "/projects/42/connections",
        "/projects/42/datasets",
        "/projects/42/workflows",
        "/projects/42/policy",
        "/projects/42/reindex",
        "/projects/42/not-a-real-page",
        "/registry/agents/developer/history",
        "/registry/agents/developer/not-a-real-tab",
        "/registry/tools/demo-tool/history",
        "/registry/workspaces/00000000-0000-0000-0000-000000000003/history",
        "/registry/workflows/wf-42/simulate",
        "/registry/workflows/wf-42/reactions",
        "/chat/not-a-real-page",
    ],
)
def test_frontend_shell_rejects_removed_public_aliases(tmp_path: Path, path: str) -> None:
    dist_root = tmp_path / "dist"
    _prepare_dist(dist_root)

    client = _build_client(dist_root)
    response = client.get(path)

    assert response.status_code == 404
    assert "<div id='root'></div>" not in response.text


def test_public_routes_are_owned_by_react_shell_even_with_template_conflicts(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    _prepare_dist(dist_root)

    client = _build_public_conflict_client(dist_root=dist_root)

    dashboard = client.get("/dashboard")
    settings = client.get("/settings")
    registry = client.get("/registry")
    app_dashboard = client.get("/app/dashboard")

    assert dashboard.status_code == 200
    assert settings.status_code == 200
    assert registry.status_code == 200
    assert app_dashboard.status_code == 200
    assert "<div id='root'></div>" in dashboard.text
    assert "<div id='root'></div>" in settings.text
    assert "<div id='root'></div>" in registry.text
    assert "<div id='root'></div>" in app_dashboard.text
    assert "TEMPLATE_DASHBOARD" not in dashboard.text
    assert "TEMPLATE_SETTINGS" not in settings.text
    assert "TEMPLATE_REGISTRY" not in registry.text


@pytest.mark.parametrize(
    "path", ["/chat", "/chat/shell", "/chat/contextual-assistant", "/settings/assistant"]
)
def test_frontend_shell_does_not_serve_retired_management_chat_routes(
    tmp_path: Path, path: str
) -> None:
    dist_root = tmp_path / "dist"
    _prepare_dist(dist_root)

    assert _build_client(dist_root).get(path).status_code == 404


def test_template_preview_frame_has_its_own_narrow_policy(tmp_path: Path) -> None:
    """A Template preview loads HTTPS images, yet runs no script but its own.

    A ``srcdoc`` frame inherits the admin policy (images from this server only),
    so the preview is served as a page of its own. The global security headers
    must not replace its policy.
    """
    import base64
    import hashlib
    import re

    from core.entrypoints.web.template_preview_frame import TEMPLATE_PREVIEW_FRAME_PATH
    from core.infrastructure.runtime.http_app_lifecycle import _security_headers_middleware

    app = FastAPI()
    app.middleware("http")(_security_headers_middleware)
    app.include_router(
        create_frontend_shell_router(
            build_artifacts=lambda: build_frontend_shell_artifacts(tmp_path),
            frontend_shell_available=frontend_shell_available,
            load_frontend_manifest=load_frontend_manifest,
            manifest_asset_paths=manifest_asset_paths,
            resolve_frontend_asset=resolve_frontend_asset,
        )
    )
    response = TestClient(app).get(TEMPLATE_PREVIEW_FRAME_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    directives = {
        part.strip().split(" ", 1)[0]: part.strip()
        for part in response.headers["content-security-policy"].split(";")
    }
    assert directives["sandbox"] == "sandbox allow-scripts"
    assert directives["img-src"] == "img-src https: data:"
    assert directives["default-src"] == "default-src 'none'"
    assert directives["frame-src"] == "frame-src 'none'"
    assert directives["form-action"] == "form-action 'none'"
    assert directives["frame-ancestors"] == "frame-ancestors 'self'"
    assert "allow-same-origin" not in response.headers["content-security-policy"]
    # The only script allowed is the page's own, pinned by its hash.
    scripts = re.findall(r"<script>(.*?)</script>", response.text, re.S)
    assert len(scripts) == 1
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
    assert directives["script-src"] == f"script-src 'sha256-{digest}'"
    assert "unsafe-inline" not in directives["script-src"]
    # It accepts content only from the window that embeds it, on this origin.
    assert "event.source!==window.parent" in scripts[0]
    assert "event.origin!==own" in scripts[0]
    # A link in the preview does not load another site into the frame.
    assert "closest('a')" in scripts[0]
    assert "preventDefault" in scripts[0]
