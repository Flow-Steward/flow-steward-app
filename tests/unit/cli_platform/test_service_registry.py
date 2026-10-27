from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from core.application.auth.user_admin.contracts import (
    UserActiveUpdateInput,
    UserCreateInput,
    UserPasswordUpdateInput,
    UserRolesUpdateInput,
    UserSelectorInput,
)
from core.application.extensions.scaffold import ExtensionScaffoldCreateInput
from core.application.management.command_specs.extensions import (
    ExtensionShowCommandInput,
    ExtensionsListCommandInput,
    ExtensionToggleCommandInput,
    ExtensionUninstallCommandInput,
)
from core.application.management.command_specs.projects import (
    ProjectDispatchNotificationCommandInput,
    ProjectListCommandInput,
    ProjectNotificationDeliveriesCommandInput,
    ProjectNotificationRoutesCommandInput,
    ProjectShowCommandInput,
)
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.application.management.command_specs.users import UsersListCommandInput
from core.application.management.command_specs.workflows import (
    WorkflowEntrypointsCommandInput,
    WorkflowListCommandInput,
    WorkflowTargetCommandInput,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.cli.platform.service_registry import (
    CHOICE_PROVIDERS,
    SERVICE_FACTORIES,
    ServiceRegistry,
)


@dataclass
class _Runtime:
    user_query_service: object
    user_command_service: object
    project_query_service: object
    project_command_service: object
    workflow_operator_service: object
    extension_query_service: object
    extension_command_service: object
    extension_binding_service: object
    extension_scaffold_service: object


class _UserQuery:
    def list_users(self, **kwargs):
        self.kwargs = kwargs
        return {"users": []}

    async def list_accounts(self, *, limit: int):
        self.account_limit = limit
        return [
            {"id": "active", "display_name": "Active", "is_active": True},
            {"id": "inactive", "display_name": "Inactive", "is_active": False},
        ]


class _UserCommand:
    def create_user(self, command: UserCreateInput, **kwargs):
        self.command = command
        self.create_kwargs = kwargs
        return {"status": "created"}

    def set_password(self, command: UserPasswordUpdateInput, **kwargs):
        self.password_command = command
        self.password_kwargs = kwargs
        return {"status": "password_updated"}

    def force_password_reset(self, command: UserSelectorInput, **kwargs):
        self.force_reset_command = command
        self.force_reset_kwargs = kwargs
        return {"status": "password_reset_required"}

    def unlock_user(self, command: UserSelectorInput, **kwargs):
        self.unlock_command = command
        self.unlock_kwargs = kwargs
        return {"status": "unlocked"}

    def set_active(self, command: UserActiveUpdateInput, **kwargs):
        self.active_command = command
        self.active_kwargs = kwargs
        return {"status": "updated"}

    def set_roles(self, command: UserRolesUpdateInput, **kwargs):
        self.roles_command = command
        self.roles_kwargs = kwargs
        return {"status": "updated"}


class _ProjectQuery:
    async def list_projects(self, *, per_page: int, **kwargs):
        self.per_page = per_page
        self.list_projects_kwargs = kwargs
        return {"projects": [{"id": "p1", "name": "Project One"}]}

    def get_project_detail(self, project_id: str, **kwargs):
        self.detail_project_id = project_id
        self.detail_kwargs = kwargs
        return {"project": {"id": project_id, "name": "Project One"}}

    def get_runtime_config(self, project_id: str, **kwargs):
        self.runtime_config_project_id = project_id
        self.runtime_config_kwargs = kwargs
        return {"project_id": project_id, "run_config": {"default_branch": "main"}}

    def list_notification_routes(self, project_id: str, **kwargs):
        self.routes_call = (project_id, kwargs)
        return {"project_id": project_id, "notification_routes": [{"id": "route_1"}]}

    def list_notification_deliveries(self, project_id: str, **kwargs):
        self.deliveries_call = (project_id, kwargs)
        return {"project_id": project_id, "notification_deliveries": [{"id": "delivery_1"}]}


class _ProjectCommand:
    def dispatch_project_notification(self, project_id: str, command, **kwargs):
        self.dispatch_call = (project_id, command, kwargs)
        return {"project_id": project_id, "notification_deliveries": [{"id": "delivery_1"}]}


class _WorkflowOperator:
    def list_project_workflows(self, project_id: str, **kwargs):
        self.list_call = (project_id, kwargs)
        return {"project_id": project_id, "workflows": [{"workflow_id": "wf.release"}]}

    def list_project_entrypoints(self, project_id: str, **kwargs):
        self.entrypoints_call = (project_id, kwargs)
        return {"project_id": project_id, "entrypoints": [{"entry_key": "release"}]}

    def project_workflow_validate(self, project_id: str, workflow_id: str, **kwargs):
        self.validate_call = (project_id, workflow_id, kwargs)
        return {"workflow_id": workflow_id, "validation": {"valid": True}, "issues": []}

    def project_workflow_contract(self, project_id: str, workflow_id: str, **kwargs):
        self.contract_call = (project_id, workflow_id, kwargs)
        return {"workflow_id": workflow_id, "interface": {"input": {}}}

    def project_workflow_suggestions(self, project_id: str, workflow_id: str, **kwargs):
        self.suggestions_call = (project_id, workflow_id, kwargs)
        return {"workflow_id": workflow_id, "suggestions": []}


class _ExtensionScaffold:
    def create(self, command: ExtensionScaffoldCreateInput):
        self.command = command
        return {"status": "created"}


class _ExtensionQuery:
    def list_extensions(self, *, filters: dict[str, Any], scope: ScopeContext):
        state = filters["state"][0] if filters["state"] else "all"
        self.filters = filters
        self.list_scope = scope
        return {"extensions": [{"extension_id": "acme.crm-sync", "state": state}]}

    def get_extension(self, extension_id: str, *, scope: ScopeContext):
        self.extension_id = extension_id
        self.show_scope = scope
        return {"extension_id": extension_id, "extension_name": "CRM Sync"}

    def get_health(self, extension_id: str, *, scope: ScopeContext):
        self.health_extension_id = extension_id
        self.health_scope = scope
        return {
            "extension_id": extension_id,
            "health": {"status": "healthy", "reason": "Runtime probe passed."},
        }


class _ExtensionBinding:
    def install_extension_archive_path(self, *, archive_path: Path, scope: ScopeContext):
        self.install_call = (archive_path, scope)
        return {
            "extension": {"extension_id": "acme.crm-sync"},
            "installed_path": str(archive_path),
        }

    def preview_extension_uninstall(self, extension_id: str, *, scope: ScopeContext):
        self.preview_uninstall_call = (extension_id, scope)
        return {
            "extension_id": extension_id,
            "install_id": "install-acme",
            "release_id": "release-acme",
            "revision": "rev-1",
            "remove": [],
            "affected": [],
            "retained": [],
        }

    def uninstall_extension(self, extension_id: str, *, plan_revision: str, scope: ScopeContext):
        self.uninstall_call = (extension_id, plan_revision, scope)
        return {"extension_id": extension_id, "state": "uninstalled", "removed_data": []}

    def set_enabled_state(
        self,
        extension_id: str,
        *,
        version: str | None,
        enabled: bool,
        config,
        scope: ScopeContext,
        approved_browser_authority_impact_hash: str = "",
    ):
        self.toggle_call = (
            extension_id,
            version,
            enabled,
            config,
            scope,
            approved_browser_authority_impact_hash,
        )
        return {
            "extension": {
                "extension_id": extension_id,
                "state": "active" if enabled else "disabled",
            }
        }


class _ExtensionCommand:
    def enable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext,
        approved_browser_authority_impact_hash: str = "",
    ):
        self.enable_call = (
            extension_id,
            scope,
            approved_browser_authority_impact_hash,
        )
        return {"extension_id": extension_id, "state": "active"}


