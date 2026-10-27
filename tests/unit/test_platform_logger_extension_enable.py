from __future__ import annotations

from unittest.mock import MagicMock

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.extension_scan_lifecycle import (
    sync_enabled_extension_with_llm_lifecycle,
)
from tests.support.platform_test_store import PlatformTestStore


def _enable_extension(logger, extension_key, **kwargs):
    return sync_enabled_extension_with_llm_lifecycle(logger, extension_key, **kwargs)


def test_list_extensions_uses_fallback_columns_when_optional_schema_absent():
    logger = PlatformTestStore.__new__(PlatformTestStore)
    logger.conn = MagicMock()
    logger._table_exists = MagicMock(return_value=True)
    logger._column_exists = MagicMock(return_value=False)
    logger._normalize_extension_row = MagicMock(side_effect=lambda row: row)

    cursor = MagicMock()
    row = {"bundle_id": 1, "extension_key": "example_notification_provider", "kind": "notification"}
    cursor.fetchall.return_value = [row]
    logger.conn.cursor.return_value.__enter__.return_value = cursor

    result = logger.list_extensions(enabled_only=True)

    sql = cursor.execute.call_args.args[0]
    assert "WHERE coalesce(ee.enabled, false) = true" in sql
    assert "'{}'::jsonb AS config_jsonb" in sql
    assert f"'{DEFAULT_ACCOUNT_ID}'::text AS account_id" in sql
    assert result == [row]


def test_enable_extension_defaults_account_id_when_config_omits_it():
    logger = PlatformTestStore.__new__(PlatformTestStore)
    logger.conn = MagicMock()
    logger.conn.conn = None
    logger.conn._pool = None
    logger.conn.autocommit = True
    logger._table_exists = MagicMock(
        side_effect=lambda name: (
            name
            in {
                "enabled_extensions",
                "extension_bundles",
                "extension_releases",
                "extension_installs",
            }
        )
    )

    def _column_exists(table: str, column: str) -> bool:
        return table == "enabled_extensions" and column == "account_id"

    logger._column_exists = MagicMock(side_effect=_column_exists)
    logger.get_extension = MagicMock(
        return_value={"extension_key": "example_notification_provider"}
    )

    cursor = MagicMock()
    cursor.fetchone.side_effect = [
        {"id": 11, "latest_version": "1.0.0"},
        {"id": 22},
        {"manifest_jsonb": {"kind": "local"}, "compiled_manifest_jsonb": {}},
        None,
    ]
    logger.conn.cursor.return_value.__enter__.return_value = cursor

    result = _enable_extension(logger, "example_notification_provider", config={})

    insert_params = next(
        call.args[1]
        for call in cursor.execute.call_args_list
        if "INSERT INTO extension_installs" in call.args[0]
    )
    assert DEFAULT_ACCOUNT_ID in insert_params
    assert result == {"extension_key": "example_notification_provider"}


def test_enable_extension_uses_disabled_state_for_extension_install_row():
    logger = PlatformTestStore.__new__(PlatformTestStore)
    logger.conn = MagicMock()
    logger.conn.conn = None
    logger.conn._pool = None
    logger.conn.autocommit = True
    logger._table_exists = MagicMock(
        side_effect=lambda name: (
            name
            in {
                "enabled_extensions",
                "extension_bundles",
                "extension_releases",
                "extension_installs",
            }
        )
    )

    def _column_exists(table: str, column: str) -> bool:
        return table == "enabled_extensions" and column == "account_id"

    logger._column_exists = MagicMock(side_effect=_column_exists)
    logger.get_extension = MagicMock(
        return_value={"extension_key": "example_notification_provider"}
    )

    cursor = MagicMock()
    cursor.fetchone.side_effect = [
        {"id": 11, "latest_version": "1.0.0"},
        {"id": 22},
        {"manifest_jsonb": {"kind": "local"}, "compiled_manifest_jsonb": {}},
        None,
    ]
    logger.conn.cursor.return_value.__enter__.return_value = cursor

    _enable_extension(logger, "example_notification_provider", enabled=False, config={})

    insert_params = next(
        call.args[1]
        for call in cursor.execute.call_args_list
        if "INSERT INTO extension_installs" in call.args[0]
    )
    assert "disabled" in insert_params
    assert "inactive" not in insert_params


def test_upsert_project_connection_extension_conflicts_on_connection_id():
    logger = PlatformTestStore.__new__(PlatformTestStore)
    logger.conn = MagicMock()
    logger._table_exists = MagicMock(return_value=True)
    logger._column_exists = MagicMock(return_value=False)
    logger.get_project_connection = MagicMock(return_value=None)

    cursor = MagicMock()
    logger.conn.cursor.return_value.__enter__.return_value = cursor

    logger.upsert_project_connection(
        project_id=1,
        extension_id="fixture.notification-provider",
        account_id="acc_default",
        connection_kind="extension",
    )

    sql = cursor.execute.call_args.args[0]
    assert "ON CONFLICT (connection_id)" in sql


def test_upsert_project_connection_extension_runtime_conflicts_on_connection_id():
    logger = PlatformTestStore.__new__(PlatformTestStore)
    logger.conn = MagicMock()
    logger._table_exists = MagicMock(return_value=True)
    logger._column_exists = MagicMock(return_value=False)
    logger.get_project_connection = MagicMock(return_value=None)

    cursor = MagicMock()
    logger.conn.cursor.return_value.__enter__.return_value = cursor

    logger.upsert_project_connection(
        project_id=1,
        extension_id="",
        account_id="acc_default",
        connection_kind="extension_runtime",
        extension_install_id="pi_runtime_1",
        runtime_mode="local",
    )

    sql = cursor.execute.call_args.args[0]
    assert "ON CONFLICT (connection_id)" in sql
