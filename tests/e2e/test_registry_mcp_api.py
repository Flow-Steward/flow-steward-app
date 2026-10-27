from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from core.application.management.command_models import (
    McpConnectorCreateInput,
    McpConnectorUpdateInput,
)
from core.application.management.mcp_connector_services import (
    McpConnectorCommandService,
    McpConnectorStore,
)
from core.application.management.scope import ScopeContext
from core.application.orchestration.connector_step_runtime import execute_connector_step


class _InMemoryMcpConnectorStore:
    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}

    def insert(self, row: dict[str, Any]) -> None:
        self.rows[row["connector_id"]] = dict(row)

    def get(
        self,
        connector_id: str,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        row = self.rows.get(connector_id)
        if row is None:
            return None
        if account_id and row.get("account_id") != account_id:
            return None
        if project_id and row.get("project_id") != project_id:
            return None
        if tenant_id and row.get("tenant_id") != tenant_id:
            return None
        return dict(row)

    def update(
        self,
        connector_id: str,
        updates: dict[str, Any],
        *,
        account_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        row = self.rows.get(connector_id)
        if row is None:
            return
        if account_id and row.get("account_id") != account_id:
            return
        if project_id and row.get("project_id") != project_id:
            return
        if tenant_id and row.get("tenant_id") != tenant_id:
            return
        row.update(dict(updates))
        row["updated_at"] = datetime.now(UTC).isoformat()


class _FakeRegistryDbStore:
    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}

    def get_tool(self, tool_id: str) -> dict[str, Any] | None:
        row = self.rows.get(tool_id)
        return dict(row) if row else None

    def list_tools(
        self, enabled_only: bool = False, include_trashed: bool = False
    ) -> list[dict[str, Any]]:
        _ = include_trashed
        rows = [dict(row) for row in self.rows.values()]
        if enabled_only:
            rows = [row for row in rows if row.get("enabled", True)]
        rows.sort(key=lambda row: str(row.get("tool_id") or ""))
        return rows

    def save_tool(self, data: dict[str, Any], author: str = "system") -> None:
        _ = author
        from core.application.registry.tool_manifest import ToolManifest

        manifest = ToolManifest.model_validate(data)
        row = manifest.to_registry_payload()
        row["tool_id"] = manifest.tool_id
        row["display_name"] = manifest.display_name
        row["enabled"] = manifest.enabled
        self.rows[manifest.tool_id] = row

    def delete_tool(self, tool_id: str, author: str = "system") -> None:
        _ = author
        self.rows.pop(tool_id, None)


class _FakeRegistryManager:
    def __init__(self):
        self.db_store = _FakeRegistryDbStore()
        self._manifests: dict[str, Any] = {}
        self.tools = SimpleNamespace(get_tool=self.get_tool)

    def get_tool(self, tool_id: str):
        return self._manifests.get(tool_id)

    def reload_all(self) -> None:
        from core.application.registry.tool_manifest import ToolManifest

        self._manifests = {
            tool_id: ToolManifest.from_db_row(row) for tool_id, row in self.db_store.rows.items()
        }


def test_registry_mcp_flow_projects_tools_and_invokes_through_runtime():
    store = _InMemoryMcpConnectorStore()
    registry_manager = _FakeRegistryManager()
    bl = MagicMock()
    service = McpConnectorCommandService(get_logger=lambda: bl)
    scope = ScopeContext(
        account_id="account-a",
        tenant_id="tenant-a",
        project_id="project-a",
        allowed_project_ids=frozenset({"project-a"}),
        actor_id="operator-a",
        principal_type="operator",
    )

    @contextmanager
    def _rpc_context(_bl, *, connector_row, timeout_seconds, **_kwargs):
        assert connector_row["account_id"] == "account-a"
        assert connector_row["project_id"] == "project-a"
        assert connector_row["tenant_id"] == "tenant-a"

        def _rpc(payload):
            if payload["method"] == "tools/list":
                return (
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "catalog.read",
                                    "title": "Catalog Read",
                                    "description": "Read catalog rows.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"limit": {"type": "integer"}},
                                        "required": ["limit"],
                                    },
                                }
                            ]
                        },
                    },
                    1,
                )
            assert payload["method"] == "tools/call"
            assert payload["params"]["name"] == "catalog.read"
            assert payload["params"]["arguments"] == {"limit": 5}
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"structuredContent": {"rows": [{"sku": "A1"}]}},
                },
                1,
            )

        yield _rpc

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                McpConnectorCommandService,
                "_insert_connector",
                lambda self, bl, row: store.insert(row),
            )
        )
        stack.enter_context(
            patch.object(
                McpConnectorCommandService,
                "_get_connector_row",
                lambda self, bl, connector_id, account_id=None, project_id=None, tenant_id=None: (
                    store.get(
                        connector_id,
                        account_id=account_id,
                        project_id=project_id,
                        tenant_id=tenant_id,
                    )
                ),
            )
        )
        stack.enter_context(
            patch.object(
                McpConnectorCommandService,
                "_update_connector_row",
                lambda self, bl, connector_id, updates, account_id=None, project_id=None, tenant_id=None: (
                    store.update(
                        connector_id,
                        updates,
                        account_id=account_id,
                        project_id=project_id,
                        tenant_id=tenant_id,
                    )
                ),
            )
        )
        stack.enter_context(
            patch.object(
                McpConnectorStore,
                "get_connector_row",
                lambda self, bl, connector_id, account_id=None, project_id=None, tenant_id=None: (
                    store.get(
                        connector_id,
                        account_id=account_id,
                        project_id=project_id,
                        tenant_id=tenant_id,
                    )
                ),
            )
        )
        stack.enter_context(
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry_manager,
            )
        )
        stack.enter_context(
            patch(
                "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
                side_effect=_rpc_context,
            )
        )
        stack.enter_context(
            patch(
                "core.application.management.mcp_connector_http._resolve_hostname_ips",
                return_value={"93.184.216.34"},
            )
        )

        created = service.create_connector(
            McpConnectorCreateInput(
                display_name="Flow Steward MCP",
                base_url="https://fs.example/mcp",
                namespace_prefix="fs",
            ),
            scope=scope,
        )

        synced = service.sync_connector(created.connector_id, scope=scope)

        assert synced.discovered_tools_jsonb[0]["projected_tool_id"] == "fs.catalog.read"

        updated = service.update_connector(
            created.connector_id,
            McpConnectorUpdateInput(allowlisted_tools=["fs.catalog.read"]),
            scope=scope,
        )

        projected_tool = registry_manager.db_store.get_tool("fs.catalog.read")
        assert projected_tool is not None
        assert projected_tool["enabled"] is True
        assert updated.projected_tools[0]["tool_id"] == "fs.catalog.read"

        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry_manager,
            ),
            patch.dict(
                "os.environ",
                {"DATABASE_URL": "postgres://fs.example/runtime"},
                clear=True,
            ),
            # Runtime RPC functions resolve their context in the extracted owner;
            # discovery above still imports the public context directly.
            patch(
                "core.application.auth.mcp_connector_oauth_rpc.mcp_connector_rpc_context",
                side_effect=_rpc_context,
            ),
            patch(
                "core.application.orchestration.connector_tool_runtime._shared_logger",
                return_value=bl,
            ),
        ):
            result = execute_connector_step(
                step={
                    "step_kind": "connector",
                    "connector_id": "connector.fs.catalog.read",
                    "override": {
                        "connector": {
                            "operation_id": "fs.catalog.read",
                            "input_mapping": {
                                "limit": "literal:5",
                            },
                        }
                    },
                    "output_mapping_jsonb": {
                        "rows": "state.catalog_rows",
                    },
                },
                state={
                    "account_id": "account-a",
                    "tenant_id": "tenant-a",
                    "project_id": "project-a",
                },
            )

    assert result["ok"] is True, result
    assert result["runtime"] == "tool_manifest"
    assert result["tool_id"] == "fs.catalog.read"
    assert result["result"]["rows"] == [{"sku": "A1"}]
    assert result["state_patch"]["catalog_rows"] == [{"sku": "A1"}]
    assert store.rows[created.connector_id]["account_id"] == "account-a"
    assert store.rows[created.connector_id]["project_id"] == "project-a"
    assert store.rows[created.connector_id]["tenant_id"] == "tenant-a"