def _registry() -> ServiceRegistry:
    return ServiceRegistry(
        _Runtime(
            _UserQuery(),
            _UserCommand(),
            _ProjectQuery(),
            _ProjectCommand(),
            _WorkflowOperator(),
            _ExtensionQuery(),
            _ExtensionCommand(),
            _ExtensionBinding(),
            _ExtensionScaffold(),
        )
    )


def _assert_cli_scope(value: object, *, project_id: str | None = None) -> ScopeContext:
    assert isinstance(value, ScopeContext)
    assert value.actor_id == "flow-steward-cli"
    assert value.principal_type == "system_actor"
    assert value.project_id == project_id
    if project_id:
        assert value.allowed_project_ids == frozenset({project_id})
    return value


def _assert_cli_admin_scope(value: object) -> ScopeContext:
    assert isinstance(value, ScopeContext)
    assert value.actor_id == "flow-steward-cli"
    assert value.principal_type == "admin"
    assert value.project_id is None
    assert value.allowed_project_ids == frozenset()
    return value


def test_registry_resolves_only_explicit_first_party_keys() -> None:
    registry = _registry()

    assert set(SERVICE_FACTORIES) == {spec.service_ref for spec in PUBLIC_COMMAND_SPECS}
    assert set(CHOICE_PROVIDERS) == {
        "accounts.available",
        "projects.available",
        "stack.log-services",
    }
    assert all(not isinstance(entry, str) for entry in SERVICE_FACTORIES.values())
    with pytest.raises(KeyError):
        registry.service("unknown.operation")
    with pytest.raises(KeyError):
        registry.choice_provider("unknown.available")


