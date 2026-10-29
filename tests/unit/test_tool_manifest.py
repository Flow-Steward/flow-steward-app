from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.mark.parametrize(
    "identifier",
    [" ", "fixture invalid/id", "fixture/tool", "fixture\\tool", "fixture\x00tool", ".."],
)
def test_registry_tool_write_rejects_unaddressable_id_before_database_access(identifier):
    from core.application.registry.db_store import RegistryDBStore

    store = RegistryDBStore("postgresql://unused")
    store._column_exists = MagicMock(return_value=False)
    store._exists = MagicMock(return_value=False)
    store._log_change_cur = MagicMock()
    store._trigger_public_contract_projection_sync = MagicMock()
    store._transaction = MagicMock()
    with pytest.raises(ValueError):
        store.save_tool({"tool_id": identifier, "display_name": "Draft", "enabled": False})
    store._column_exists.assert_not_called()
    store._transaction.assert_not_called()


def test_tool_manifest_read_does_not_hide_saved_invalid_identifier():
    from core.application.registry.tool_manifest import ToolManifest

    manifest = ToolManifest.from_db_row(
        {
            "tool_id": "fixture invalid/id",
            "display_name": "Existing rejected-ID fixture",
            "enabled": False,
        }
    )
    assert manifest.tool_id == "fixture invalid/id"


def test_tool_manifest_normalizes_legacy_type_and_derives_contract_schemas():
    from core.application.registry.tool_manifest import ToolManifest

    manifest = ToolManifest.from_db_row(
        {
            "tool_id": "search_tool",
            "version": "1.2.3",
            "display_name": "Search Tool",
            "enabled": True,
            "category": "data",
            "prompt": "Use this tool for semantic search.",
            "interface": {
                "function_name": "search_code",
                "parameters": [
                    {"name": "query", "type": "string", "required": True},
                    {"name": "limit", "type": "integer", "required": False, "default": 10},
                ],
                "returns": {"name": "results", "type": "array", "required": True},
            },
            "implementation": {
                "type": "mcp",
                "server": "docsmcp",
                "connector_id": "docs_mcp_connector",
            },
            "config": {"timeout_seconds": 15},
            "tags": ["search", "code"],
        }
    )

    artifact = manifest.to_dict()

    assert manifest.implementation.kind == "mcp"
    assert manifest.implementation.type == "mcp"
    assert artifact["implementation"]["kind"] == "mcp"
    assert artifact["implementation"]["type"] == "mcp"
    assert artifact["artifact_schema_version"] == "tool_artifact_v1"
    assert artifact["input_schema"]["properties"]["query"]["type"] == "string"
    assert artifact["output_schema"]["properties"]["results"]["type"] == "array"
    assert artifact["operations"][0]["name"] == "search_code"


def test_tool_function_preserves_explicit_artifact_policy_operation():
    from core.application.registry.tool_manifest import ToolManifest

    manifest = ToolManifest.model_validate(
        {
            "tool_id": "extension.install.catalog",
            "display_name": "Catalog",
            "implementation": {"type": "extension", "extension_key": "example.catalog"},
            "interface": {
                "functions": [
                    {
                        "name": "store_catalog",
                        "artifact_policy_operation_id": "invoke_tool",
                    }
                ]
            },
        }
    )

    assert manifest.interface.operations[0]["artifact_policy_operation_id"] == "invoke_tool"


def test_mcp_tool_manifest_requires_connector_id():
    from core.application.registry.tool_manifest import ToolManifest

    with pytest.raises(ValueError, match="connector_id"):
        ToolManifest.from_db_row(
            {
                "tool_id": "search_tool",
                "display_name": "Search Tool",
                "enabled": True,
                "interface": {
                    "function_name": "search_code",
                    "parameters": [{"name": "query", "type": "string", "required": True}],
                },
                "implementation": {"type": "mcp", "server": "docsmcp"},
            }
        )


