"""The connector catalog must read from stores that can answer its questions.

The catalog resolves project connections through two paths — the admin db
handle, then a store built from its DSN. The admin handle is a RegistryDBStore,
which has no list_project_connections, and the fallback used to build a
PlatformIdentityStore, which has none either. Both lookups missed, and the
catalog returned an empty connection list rather than an error, so a connector
step could never be bound to a project connection on any deployment and the
authoring surface simply showed none.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.entrypoints.admin import (
    workflows_common,
    workflows_routes_history,
    workflows_routes_read,
    workflows_scope,
)
from core.entrypoints.admin.workflows_connector_catalog import (
    _list_project_connections_for_catalog,
)
from core.infrastructure.db.platform_stores import (
    PlatformIdentityStore,
    PlatformIntegrationStore,
    PlatformJobStore,
    PlatformProjectStore,
)
from core.infrastructure.runtime import entrypoint_db_adapters


class _AdminDbHandle:
    """Shaped like the admin RegistryDBStore: carries a dsn, answers nothing else."""

    dsn = "postgresql://example/flow_steward"


def test_workflow_import_transaction_binds_both_stores_to_one_connection() -> None:
    events = []
    connection_surface = object()
    transaction_conn = object()

    class _RegistryStore:
        @contextmanager
        def participate_in_external_transaction(self, connection):
            events.append(("registry_enter", connection))
            yield
            events.append(("registry_exit", connection))

    class _ConnectionStore:
        conn = connection_surface

    @contextmanager
    def transaction(connection):
        events.append(("transaction_enter", connection))
        yield transaction_conn
        events.append(("transaction_exit", connection))

    with (
        patch.object(entrypoint_db_adapters, "transaction_connection", transaction),
        entrypoint_db_adapters.workflow_import_transaction(
            _RegistryStore(), _ConnectionStore()
        ) as yielded,
    ):
        assert yielded is transaction_conn

    assert events == [
        ("transaction_enter", connection_surface),
        ("registry_enter", transaction_conn),
        ("registry_exit", transaction_conn),
        ("transaction_exit", connection_surface),
    ]


def test_admin_portability_service_wires_atomic_import_transaction() -> None:
    registry_store = _AdminDbHandle()
    connection_store = object()

    @contextmanager
    def transaction():
        yield "shared-transaction"

    with (
        patch.object(
            workflows_scope,
            "_platform_logger_for_admin_catalog",
            return_value=connection_store,
        ),
        patch.object(
            workflows_scope,
            "workflow_import_transaction",
            return_value=transaction(),
        ) as factory,
    ):
        service = workflows_scope._get_workflow_portability_service(registry_store)
        with service._import_transaction() as yielded:
            assert yielded == "shared-transaction"

    assert service.project_connection_store is connection_store
    factory.assert_called_once_with(registry_store, connection_store)


def test_admin_portability_service_reads_the_project_model_from_the_project_store() -> None:
    """The import checks the project's model through a store that owns the lookup.

    The integration store passed as project_connection_store has no
    get_project_run_config, so reading the model through it left every imported
    agent disabled with "no configured model" even when the project had one.
    """
    project_store = object()
    with (
        patch.object(workflows_scope, "_platform_logger_for_admin_catalog", return_value=None),
        patch.object(
            workflows_common, "create_project_store", return_value=project_store
        ) as factory,
    ):
        service = workflows_scope._get_workflow_portability_service(_AdminDbHandle())

    factory.assert_called_once_with(_AdminDbHandle.dsn)
    assert service.project_store is project_store


def test_integration_store_cannot_answer_the_project_model() -> None:
    assert not hasattr(PlatformIntegrationStore, "get_project_run_config")
    assert hasattr(PlatformProjectStore, "get_project_run_config")


def test_catalog_fallback_store_can_list_project_connections() -> None:
    """The resolver must build a store that actually has the method."""
    with patch.object(
        workflows_common, "create_integration_store", return_value=PlatformIntegrationStore
    ) as factory:
        store = workflows_common._platform_logger_for_admin_catalog(_AdminDbHandle())

    factory.assert_called_once_with(_AdminDbHandle.dsn)
    assert hasattr(store, "list_project_connections")


def test_identity_store_could_not_have_answered() -> None:
    """Documents the defect: the previous fallback simply lacks the method.

    Kept as an explicit assertion so a future change back to the identity store
    fails here with the reason rather than silently emptying the catalog again.
    """
    assert not hasattr(PlatformIdentityStore, "list_project_connections")
    assert hasattr(PlatformIntegrationStore, "list_project_connections")


def test_catalog_reads_connections_through_the_fallback_store() -> None:
    """End to end for the lookup: admin handle misses, fallback answers."""

    class _FallbackStore:
        @staticmethod
        def list_project_connections(project_id, enabled_only=False):
            assert project_id == "proj-1"
            assert enabled_only is True
            return [{"connection_id": "conn_1", "extension_id": "acme.file-transfer"}]

    with (
        patch.object(
            workflows_common,
            "_platform_logger_for_admin_catalog",
            return_value=_FallbackStore,
        ),
        patch(
            "core.entrypoints.admin.workflows_connector_catalog._platform_logger_for_admin_catalog",
            return_value=_FallbackStore,
        ),
    ):
        rows = _list_project_connections_for_catalog(_AdminDbHandle(), project_id="proj-1")

    assert [row["connection_id"] for row in rows] == ["conn_1"]


def test_catalog_returns_nothing_when_no_store_can_answer() -> None:
    """Still degrades quietly when there is genuinely no store — but only then."""

    class _StoreWithoutConnections:
        pass

    with patch(
        "core.entrypoints.admin.workflows_connector_catalog._platform_logger_for_admin_catalog",
        return_value=_StoreWithoutConnections,
    ):
        rows = _list_project_connections_for_catalog(_AdminDbHandle(), project_id="proj-1")

    assert rows == []


def test_workflow_catalog_groups_projected_mcp_tools_under_one_source() -> None:
    db = MagicMock()
    db.list_tools.return_value = [
        {
            "tool_id": "drive.create_file",
            "account_id": DEFAULT_ACCOUNT_ID,
            "display_name": "Drive / create_file",
            "description": "Create a file.",
            "enabled": True,
            "category": "other",
            "interface": {
                "functions": [
                    {
                        "name": "drive.create_file",
                        "display_name": "create_file",
                        "parameters": [],
                        "returns": {"name": "result", "type": "json"},
                    }
                ]
            },
            "implementation": {
                "type": "mcp",
                "connector_id": "mcp_drive",
                "remote_tool_name": "create_file",
            },
        },
        {
            "tool_id": "drive.search_files",
            "account_id": DEFAULT_ACCOUNT_ID,
            "display_name": "Drive / search_files",
            "description": "Search files.",
            "enabled": True,
            "category": "other",
            "interface": {
                "functions": [
                    {
                        "name": "drive.search_files",
                        "display_name": "search_files",
                        "parameters": [],
                        "returns": {"name": "result", "type": "json"},
                    }
                ]
            },
            "implementation": {
                "type": "mcp",
                "connector_id": "mcp_drive",
                "remote_tool_name": "search_files",
            },
        },
    ]
    ready_tools = {row["tool_id"]: row for row in db.list_tools.return_value}
    db.get_tool.side_effect = ready_tools.get

    with (
        patch.object(
            workflows_routes_read,
            "_request_scope",
            return_value={"account_id": DEFAULT_ACCOUNT_ID},
        ),
        patch.object(workflows_routes_read, "_get_db", return_value=db),
        patch.object(
            workflows_routes_read,
            "_list_connector_catalog_rows",
            return_value=[
                {
                    "connector_id": "mcp_drive",
                    "catalog_item_id": "drive",
                    "display_name": "Drive",
                    "description": "Cloud file MCP connector",
                    "namespace_prefix": "drive",
                    "allowlisted_tools": ["drive.create_file", "drive.search_files"],
                    "enabled": True,
                    "status": "active",
                }
            ],
        ),
        patch.object(
            workflows_routes_read,
            "_enforce_project_scope",
            return_value="project-1",
        ),
        patch.object(
            workflows_routes_read,
            "_list_project_connections_for_catalog",
            return_value=[],
        ),
        patch.object(
            workflows_routes_read,
            "_extension_action_connector_rows",
            return_value=[],
        ),
    ):
        payload = workflows_routes_read.list_connector_catalog(object(), "project-1")

    assert payload["count"] == 1
    connector = payload["connectors"][0]
    assert connector["connector_id"] == "mcp_drive"
    assert connector["catalog_item_id"] == "drive"
    assert connector["display_name"] == "Drive"
    assert [operation["display_name"] for operation in connector["operations"]] == [
        "create_file",
        "search_files",
    ]
    assert [operation["runtime_connector_id"] for operation in connector["operations"]] == [
        "connector.drive.create_file",
        "connector.drive.search_files",
    ]


def test_workflow_catalog_queries_mcp_connectors_in_selected_project_scope() -> None:
    request_scope = MagicMock(project_id=None)
    scoped_request = MagicMock(project_id="project-1")
    db = MagicMock()
    db.list_tools.return_value = []

    with (
        patch.object(workflows_routes_read, "_request_scope", return_value=request_scope),
        patch.object(workflows_routes_read, "_get_db", return_value=db),
        patch.object(
            workflows_routes_read,
            "_enforce_project_scope",
            return_value="project-1",
        ),
        patch.object(
            workflows_routes_read,
            "normalize_scope",
            return_value=scoped_request,
        ) as normalize_scope,
        patch.object(
            workflows_routes_read,
            "_list_connector_catalog_rows",
            return_value=[],
        ) as list_connectors,
        patch.object(
            workflows_routes_read,
            "_list_project_connections_for_catalog",
            return_value=[],
        ),
        patch.object(
            workflows_routes_read,
            "_extension_action_connector_rows",
            return_value=[],
        ),
    ):
        workflows_routes_read.list_connector_catalog(object(), "project-1")

    normalize_scope.assert_called_once_with(request_scope, project_id="project-1")
    list_connectors.assert_called_once_with(db, scope=scoped_request)


def test_import_scope_checks_connections_through_the_owning_store() -> None:
    class _ConnectionStore:
        @staticmethod
        def get_project_connection(*, project_id, connection_id):
            return {
                "project_id": project_id,
                "connection_id": connection_id,
            }

    with patch.object(
        workflows_scope,
        "_platform_logger_for_admin_catalog",
        return_value=_ConnectionStore,
    ):
        resolved = workflows_scope._enforce_project_connection_scope(
            _AdminDbHandle(),
            project_id="proj-1",
            connection_id="conn-1",
        )

    assert resolved == "conn-1"


def test_workflow_stats_resolver_builds_the_job_store() -> None:
    """Workflow statistics must use the store that owns job aggregations."""
    with patch.object(
        workflows_common, "create_job_store", return_value=PlatformJobStore
    ) as factory:
        store = workflows_common._platform_job_store_for_workflow_stats(_AdminDbHandle())

    factory.assert_called_once_with(_AdminDbHandle.dsn)
    assert hasattr(store, "get_workflow_stats")
    assert not hasattr(PlatformIntegrationStore, "get_workflow_stats")


def test_workflow_stats_route_uses_the_job_stats_store() -> None:
    """The protected workflow detail route must not reuse the integration store."""

    class _StatsStore:
        @staticmethod
        def get_workflow_stats(*, workflow_id, account_id):
            return {
                "workflow_id": workflow_id,
                "account_id": account_id,
                "total_runs": 3,
            }

    class _Scope:
        account_id = "00000000-0000-0000-0000-000000000001"

    with (
        patch.object(workflows_routes_history, "_get_db", return_value=_AdminDbHandle()),
        patch.object(
            workflows_routes_history,
            "_resolve_workflow_scope_target",
            return_value=({"workflow_id": "weekly_digest"}, _Scope()),
        ),
        patch.object(
            workflows_routes_history,
            "_platform_job_store_for_workflow_stats",
            return_value=_StatsStore(),
        ),
    ):
        result = workflows_routes_history.get_workflow_stats("weekly_digest", object())

    assert result == {
        "workflow_id": "weekly_digest",
        "account_id": _Scope.account_id,
        "total_runs": 3,
    }


def test_workflow_stats_route_fails_closed_when_job_store_is_unavailable() -> None:
    """Missing required stats persistence must not masquerade as zero activity."""

    class _Scope:
        account_id = "00000000-0000-0000-0000-000000000001"

    with (
        patch.object(workflows_routes_history, "_get_db", return_value=_AdminDbHandle()),
        patch.object(
            workflows_routes_history,
            "_resolve_workflow_scope_target",
            return_value=({"workflow_id": "weekly_digest"}, _Scope()),
        ),
        patch.object(
            workflows_routes_history,
            "_platform_job_store_for_workflow_stats",
            return_value=None,
        ),
        pytest.raises(workflows_routes_history.HTTPException) as exc_info,
    ):
        workflows_routes_history.get_workflow_stats("weekly_digest", object())

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Workflow statistics store is unavailable"