def test_lifecycle_registry_fails_closed_before_product_binding_import() -> None:
    registry = ServiceRegistry(object(), allowed_domains=frozenset({"stack", "readiness"}))

    with (
        patch("core.entrypoints.cli.platform.service_registry.import_module") as import_module,
        pytest.raises(KeyError, match="not available"),
    ):
        registry.service("users.list")

    import_module.assert_not_called()


def test_cli_service_boundary_has_no_dynamic_signature_compatibility() -> None:
    root = Path(__file__).resolve().parents[3]
    common = (root / "core/entrypoints/cli/platform/service_bindings/common.py").read_text()
    projects = (root / "core/entrypoints/cli/platform/service_bindings/projects.py").read_text()
    execution = (root / "core/entrypoints/cli/platform/execution.py").read_text()
    click_app = (root / "core/entrypoints/cli/click_app.py").read_text()
    assert "inspect.signature" not in common + projects + execution
    assert (
        "**kwargs"
        not in execution.partition("class ServiceInvoker")[2].partition("def execute_command")[0]
    )
    assert "cast(RuntimeServices" not in click_app
    assert (
        ": Any" not in common.partition("class RuntimeServices")[2].partition("ServiceFactory")[0]
    )


def test_runtime_services_protocol_members_are_read_only_properties() -> None:
    from core.entrypoints.cli.platform.service_bindings.common import RuntimeServices

    members = (
        "user_query_service",
        "user_command_service",
        "project_query_service",
        "project_command_service",
        "job_query_service",
        "job_command_service",
        "approval_command_service",
        "reporting_service",
        "ops_health_snapshot_service",
        "workflow_operator_service",
        "extension_query_service",
        "extension_binding_service",
        "extension_scaffold_service",
        "database_tooling_service",
        "testing_tooling_service",
        "quality_tooling_service",
        "localization_tooling_service",
    )
    assert all(isinstance(RuntimeServices.__dict__.get(name), property) for name in members)


