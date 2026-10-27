from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI

import core.entrypoints.web.frontend_shell as frontend_shell
from core.entrypoints.api import route_registration
from core.infrastructure.runtime import http_app_lifecycle

_PUBLIC_ROUTE_GROUP_FILES = {
    "auth": (
        "assets/frontend/src/pages/auth/login/index.tsx",
        "assets/frontend/src/pages/auth/bootstrap/index.tsx",
        "assets/frontend/src/pages/auth/reset-password/index.tsx",
    ),
    "dashboard": ("assets/frontend/src/pages/dashboard/index.tsx",),
    "jobs": ("assets/frontend/src/pages/jobs/detail/index.tsx",),
    "projects": (
        "assets/frontend/src/pages/projects/detail/index.tsx",
        "assets/frontend/src/pages/projects/datasets/index.tsx",
        "assets/frontend/src/pages/projects/notifications/index.tsx",
        "assets/frontend/src/pages/projects/notifications/overview.tsx",
    ),
    "extensions": ("assets/frontend/src/pages/extensions/index.tsx",),
    "security": ("assets/frontend/src/pages/settings/security/index.tsx",),
    "settings": (
        "assets/frontend/src/pages/settings/account/index.tsx",
        "assets/frontend/src/pages/settings/general/index.tsx",
        "assets/frontend/src/pages/settings/notifications/index.tsx",
    ),
    "monitoring": ("assets/frontend/src/pages/monitoring/index.tsx",),
    "trash": ("assets/frontend/src/pages/trash/index.tsx",),
    "registry": (
        "assets/frontend/src/pages/registry/agents/index.tsx",
        "assets/frontend/src/pages/registry/datasets/index.tsx",
        "assets/frontend/src/pages/registry/datasets/detail/index.tsx",
        "assets/frontend/src/pages/workflows/detail/index.tsx",
        "assets/frontend/src/pages/workflows/projects/index.tsx",
    ),
}

_UNFINISHED_MARKERS = (
    "JsonRecordWorkbench",
    "JsonCollectionWorkbench",
    "PagePlaceholder",
    "<pre>{JSON.stringify(",
)

_CRITICAL_SURFACE_MARKERS = {
    "assets/frontend/src/components/layout/AppShell.tsx": (
        "shell_navigation",
        "shell.nav.main",
        "shell.nav.system",
        'to: "/registry/mcp"',
        'to: "/registry/datasets"',
    ),
    "assets/frontend/src/app/router.tsx": (
        'path="forgot-password" element={<ResetPasswordPage />}',
        'path="reset-password" element={<ResetPasswordPage />}',
        'element={<RequireCapability capability="administer_platform"><SettingsHubPage /></RequireCapability>}',
        'path="extensions" element={<RequireCapability capability="administer_platform"><ExtensionsHubPage /></RequireCapability>}',
        'path="agents/:agentId" element={<RegistryAgentsDetailPage />}',
        'path="mcp" element={<RegistryMcpPage />}',
        'path="datasets" element={<RegistryDatasetsPage />}',
    ),
    "assets/frontend/src/pages/dashboard/index.tsx": (
        "CommandCenter,",
        "<CommandCenter",
    ),
    "assets/frontend/src/pages/dashboard/command-center.tsx": (
        'data-testid="cmd-center"',
        'id="cmd-center"',
    ),
    # Trash moved from the retired platform-grid bridge onto the published
    # DsTable. The contract is the same — the surface still renders a real data
    # grid — so the marker follows the component that provides it.
    "assets/frontend/src/pages/trash/index.tsx": (
        'id="trash-search"',
        "<DsTable",
        'action_id: "restore"',
        'action_id: "hard_delete"',
    ),
    # Both grids kept their identifiers when they moved onto DsTable; they are
    # test ids on the surface now rather than keys in a bridge config object.
    "assets/frontend/src/pages/monitoring/shared/processes/index.tsx": (
        'data-testid="proc-jobs-grid"',
        'data-testid="proc-workers-grid"',
        'action_id: "bulk_cancel"',
        'action_id: "bulk_kill"',
    ),
    # One level: every project section is a tab of the project page.
    "assets/frontend/src/pages/projects/detail/index.tsx": (
        '{ key: "data-connections"',
        '{ key: "history"',
    ),
    "assets/frontend/src/pages/projects/detail/project-edit-form.tsx": (
        "<ProjectHistoryPanel",
        "disabled={readOnly}",
    ),
    "assets/frontend/src/pages/projects/notifications/index.tsx": (
        "Notification routes",
        'id="notification-routes-add"',
        "<NotificationRouteDialog",
    ),
    "assets/frontend/src/pages/projects/notifications/notification-route-dialog.tsx": (
        'id="notification-route-save"',
    ),
    "assets/frontend/src/pages/workflows/projects/index.tsx": (
        "WorkflowProjectBindingsPanel",
        'id="workflow-project-bindings"',
        "Save project bindings",
    ),
    "assets/frontend/src/pages/workflows/builder/index.tsx": (
        "WorkflowsDetailPage",
        "fixed inset-0",
        "overflow-hidden",
        'embedded surface="builder"',
    ),
    "assets/frontend/src/pages/jobs/list/api.ts": (
        "job_id",
        "normalizeRow",
    ),
}

