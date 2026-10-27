from __future__ import annotations

from unittest.mock import MagicMock

from core.application.management.snapshot_serializers import project_settings_diff
from tests.support.platform_test_store import PlatformTestStore as PlatformLogger


class _RecordingCursor:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self._calls = calls
        self._fetchone_results: list[object] = []
        self._fetchall_results: list[list[object]] = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self._calls.append((str(sql), params))
        exc = getattr(self, "_execute_exception", None)
        if exc is not None:
            raise exc

    def fetchone(self):
        if self._fetchone_results:
            return self._fetchone_results.pop(0)
        return (123,)

    def fetchall(self):
        if self._fetchall_results:
            return self._fetchall_results.pop(0)
        return []


class _RecordingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fetchone_results: list[object] = []
        self.fetchall_results: list[list[object]] = []
        self.execute_exception: Exception | None = None

    def cursor(self, *args, **kwargs):
        cursor = _RecordingCursor(self.calls)
        cursor._fetchone_results = self.fetchone_results
        cursor._fetchall_results = self.fetchall_results
        cursor._execute_exception = self.execute_exception
        return cursor


def _make_logger() -> PlatformLogger:
    logger = PlatformLogger.__new__(PlatformLogger)
    logger.conn = _RecordingConn()
    logger._column_exists_cache = {}
    logger._column_exists = lambda table_name, column_name: False  # type: ignore[method-assign]
    return logger


def test_get_project_settings_snapshot_collects_normalized_project_payload() -> None:
    logger = _make_logger()
    logger._table_exists = lambda table_name: table_name == "project_secrets"  # type: ignore[method-assign]
    logger.get_project = MagicMock(
        return_value={
            "id": 7,
            "name": "Demo",
            "description": "Normalized project description",
            "agents_config": {"writer": {"model": "gpt-5"}},
            "project_rules": "Ship it",
        }
    )
    logger.get_project_workflows = MagicMock(
        return_value=[
            {"workflow_id": "wf-default", "is_default": True, "enabled": True, "priority": 10},
            {"workflow_id": "wf-alt", "is_default": False, "enabled": False, "priority": 20},
        ]
    )
    logger.get_project_policy = MagicMock(return_value={"autonomy_mode": "supervised"})
    logger.get_project_provider_bindings = MagicMock(
        return_value=[
            {
                "provider_key": "fixture_notifier",
                "enabled": True,
                "binding_config": {"destination_id": "1"},
            }
        ]
    )
    logger.list_project_data_connections = MagicMock(
        return_value=[
            {
                "connection_id": "warehouse-primary",
                "display_name": "Warehouse Primary",
                "health_status": "healthy",
                "last_checked_at": "2026-07-31T10:00:00Z",
                "schema_snapshot": {"tables": [{"name": "orders", "columns": ["id"]}]},
            }
        ]
    )
    logger.conn.fetchall_results = [[("api_token",), ("db_dsn",)]]

    snapshot = logger.get_project_settings_snapshot(7)

    assert snapshot["schema_version"] == 1
    assert snapshot["operator_default_workflow_id"] == "wf-default"
    assert snapshot["project"]["description"] == "Normalized project description"
    assert snapshot["project"]["data_connection_ids"] == ["warehouse-primary"]
    assert "assigned_data_connections" not in snapshot["project"]
    assert snapshot["project"]["agents_config"] == {"writer": {"model": "gpt-5"}}
    assert snapshot["project"]["project_rules"] == "Ship it"
    assert snapshot["project_policy"]["autonomy_mode"] == "supervised"
    assert snapshot["project_providers"][0]["provider_key"] == "fixture_notifier"
    assert snapshot["project_secret_keys"] == ["api_token", "db_dsn"]


def test_project_settings_diff_ignores_legacy_derived_connection_snapshots() -> None:
    before = {
        "project": {
            "name": "Demo",
            "description": "Before",
            "data_connection_ids": ["warehouse-primary"],
            "assigned_data_connections": [
                {
                    "connection_id": "warehouse-primary",
                    "health_status": "healthy",
                    "last_checked_at": "2026-07-31T10:00:00Z",
                    "schema_snapshot": {"tables": [{"name": "orders", "columns": ["id"]}]},
                }
            ],
        },
    }
    after = {
        "project": {
            "name": "Demo",
            "description": "After",
            "data_connection_ids": ["warehouse-primary"],
            "assigned_data_connections": [
                {
                    "connection_id": "warehouse-primary",
                    "health_status": "failed",
                    "last_checked_at": "2026-07-31T10:05:00Z",
                    "schema_snapshot": {
                        "tables": [{"name": "orders", "columns": ["id", "volatile_remote_column"]}]
                    },
                }
            ],
        },
    }

    diff = project_settings_diff(before, after)

    assert diff == {
        "changes": [
            {
                "path": "project.description",
                "change": "changed",
                "from": "Before",
                "to": "After",
            }
        ],
        "summary": {"total": 1, "added": 0, "removed": 0, "changed": 1},
    }

    connection_change = project_settings_diff(
        {"project": {"data_connection_ids": ["warehouse-primary"]}},
        {"project": {"data_connection_ids": ["warehouse-primary", "analytics"]}},
    )
    assert connection_change["changes"] == [
        {
            "path": "project.data_connection_ids[1]",
            "change": "added",
            "from": None,
            "to": "analytics",
        }
    ]