def test_registry_adapts_validated_models_and_async_service_results() -> None:
    registry = _registry()
    users_list_input = UsersListCommandInput(
        search="Ada", include_inactive=True, include_deleted=True
    )
    user_input = UserCreateInput(email="operator@example.com", password="safe", roles=["viewer"])

    assert asyncio.run(registry.invoke("users.list", users_list_input)) == {"users": []}
    assert registry.runtime.user_query_service.kwargs == {
        "search": "Ada",
        "include_inactive": True,
        "include_deleted": True,
        "limit": 50,
        "offset": 0,
    }
    assert asyncio.run(registry.invoke("users.create", user_input)) == {"status": "created"}
    assert asyncio.run(registry.invoke("projects.list", ProjectListCommandInput())) == {
        "projects": [{"id": "p1", "name": "Project One"}]
    }
    list_scope = registry.runtime.project_query_service.list_projects_kwargs.pop("scope")
    assert registry.runtime.project_query_service.list_projects_kwargs == {
        "page": 1,
        "search": "",
    }
    _assert_cli_scope(list_scope)
    assert asyncio.run(
        registry.invoke("projects.show", ProjectShowCommandInput(project_id="p1"))
    ) == {
        "project": {
            "id": "p1",
            "name": "Project One",
            "assigned_data_connections": [],
            "available_data_connection_count": 0,
        },
        "connection_schema_page": {
            "included": False,
            "items": [],
            "page": 1,
            "per_page": 25,
            "total_items": 0,
            "total_pages": 1,
        },
    }
    detail_scope = registry.runtime.project_query_service.detail_kwargs.pop("scope")
    assert registry.runtime.project_query_service.detail_project_id == "p1"
    _assert_cli_scope(detail_scope, project_id="p1")
    assert registry.runtime.user_command_service.command is user_input


def test_registry_delegates_phase2_project_and_workflow_operations() -> None:
    registry = _registry()

    assert asyncio.run(
        registry.invoke(
            "projects.notification-routes",
            ProjectNotificationRoutesCommandInput(project_id="p1", enabled_only=True),
        )
    ) == {"project_id": "p1", "notification_routes": [{"id": "route_1"}]}
    assert asyncio.run(
        registry.invoke(
            "projects.notification-deliveries",
            ProjectNotificationDeliveriesCommandInput(project_id="p1", job_id="job-1", limit=5),
        )
    ) == {"project_id": "p1", "notification_deliveries": [{"id": "delivery_1"}]}
    assert asyncio.run(
        registry.invoke(
            "projects.dispatch-notification",
            ProjectDispatchNotificationCommandInput(
                project_id="p1",
                event_type="workflow.completed",
                workflow_id="wf.release",
                payload={"ok": True},
            ),
        )
    )["notification_deliveries"] == [{"id": "delivery_1"}]
    assert registry.runtime.project_command_service.dispatch_call[1].payload == {"ok": True}

    assert asyncio.run(
        registry.invoke(
            "workflows.list",
            WorkflowListCommandInput(project_id="p1", include_disabled=True),
        )
    ) == {"project_id": "p1", "workflows": [{"workflow_id": "wf.release"}]}
    workflow_project_id, workflow_list_kwargs = registry.runtime.workflow_operator_service.list_call
    workflow_list_scope = workflow_list_kwargs.pop("scope")
    assert (workflow_project_id, workflow_list_kwargs) == (
        "p1",
        {"enabled_only": False, "include_validation": False},
    )
    _assert_cli_scope(workflow_list_scope, project_id="p1")
    assert asyncio.run(
        registry.invoke(
            "workflows.entrypoints",
            WorkflowEntrypointsCommandInput(project_id="p1", include_disabled=True),
        )
    ) == {"project_id": "p1", "entrypoints": [{"entry_key": "release"}]}
    assert asyncio.run(
        registry.invoke(
            "workflows.validate",
            WorkflowTargetCommandInput(project_id="p1", workflow_id="wf.release"),
        )
    )["validation"] == {"valid": True}
    assert asyncio.run(
        registry.invoke(
            "workflows.contract",
            WorkflowTargetCommandInput(project_id="p1", workflow_id="wf.release"),
        )
    )["interface"] == {"input": {}}
    assert (
        asyncio.run(
            registry.invoke(
                "workflows.suggestions",
                WorkflowTargetCommandInput(project_id="p1", workflow_id="wf.release"),
            )
        )["suggestions"]
        == []
    )