def test_legacy_mcp_alias_row_backfills_fs_builtin_connector_id():
    from core.application.registry.tool_manifest import ToolManifest

    manifest = ToolManifest.from_db_row(
        {
            "tool_id": "workflow_ops",
            "display_name": "Workflow Operations",
            "enabled": True,
            "interface": {
                "function_name": "workflow_list",
                "parameters": [],
            },
            "implementation": {"type": "mcp", "server": "fs"},
        }
    )

    assert manifest.implementation.connector_id == "builtin_fs_mcp"


def test_registry_db_store_save_tool_persists_canonical_tool_artifact_payload():
    from core.application.registry.db_store import RegistryDBStore

    store = RegistryDBStore("postgresql://unused")
    executed: list[tuple[str, dict]] = []
    cursor = MagicMock()
    cursor.fetchone.return_value = None

    def _execute(sql, params=None):
        executed.append((sql, params))

    cursor.execute.side_effect = _execute

    @contextmanager
    def _transaction():
        yield cursor

    store._transaction = _transaction  # type: ignore[assignment]
    store._exists = MagicMock(return_value=False)
    store._column_exists = MagicMock(return_value=False)
    store._log_change_cur = MagicMock()
    store._trigger_public_contract_projection_sync = MagicMock()

    store.save_tool(
        {
            "tool_id": "context_lookup",
            "display_name": "Context Lookup",
            "description": "Lookup external docs",
            "enabled": True,
            "category": "data",
            "prompt": "Query external knowledge.",
            "interface": {
                "function_name": "lookup_docs",
                "parameters": [{"name": "topic", "type": "string", "required": True}],
                "returns": {"name": "documents", "type": "array", "required": True},
            },
            "implementation": {
                "type": "mcp",
                "server": "docsmcp",
                "connector_id": "docs_mcp_connector",
            },
            "config": {"cost_tier": "medium"},
            "tags": ["docs"],
        },
        author="test",
    )

    params = next(params for sql, params in executed if "INSERT INTO registry_tools" in str(sql))
    implementation = json.loads(params["implementation"])
    interface = json.loads(params["interface"])

    assert implementation["kind"] == "mcp"
    assert implementation["type"] == "mcp"
    assert interface["input_schema"]["properties"]["topic"]["type"] == "string"
    assert interface["output_schema"]["properties"]["documents"]["type"] == "array"

    snapshot = store._log_change_cur.call_args.args[4]
    assert snapshot["artifact_schema_version"] == "tool_artifact_v1"
    assert snapshot["implementation"]["kind"] == "mcp"


def test_registry_db_store_save_tool_preserves_host_availability_metadata():
    from core.application.registry.db_store import RegistryDBStore

    store = RegistryDBStore("postgresql://unused")
    executed: list[tuple[str, dict]] = []
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.execute.side_effect = lambda sql, params=None: executed.append((sql, params))

    @contextmanager
    def _transaction():
        yield cursor

    store._transaction = _transaction  # type: ignore[assignment]
    store._exists = MagicMock(return_value=True)
    store._column_exists = MagicMock(
        side_effect=lambda _table, column: (
            column
            in {
                "availability_status",
                "availability_reason_code",
                "availability_reason_text",
            }
        )
    )
    store._log_change_cur = MagicMock()
    store._trigger_public_contract_projection_sync = MagicMock()

    store.save_tool(
        {
            "tool_id": "orphaned_tool",
            "display_name": "Orphaned Tool",
            "enabled": False,
            "interface": {"function_name": "invoke"},
            "implementation": {"type": "internal"},
            "availability_status": "unavailable",
            "availability_reason_code": "source_connector_removed",
            "availability_reason_text": "Source MCP connector mcp_removed was deleted.",
        },
        author="test",
    )

    params = next(params for sql, params in executed if "INSERT INTO registry_tools" in str(sql))
    assert params["availability_status"] == "unavailable"
    assert params["availability_reason_code"] == "source_connector_removed"
    assert params["availability_reason_text"] == ("Source MCP connector mcp_removed was deleted.")