def test_create_project_settings_revision_persists_snapshot_json() -> None:
    logger = _make_logger()
    logger._table_exists = lambda table_name: table_name == "project_settings_revisions"  # type: ignore[method-assign]
    logger.get_project_settings_snapshot = MagicMock(return_value={"project": {"name": "Demo"}})  # type: ignore[method-assign]

    revision_id = logger.create_project_settings_revision(
        9,
        author="alice",
        action="save",
        source="ui",
    )

    assert revision_id == "123"
    sql, params = logger.conn.calls[-1]
    assert "INSERT INTO project_settings_revisions" in sql
    assert params[:4] == ("9", "alice", "save", "ui")
    assert params[4] == '{"project": {"name": "Demo"}}'


def test_get_project_settings_revision_returns_dict_snapshot() -> None:
    logger = _make_logger()
    logger._table_exists = lambda table_name: table_name == "project_settings_revisions"  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        {
            "id": 7,
            "project_id": "9",
            "author": "alice",
            "action": "save",
            "source": "ui",
            "snapshot_jsonb": {"project": {"name": "Demo"}},
            "created_at": None,
        }
    ]

    revision = logger.get_project_settings_revision(9, 7)

    assert revision is not None
    assert revision["id"] == 7
    assert revision["snapshot_jsonb"]["project"]["name"] == "Demo"


def test_get_project_settings_revision_returns_none_for_invalid_uuid_revision() -> None:
    logger = _make_logger()
    logger._table_exists = lambda table_name: table_name == "project_settings_revisions"  # type: ignore[method-assign]
    logger.conn.execute_exception = Exception('invalid input syntax for type uuid: "99999"')

    revision = logger.get_project_settings_revision(9, "99999")

    assert revision is None


def test_restore_project_settings_revision_updates_project_and_provider_bindings() -> None:
    logger = _make_logger()
    logger.get_project_settings_revision = MagicMock(  # type: ignore[method-assign]
        return_value={
            "id": 7,
            "project_id": "9",
            "snapshot_jsonb": {
                "project": {
                    "name": "Demo",
                    "description": "Restored description",
                    "data_connection_ids": ["warehouse", "analytics"],
                    "agents_config": {"writer": {"model": "gpt-5"}},
                    "project_rules": "Ship it",
                },
                "project_workflows": [
                    {"workflow_id": "wf-default", "enabled": True},
                    {"workflow_id": "wf-alt", "enabled": False},
                ],
                "operator_default_workflow_id": "wf-default",
                "project_policy": {"autonomy_mode": "auto"},
                "project_providers": [
                    {
                        "provider_key": "fixture_notifier",
                        "enabled": True,
                        "binding_config": {"destination_id": "1"},
                    }
                ],
            },
        }
    )
    logger.update_project = MagicMock()  # type: ignore[method-assign]
    logger.set_project_data_connections = MagicMock()  # type: ignore[method-assign]
    logger.get_project_provider_bindings = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {
                "provider_key": "fixture_notifier",
                "enabled": True,
                "binding_config": {"destination_id": "old"},
            },
            {"provider_key": "slack", "enabled": True, "binding_config": {"channel": "ops"}},
        ]
    )
    logger.save_project_provider_binding = MagicMock()  # type: ignore[method-assign]
    logger.create_project_settings_revision = MagicMock(return_value=11)  # type: ignore[method-assign]

    payload = logger.restore_project_settings_revision(9, 7, author="alice")

    logger.update_project.assert_called_once()
    update_kwargs = logger.update_project.call_args.kwargs
    assert update_kwargs["name"] == "Demo"
    assert update_kwargs["description"] == "Restored description"
    assert update_kwargs["agents_config"] == {"writer": {"model": "gpt-5"}}
    assert update_kwargs["project_rules"] == "Ship it"
    assert update_kwargs["operator_default_workflow_id"] == "wf-default"
    assert update_kwargs["project_workflow_ids"] == ["wf-default"]
    assert update_kwargs["project_policy"]["autonomy_mode"] == "auto"
    logger.set_project_data_connections.assert_called_once_with("9", ["warehouse", "analytics"])

    calls = logger.save_project_provider_binding.call_args_list
    assert len(calls) == 2
    assert calls[0].args[:2] == ("9", "slack")
    assert calls[0].kwargs["enabled"] is False
    assert calls[1].args[:2] == ("9", "fixture_notifier")
    assert calls[1].kwargs["enabled"] is True
    assert calls[1].kwargs["config"] == {"destination_id": "1"}

    logger.create_project_settings_revision.assert_called_once_with(
        "9",
        author="alice",
        action="restore",
        source="revision:7",
    )
    assert payload == {
        "status": "restored",
        "project_id": "9",
        "restored_from_revision_id": "7",
        "new_revision_id": "11",
    }


def test_update_project_accepts_restored_workflow_snapshot() -> None:
    logger = _make_logger()
    logger._table_exists = lambda _table_name: False  # type: ignore[method-assign]
    logger._sync_project_workflows_cur = MagicMock()  # type: ignore[method-assign]

    logger.update_project(
        "project-1",
        name="Demo",
        project_workflow_ids=["wf-active"],
        operator_default_workflow_id="wf-active",
    )

    logger._sync_project_workflows_cur.assert_called_once()
    sync_call = logger._sync_project_workflows_cur.call_args
    assert sync_call.kwargs["project_id"] == "project-1"
    assert sync_call.kwargs["project_workflow_ids"] == ["wf-active"]
    assert sync_call.kwargs["operator_default_workflow_id"] == "wf-active"