_DISALLOWED_PARITY_MARKERS = {
    "assets/frontend/src/pages/workflows/builder/index.tsx": (
        "React Flow builder foundation",
        "Manage phase steps, ordering, and step-level patch payloads.",
        "Phase authoring",
        "Step authoring",
        "<WorkflowsDetailPage embedded />",
    ),
}

_REGISTRY_REDIRECT_MARKERS = (
    'QueryAliasRedirect to="/registry/agents"',
    'QueryAliasRedirect to="/registry/tools"',
    'QueryAliasRedirect to="/registry/skills"',
    'QueryAliasRedirect to="/registry/guardrails"',
    'QueryAliasRedirect to="/registry/data-contracts"',
    'QueryAliasRedirect to="/registry/fields"',
    'QueryAliasRedirect to="/registry/published-blocks"',
)


def test_frontend_shell_router_is_registered_without_cutover_toggle_kwargs(monkeypatch) -> None:
    captured_kwargs: list[set[str]] = []

    def _fake_create_frontend_shell_router(**kwargs):
        captured_kwargs.append(set(kwargs))
        return APIRouter()

    monkeypatch.setattr(
        frontend_shell, "create_frontend_shell_router", _fake_create_frontend_shell_router
    )
    route_registration._register_frontend_shell_routes(FastAPI())

    assert captured_kwargs
    assert "public_routes_enabled" not in captured_kwargs[0]
    assert "public_route_groups_enabled" not in captured_kwargs[0]


def test_static_asset_routes_are_registered_without_template_runtime() -> None:
    app = FastAPI()
    http_app_lifecycle._register_static_assets(app)

    route_paths = {getattr(route, "path", "") for route in app.routes}
    assert "/static" in route_paths
    assert "/static-v/{asset_version}/{file_path:path}" in route_paths
    assert "/favicon.ico" in route_paths
    assert not hasattr(app.state, "templates")


def test_public_route_group_file_map_matches_router_groups() -> None:
    assert set(_PUBLIC_ROUTE_GROUP_FILES) == set(frontend_shell.PUBLIC_ROUTE_GROUPS)


def test_public_react_route_files_do_not_contain_unfinished_shell_markers() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for group in frontend_shell.PUBLIC_ROUTE_GROUPS:
        for relative_path in _PUBLIC_ROUTE_GROUP_FILES[group]:
            file_path = repo_root / relative_path
            contents = file_path.read_text(encoding="utf-8")
            for marker in _UNFINISHED_MARKERS:
                assert marker not in contents, (
                    f"{relative_path} contains unfinished marker: {marker}"
                )


def test_critical_public_surfaces_keep_behavioral_markers() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for relative_path, markers in _CRITICAL_SURFACE_MARKERS.items():
        contents = (repo_root / relative_path).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in contents, (
                f"{relative_path} is missing critical surface marker: {marker}"
            )


def test_settings_hub_tabs_share_platform_admin_guard() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    router_contents = (repo_root / "assets/frontend/src/app/router.tsx").read_text(encoding="utf-8")
    # The hub now owns a parent route so its tab row survives navigation. Its
    # index and tab routes must stay inside the same capability boundary.
    guarded_parent = (
        '<Route element={<RequireCapability capability="administer_platform">'
        "<SettingsHubPage /></RequireCapability>}>"
    )
    assert guarded_parent in router_contents
    hub_children = router_contents.split(guarded_parent, 1)[1].split("</Route>", 1)[0]
    assert "<Route index />" in hub_children
    for tab in (
        "general",
        "providers",
        "service-accounts",
        "security",
        "retention",
        "notifications",
    ):
        assert f'<Route path="{tab}" />' in hub_children, (
            f"Settings tab {tab} must remain inside the platform admin guard"
        )