def test_tool_registry_exposes_typed_catalog_entries():
    from core.application.registry.tool_registry import ToolRegistry

    row = {
        "tool_id": "search_tool",
        "version": "1.0.0",
        "display_name": "Search Tool",
        "description": "Search indexed code.",
        "enabled": True,
        "category": "data",
        "prompt": "Search the codebase.",
        "interface": {
            "function_name": "search_code",
            "parameters": [{"name": "query", "type": "string", "required": True}],
            "returns": {"name": "results", "type": "array", "required": True},
        },
        "implementation": {
            "kind": "internal",
            "module": "core.application.orchestration.tools.search_tools",
        },
        "config": {},
        "tags": ["search"],
    }
    db_store = MagicMock()
    db_store.list_tools.return_value = [row]
    db_store.get_effective_tool_catalog.return_value = [
        {
            "tool_id": "search_tool",
            "implementation_kind": "internal",
            "implementation": {"kind": "internal", "type": "internal"},
            "source": "skill",
            "interface": {"operations": [{"name": "search_code"}]},
        }
    ]

    registry = ToolRegistry(db_store)

    catalog_entry = registry.get_catalog_entry("search_tool")
    effective_catalog = registry.get_effective_catalog("composer")

    assert catalog_entry is not None
    assert catalog_entry["tool_id"] == "search_tool"
    assert catalog_entry["implementation_kind"] == "internal"
    assert catalog_entry["interface"]["operations"][0]["name"] == "search_code"
    assert effective_catalog[0]["source"] == "skill"


def test_sync_builtin_standard_tools_preserves_config_and_forces_connector_only():
    from core.application.registry.builtin_standard_tools import (
        builtin_standard_tool_manifests,
        sync_builtin_standard_tools,
    )

    class _Store:
        def __init__(self):
            self.saved: dict[str, dict] = {}
            self.existing = {
                "std.text": {
                    "tool_id": "std.text",
                    "version": "9.9.9",
                    "display_name": "Custom Text Tools",
                    "description": "old",
                    "enabled": False,
                    "category": "marketing",
                    "prompt": "old prompt",
                    "interface": {},
                    "implementation": {},
                    "config": {"owner": "ops"},
                    "tags": ["custom-tag"],
                },
                "std.json": {
                    "tool_id": "std.json",
                    "version": "1.0.0",
                    "display_name": "JSON",
                    "enabled": True,
                    "category": "standard_tool_action",
                    "config": {},
                    "tags": [],
                },
            }

        def get_tool(self, tool_id: str):
            return self.existing.get(tool_id)

        def save_tool(self, payload: dict, author: str = "system"):
            self.saved[payload["tool_id"]] = {"author": author, **payload}

    store = _Store()
    changed = sync_builtin_standard_tools(store)

    assert set(changed) == set(builtin_standard_tool_manifests())
    saved_text = store.saved["std.text"]
    assert saved_text["version"] == "9.9.9"
    assert saved_text["display_name"] == "Custom Text Tools"
    assert saved_text["enabled"] is False
    assert saved_text["category"] == "marketing"
    assert saved_text["config"] == {
        "owner": "ops",
        "agent_exposure": "connector_only",
    }
    assert saved_text["tags"] == ["custom-tag"]
    assert store.saved["std.json"]["config"] == {"agent_exposure": "connector_only"}
    # A category from before the shared list is replaced by the shipped one.
    assert store.saved["std.json"]["category"] == "data"


def test_sync_builtin_standard_tools_is_idempotent_against_canonical_db_payloads():
    from core.application.registry.builtin_standard_tools import (
        builtin_standard_tool_manifests,
        sync_builtin_standard_tools,
    )
    from core.application.registry.tool_manifest import ToolManifest

    class _Store:
        def __init__(self):
            self.existing: dict[str, dict] = {}
            self.saved: list[dict] = []

        def get_tool(self, tool_id: str):
            return self.existing.get(tool_id)

        def save_tool(self, payload: dict, author: str = "system"):
            self.saved.append({"author": author, **payload})
            self.existing[payload["tool_id"]] = ToolManifest.model_validate(
                payload
            ).to_registry_payload()

    store = _Store()

    assert set(sync_builtin_standard_tools(store)) == set(builtin_standard_tool_manifests())
    store.saved.clear()

    assert sync_builtin_standard_tools(store) == []
    assert store.saved == []