def test_registry_delegates_phase2_user_and_extension_operations(tmp_path: Path) -> None:
    registry = _registry()
    archive = tmp_path / "extension.zip"
    archive.write_bytes(b"zip-bytes")

    assert asyncio.run(
        registry.invoke(
            "users.set-password",
            UserPasswordUpdateInput(email="operator@example.com", password="StrongPass_123!"),
        )
    ) == {"status": "password_updated"}
    assert asyncio.run(
        registry.invoke("users.force-reset", UserSelectorInput(email="operator@example.com"))
    ) == {"status": "password_reset_required"}
    assert asyncio.run(
        registry.invoke("users.unlock", UserSelectorInput(email="operator@example.com"))
    ) == {"status": "unlocked"}
    assert asyncio.run(
        registry.invoke(
            "users.set-active",
            UserActiveUpdateInput(email="operator@example.com", is_active=False),
        )
    ) == {"status": "updated"}
    assert asyncio.run(
        registry.invoke(
            "users.set-roles",
            UserRolesUpdateInput(email="operator@example.com", roles=["operator"]),
        )
    ) == {"status": "updated"}

    assert asyncio.run(
        registry.invoke("extensions.list", ExtensionsListCommandInput(state="disabled"))
    ) == {"extensions": [{"extension_id": "acme.crm-sync", "state": "disabled"}]}
    _assert_cli_admin_scope(registry.runtime.extension_query_service.list_scope)
    assert asyncio.run(
        registry.invoke("extensions.show", ExtensionShowCommandInput(extension_id="acme.crm-sync"))
    ) == {"extension_id": "acme.crm-sync", "extension_name": "CRM Sync"}
    _assert_cli_admin_scope(registry.runtime.extension_query_service.show_scope)
    assert asyncio.run(
        registry.invoke(
            "extensions.health", ExtensionShowCommandInput(extension_id="acme.crm-sync")
        )
    ) == {
        "extension_id": "acme.crm-sync",
        "health": {"status": "healthy", "reason": "Runtime probe passed."},
    }
    assert registry.runtime.extension_query_service.health_extension_id == "acme.crm-sync"
    _assert_cli_admin_scope(registry.runtime.extension_query_service.health_scope)
    with patch.object(
        Path, "read_bytes", side_effect=AssertionError("CLI must not read archive bytes")
    ):
        assert asyncio.run(
            registry.invoke(
                "extensions.install",
                {"archive": archive},
            )
        )["installed_path"] == str(archive)
    assert registry.runtime.extension_binding_service.install_call[0] == archive
    _assert_cli_admin_scope(registry.runtime.extension_binding_service.install_call[1])
    assert asyncio.run(
        registry.invoke(
            "extensions.uninstall",
            ExtensionUninstallCommandInput(extension_id="acme.crm-sync", plan_revision="rev-1"),
        )
    ) == {"extension_id": "acme.crm-sync", "state": "uninstalled", "removed_data": []}
    assert registry.runtime.extension_binding_service.uninstall_call[:2] == (
        "acme.crm-sync",
        "rev-1",
    )
    _assert_cli_admin_scope(registry.runtime.extension_binding_service.uninstall_call[2])
    assert (
        asyncio.run(
            registry.invoke(
                "extensions.enable", ExtensionToggleCommandInput(extension_id="acme.crm-sync")
            )
        )["extension"]["state"]
        == "active"
    )
    assert registry.runtime.extension_binding_service.toggle_call[:4] == (
        "acme.crm-sync",
        None,
        True,
        {},
    )
    _assert_cli_admin_scope(registry.runtime.extension_binding_service.toggle_call[4])
    assert registry.runtime.extension_binding_service.toggle_call[5] == ""
    assert (
        asyncio.run(
            registry.invoke(
                "extensions.disable", ExtensionToggleCommandInput(extension_id="acme.crm-sync")
            )
        )["extension"]["state"]
        == "disabled"
    )
    assert registry.runtime.extension_binding_service.toggle_call[:4] == (
        "acme.crm-sync",
        None,
        False,
        {},
    )
    _assert_cli_admin_scope(registry.runtime.extension_binding_service.toggle_call[4])


def test_choice_providers_are_bounded_and_filter_active_accounts() -> None:
    registry = _registry()

    accounts = asyncio.run(registry.choices("accounts.available"))
    projects = asyncio.run(registry.choices("projects.available"))

    assert [(item.value, item.label) for item in accounts] == [("active", "Active")]
    assert [(item.value, item.label) for item in projects] == [("p1", "Project One")]
    assert registry.runtime.user_query_service.account_limit == 200
    assert registry.runtime.project_query_service.per_page <= 200
