from __future__ import annotations

from core.infrastructure.db import secret_storage_store


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

    def cursor(self, *args, **kwargs):
        cursor = _RecordingCursor(self.calls)
        cursor._fetchone_results = self.fetchone_results
        cursor._fetchall_results = self.fetchall_results
        return cursor


def test_backfill_plaintext_secret_storage_updates_secret_tables_only() -> None:
    conn = _RecordingConn()
    conn.fetchall_results = [
        [{"id": 1, "secret_value_ciphertext": "plain-project"}],
        [{"id": 2, "secret_value_ciphertext": "plain-provider"}],
    ]
    encrypted_values: list[str] = []

    def _encrypt_secret_value(value: str):
        encrypted_values.append(value)
        return (f"enc:{value}", "nonce-1", "aesgcm-v1")

    secret_storage_store.backfill_plaintext_secret_storage(
        conn=conn,
        table_exists=lambda table_name: table_name in {"project_secrets", "provider_secrets"},
        column_exists=lambda table_name, column_name: (
            table_name == "projects" and column_name == "db_dsn"
        ),
        encrypt_secret_value=_encrypt_secret_value,
    )

    executed_sql = "\n".join(sql for sql, _ in conn.calls)
    assert "SELECT id, secret_value_ciphertext\n            FROM project_secrets" in executed_sql
    assert "UPDATE project_secrets" in executed_sql
    assert "SELECT id, secret_value_ciphertext\n            FROM provider_secrets" in executed_sql
    assert "UPDATE provider_secrets" in executed_sql
    assert "projects p" not in executed_sql
    assert "db_dsn" not in executed_sql
    assert encrypted_values == [
        "plain-project",
        "plain-provider",
    ]


def test_backfill_plaintext_secret_storage_skips_when_completion_marker_exists() -> None:
    conn = _RecordingConn()
    conn.fetchone_results = [
        {
            "setting_value_jsonb": {
                "completed": True,
                "plaintext_rows_seen": 0,
            }
        }
    ]

    secret_storage_store.backfill_plaintext_secret_storage(
        conn=conn,
        table_exists=lambda table_name: table_name in {"project_secrets", "provider_secrets"},
        column_exists=lambda table_name, column_name: False,
        encrypt_secret_value=lambda value: (_ for _ in ()).throw(AssertionError(value)),
    )

    executed_sql = "\n".join(sql for sql, _ in conn.calls)
    assert "FROM system_settings" in executed_sql
    assert "FROM project_secrets" not in executed_sql
    assert "FROM provider_secrets" not in executed_sql