def test_sync_builtin_mcp_tools_is_idempotent_against_canonical_db_payloads():
    from core.application.registry.builtin_mcp_tools import sync_builtin_mcp_tools
    from core.application.registry.tool_manifest import ToolManifest

    class _Store:
        def __init__(self):
            self.existing: dict[str, dict] = {}
            self.saved: list[dict] = []

        def get_tool(self, tool_id: str):
            return self.existing.get(tool_id)

        def save_tool(self, payload: dict, author: str = "system"):
            self.saved.append({"author": author, **payload})
            self.existing[payload["tool_id"]] = ToolManifest.model_validate(
                payload
            ).to_registry_payload()

    store = _Store()

    first_changed = sync_builtin_mcp_tools(store)
    assert set(first_changed) == {"workflow_ops"}
    store.saved.clear()

    assert sync_builtin_mcp_tools(store) == []
    assert store.saved == []


def test_sync_builtin_step_kind_profiles_skips_content_noops():
    from core.application.registry.builder_metadata import normalize_builder_metadata
    from core.application.registry.builtin_step_kind_profiles import (
        sync_builtin_step_kind_profiles,
    )

    class _Store:
        def __init__(self):
            self.existing: dict[str, dict] = {}
            self.saved: list[dict] = []

        def get_step_kind_profile(self, step_kind_key: str):
            return self.existing.get(step_kind_key)

        def upsert_step_kind_profile(self, payload: dict, author: str = "system"):
            self.saved.append({"author": author, **payload})
            self.existing[payload["step_kind_key"]] = {
                **payload,
                "builder_metadata_jsonb": json.dumps(
                    normalize_builder_metadata(payload.get("builder_metadata_jsonb"))
                ),
                "config_schema_jsonb": json.dumps(payload.get("config_schema_jsonb") or {}),
                "updated_at": f"changed-{len(self.saved)}",
            }

    store = _Store()

    first_changed = sync_builtin_step_kind_profiles(store)
    assert first_changed
    store.saved.clear()

    assert sync_builtin_step_kind_profiles(store) == []
    assert store.saved == []


def test_tool_resolver_skips_connector_only_standard_tools():
    from core.application.orchestration.tools.tool_resolver import ToolResolver
    from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
    from core.application.registry.tool_manifest import ToolManifest

    manifest = ToolManifest.model_validate(builtin_standard_tool_manifests()["std.numbers"])
    resolver = ToolResolver()

    resolver.register_from_manifest(manifest)

    assert resolver.list_registered() == {}
    assert resolver.resolve(["std.numbers"]) == []


def test_tool_resolver_registers_builtin_fs_mcp_tools():
    from core.application.orchestration.tools.tool_resolver import ToolResolver
    from core.application.registry.builtin_mcp_tools import builtin_mcp_tool_manifests
    from core.application.registry.tool_manifest import ToolManifest

    manifest = ToolManifest.model_validate(builtin_mcp_tool_manifests()["workflow_ops"])
    resolver = ToolResolver()

    resolver.register_from_manifest(manifest)

    registered = set(resolver.list_registered().get("workflow_ops") or [])
    assert "workflow_list" in registered
    assert not resolver.get_errors()


def test_tool_resolver_leaves_optional_mcp_string_parameters_unset():
    from core.application.orchestration.tools.tool_manifest_contracts import _build_args_schema
    from core.application.registry.builtin_mcp_tools import builtin_mcp_tool_manifests
    from core.application.registry.tool_manifest import ToolManifest

    manifest = ToolManifest.model_validate(builtin_mcp_tool_manifests()["workflow_ops"])
    operation = next(
        item
        for item in manifest.interface.operations
        if str((item or {}).get("name") or "") == "workflow_validate"
    )

    schema = _build_args_schema(manifest, operation)
    validated = schema.model_validate({"workflow_id": "weekly_report"})

    assert validated.project_name is None
    assert validated.model_dump(exclude_unset=True, exclude_none=True) == {
        "workflow_id": "weekly_report",
    }