def test_registry_detail_routes_do_not_fall_back_to_list_redirect_aliases() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    router_contents = (repo_root / "assets/frontend/src/app/router.tsx").read_text(encoding="utf-8")
    for marker in _REGISTRY_REDIRECT_MARKERS:
        assert marker not in router_contents, (
            f"registry detail route still redirects via alias: {marker}"
        )


def test_parity_gate_rejects_low_level_builder_markers() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for relative_path, markers in _DISALLOWED_PARITY_MARKERS.items():
        contents = (repo_root / relative_path).read_text(encoding="utf-8")
        for marker in markers:
            assert marker not in contents, (
                f"{relative_path} still contains deprecated parity marker: {marker}"
            )


def test_project_page_owns_editing_surfaces() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    # One level: the project page's own tab set holds every section, so no
    # sub-navigation and no separate Configuration screen exist any more.
    assert not (repo_root / "assets/frontend/src/pages/projects/shared").exists()
    assert not (repo_root / "assets/frontend/src/pages/projects/edit").exists()

    router = (repo_root / "assets/frontend/src/app/router.tsx").read_text(encoding="utf-8")
    assert "ProjectConfigRedirect" not in router
    assert "ProjectReindexRedirect" not in router
    assert 'path=":projectId/reindex"' not in router
    for marker in (
        'path=":projectId/connections"',
        'path=":projectId/datasets"',
        'path=":projectId/notifications"',
        'path=":projectId/workflows"',
        'path=":projectId/policy"',
        'path=":projectId/edit"',
        'path=":projectId/history"',
        'path=":projectId/scheduled-jobs"',
        'path=":projectId/queue"',
    ):
        assert marker not in router

    edit = (repo_root / "assets/frontend/src/pages/projects/detail/index.tsx").read_text(
        encoding="utf-8"
    )
    edit_form = (
        repo_root / "assets/frontend/src/pages/projects/detail/project-edit-form.tsx"
    ).read_text(encoding="utf-8")
    assert "ProjectConnectionsEditor" in edit_form
    assert "ProjectDatasetsEditor" in edit_form
    # Notifications is a read-only overview; there is no subscriptions page.
    assert "ProjectNotificationOverview" in edit_form
    assert "ProjectNotificationRoutesEditor" not in edit_form
    assert "notification-subscriptions" not in router
    assert "ProjectPolicyEditor" in edit_form
    assert "fetchWorkflows" in edit
    assert "workflowCatalogQuery" in edit
    assert "workflowSelectorRows" in edit
    assert "workflowFilterItems" in edit_form
    # Keep the selector placeholder localized without losing its English fallback.
    assert (
        'searchPlaceholder={t("react.projects.workflow_name_or_id", "Workflow name or ID")}'
        in edit_form
    )
    assert '"notifications"' in edit_form
    assert '"connections"' in edit_form
    assert '"datasets"' in edit
    assert '"history"' in edit
    assert "ProjectHistoryPanel" in edit_form


def test_workflow_projects_tab_owns_reverse_project_bindings() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    detail = (repo_root / "assets/frontend/src/pages/workflows/detail/index.tsx").read_text(
        encoding="utf-8"
    )
    projects = (repo_root / "assets/frontend/src/pages/workflows/projects/index.tsx").read_text(
        encoding="utf-8"
    )
    router = (repo_root / "assets/frontend/src/app/router.tsx").read_text(encoding="utf-8")

    assert "WorkflowProjectBindingsPanel" not in detail
    assert "fetchProjectsList" not in detail
    assert "WorkflowProjectBindingsPanel" in projects
    assert "fetchProjectsList" in projects
    assert "fetchProjectWorkflowBindings" in projects
    assert "saveProjectWorkflowBindings" in projects
    assert "selectorRows" in projects
    assert "projectFilterItems" in projects
    assert (
        'searchPlaceholder={t("react.workflows.project_name_or_id", "Project name or ID")}'
        in projects
    )
    assert "to={`/projects/${row.id}?tab=workflows`}" in projects
    assert 'path=":workflowId/projects" element={<WorkflowsProjectsPage />}' in router


def test_project_overview_and_history_use_inspectable_surfaces() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    detail = (repo_root / "assets/frontend/src/pages/projects/detail/index.tsx").read_text(
        encoding="utf-8"
    )
    assert "+ Scheduled Job" not in detail
    assert "Project overview" not in detail

    history = (repo_root / "assets/frontend/src/pages/projects/history/index.tsx").read_text(
        encoding="utf-8"
    )
    # The revision diff is a published DsDialog now, and that component owns the
    # role attribute, so the marker follows the component rather than the markup.
    assert "<DsDialog" in history
    assert "Revision diff" in history
    assert "sticky top-0" in history
