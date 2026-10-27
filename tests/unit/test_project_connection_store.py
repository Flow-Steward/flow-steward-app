from __future__ import annotations

from datetime import UTC, datetime

from core.application.management.project_connection import ProjectConnection
from core.infrastructure.db import project_connection_store


class _RecordingCursor:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self._calls = calls
        self._fetchone_results: list[object] = []
        self._fetchall_results: list[list[object]] = []
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self._calls.append((str(sql), params))

    def fetchone(self):
        if self._fetchone_results:
            return self._fetchone_results.pop(0)
        return None

    def fetchall(self):
        if self._fetchall_results:
            return self._fetchall_results.pop(0)
        return []


class _RecordingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fetchone_results: list[object] = []
        self.fetchall_results: list[list[object]] = []
        self.description = []

    def cursor(self, *args, **kwargs):
        cursor = _RecordingCursor(self.calls)
        cursor._fetchone_results = self.fetchone_results
        cursor._fetchall_results = self.fetchall_results
        cursor.description = self.description
        return cursor


def test_insert_project_connection_uses_connection_id_conflict_target() -> None:
    conn = _RecordingConn()
    project_connection_store.insert_project_connection(
        conn,
        ProjectConnection(
            project_id=1,
            extension_id=None,
            account_id="acc_default",
            connection_kind="extension_runtime",
            extension_install_id="pi_runtime_1",
            runtime_mode="local",
        ),
    )

    executed_sql = "\n".join(sql for sql, _ in conn.calls)
    assert "INSERT INTO project_connections" in executed_sql
    assert "ON CONFLICT (connection_id)" in executed_sql


def test_get_project_connection_row_decodes_json_and_datetimes() -> None:
    conn = _RecordingConn()
    created_at = datetime(2026, 3, 31, 7, 0, tzinfo=UTC)
    conn.description = [
        ("connection_id",),
        ("project_id",),
        ("connection_config",),
        ("secret_refs",),
        ("created_at",),
    ]
    conn.fetchone_results = [
        (
            "conn_123",
            7,
            '{"chat_id":"123"}',
            '{"refs":{"api_token":"project_connection:conn_123:api_token"}}',
            created_at,
        )
    ]

    row = project_connection_store.get_project_connection_row(conn, "conn_123")

    assert row == {
        "connection_id": "conn_123",
        "project_id": 7,
        "connection_config": {"chat_id": "123"},
        "secret_refs": {"refs": {"api_token": "project_connection:conn_123:api_token"}},
        "created_at": created_at.timestamp(),
    }


def test_runtime_metadata_query_excludes_secret_refs_and_values() -> None:
    conn = _RecordingConn()
    conn.description = [
        ("connection_id",),
        ("runtime_status",),
        ("oauth_metadata",),
    ]
    conn.fetchone_results = [("conn_123", "degraded", {"lifecycle_status": "connected"})]

    row = project_connection_store.get_project_connection_runtime_metadata(conn, "conn_123")

    assert row == {
        "connection_id": "conn_123",
        "runtime_status": "degraded",
        "oauth_metadata": {"lifecycle_status": "connected"},
    }
    sql, params = conn.calls[-1]
    assert "connection_config->'oauth'" in sql
    assert "secret_refs" not in sql
    assert params == ("conn_123",)


def test_platform_runtime_connection_query_keeps_extension_oauth_identity() -> None:
    """The worker must not misclassify extension-owned OAuth as legacy OAuth.

    ``connector_step_runtime`` loads through ``PlatformIntegrationStore`` and
    calls its ``get_project_connection`` method.  If that projection drops the
    provider/configuration identity, a connected store account falls through
    to the legacy PKCE-only resolver and every workflow action fails before the
    extension is invoked.
    """
    from core.application.projects.connection_repository import (
        ProjectConnectionRepositoryMixin,
    )

    class _Store(ProjectConnectionRepositoryMixin):
        def _table_exists(self, _name):
            return True

    conn = _RecordingConn()
    conn.fetchone_results = [{"connection_id": "conn_acme_store"}]
    store = _Store()
    store.conn = conn

    assert store.get_project_connection(connection_id="conn_acme_store") == {
        "connection_id": "conn_acme_store",
        "account_id": "00000000-0000-0000-0000-000000000002",
        "connection_kind": "extension",
        "extension_id": None,
        "display_name": "",
        "extension_install_id": "",
        "connection_type": "",
        "environment": "",
        "credential_revision": 1,
        "runtime_mode": "",
        "remote_base_url": "",
        "health_status": "",
        "runtime_status": "",
        "last_error": "",
        "enabled": True,
        "connection_config": {},
        "secret_refs": {},
        "connection_routes": [],
        "ingress_config": {},
        "workflow_mappings": {},
        "tags": [],
    }
    sql, _params = conn.calls[-1]
    assert "oauth_configuration_id" in sql
    assert "oauth_provider_id" in sql
    assert "oauth_granted_scopes" in sql


def test_ingress_token_claim_is_one_atomic_install_scoped_update() -> None:
    from core.application.projects.connection_repository import (
        ProjectConnectionRepositoryMixin,
    )

    class _Store(ProjectConnectionRepositoryMixin):
        pass

    conn = _RecordingConn()
    conn.fetchone_results = [("connection_notifier",)]
    store = _Store()
    store.conn = conn

    claimed = store.consume_project_connection_ingress_token(
        project_id="project_1",
        extension_id="acme.notification-provider",
        account_id="account_1",
        connection_id="connection_notifier",
        extension_install_id="install_notifier",
        token="opaque-once",
    )

    assert claimed is True
    sql, params = conn.calls[-1]
    assert "UPDATE project_connections" in sql
    assert "pending_hitl_tokens" in sql
    assert "RETURNING connection_id" in sql
    assert "connection_id = %s" in sql
    assert "extension_install_id = %s" in sql
    assert params == (
        "opaque-once",
        "project_1",
        "acme.notification-provider",
        "account_1",
        "opaque-once",
        "connection_notifier",
        "install_notifier",
    )


def test_update_project_connection_row_serializes_json_and_timestamp() -> None:
    conn = _RecordingConn()

    project_connection_store.update_project_connection_row(
        conn,
        "conn_123",
        {
            "connection_config": {"chat_id": "123"},
            "last_seen_ok_at": 1711860000.0,
            "enabled": False,
        },
    )

    update_sql, params = conn.calls[-1]
    assert "UPDATE project_connections SET " in update_sql
    assert "connection_config = %s::jsonb" in update_sql
    assert "last_seen_ok_at = to_timestamp(%s)" in update_sql
    assert "enabled = %s" in update_sql
    assert params == ['{"chat_id": "123"}', 1711860000.0, False, "conn_123"]


def test_list_extension_connections_for_update_uses_exact_extension_predicate() -> None:
    conn = _RecordingConn()

    rows = project_connection_store.list_extension_connection_rows_for_update(
        conn,
        extension_id="fixture.extension-oauth",
        connection_type="fixture_oauth_account",
    )

    assert rows == []
    sql, params = conn.calls[-1]
    assert "extension_id = %s" in sql
    assert "connection_type = %s" in sql
    assert "FOR UPDATE" in sql
    assert params == (
        "fixture.extension-oauth",
        "fixture_oauth_account",
    )
