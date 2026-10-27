from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from core.infrastructure.db import system_settings_store
from core.infrastructure.db.psycopg_runtime import errorcodes


class _Cursor:
    def __init__(
        self,
        *,
        fetchone_results: list[object] | None = None,
        execute_errors: list[Exception] | None = None,
    ) -> None:
        self.fetchone_results = list(fetchone_results or [])
        self.execute_errors = list(execute_errors or [])
        self.calls: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        if self.execute_errors:
            raise self.execute_errors.pop(0)

    def fetchone(self):
        if self.fetchone_results:
            return self.fetchone_results.pop(0)
        return None


class _Conn:
    def __init__(
        self,
        *,
        fetchone_results: list[object] | None = None,
        execute_errors: list[Exception] | None = None,
    ) -> None:
        self.cursor_obj = _Cursor(fetchone_results=fetchone_results, execute_errors=execute_errors)

    def cursor(self, *args, **kwargs):
        del args, kwargs
        return self.cursor_obj


class _PgLookupError(RuntimeError):
    def __init__(self, pgcode: str) -> None:
        super().__init__(pgcode)
        self.pgcode = pgcode


def test_normalize_retention_settings_merges_defaults_and_clamps_values() -> None:
    payload = system_settings_store.normalize_retention_settings(
        {
            "action_logs_days": "0",
            "job_logs_days": "90",
            "trash_days": "999999",
            "terminal_job_checkpoints_days": "999999",
        },
        defaults={
            "action_logs_days": 14,
            "job_logs_days": 21,
            "trash_days": 28,
            "terminal_job_checkpoints_days": 35,
        },
    )

    assert payload == {
        "action_logs_days": 1,
        "job_logs_days": 90,
        "trash_days": 3650,
        "terminal_job_checkpoints_days": 3650,
        "revisions_keep": 5,
    }


@pytest.mark.parametrize(
    ("saved", "expected"),
    [("12", 12), ("0", 1), ("-3", 1), ("5000", 100), ("many", 7), (None, 7)],
)
def test_revisions_keep_is_a_bounded_count_not_a_day_window(saved, expected) -> None:
    payload = system_settings_store.normalize_retention_settings(
        {"revisions_keep": saved},
        defaults={"revisions_keep": 7},
    )

    assert payload["revisions_keep"] == expected


def test_get_retention_settings_returns_defaults_when_table_missing() -> None:
    conn = _Conn(execute_errors=[_PgLookupError(errorcodes.UNDEFINED_TABLE)])

    payload = system_settings_store.get_retention_settings(
        conn=conn,
        table_exists=lambda table: False,
        defaults={
            "action_logs_days": 10,
            "job_logs_days": 20,
            "trash_days": 30,
            "terminal_job_checkpoints_days": 40,
        },
        account_id="acc_ops",
    )

    assert payload == {
        "account_id": "acc_ops",
        "action_logs_days": 10,
        "job_logs_days": 20,
        "trash_days": 30,
        "terminal_job_checkpoints_days": 40,
        "revisions_keep": 5,
    }


def test_get_retention_settings_tolerates_false_negative_table_exists_probe() -> None:
    conn = _Conn(
        fetchone_results=[
            {
                "setting_key": system_settings_store.RETENTION_SETTINGS_KEY,
                "account_id": "acc_ops",
                "setting_value_jsonb": {
                    "action_logs_days": 12,
                    "job_logs_days": 34,
                    "trash_days": 56,
                    "terminal_job_checkpoints_days": 78,
                },
            }
        ]
    )

    payload = system_settings_store.get_retention_settings(
        conn=conn,
        table_exists=lambda table: False,
        defaults={
            "action_logs_days": 10,
            "job_logs_days": 20,
            "trash_days": 30,
            "terminal_job_checkpoints_days": 40,
        },
        account_id="acc_ops",
    )

    assert payload["account_id"] == "acc_ops"
    assert payload["action_logs_days"] == 12
    assert payload["job_logs_days"] == 34
    assert payload["trash_days"] == 56
    assert payload["terminal_job_checkpoints_days"] == 78


def test_get_retention_settings_includes_metadata_from_saved_row() -> None:
    updated_at = datetime(2026, 4, 5, 12, 0, tzinfo=UTC)
    conn = _Conn(
        fetchone_results=[
            {
                "setting_key": system_settings_store.RETENTION_SETTINGS_KEY,
                "account_id": "acc_team",
                "setting_value_jsonb": {
                    "action_logs_days": 45,
                    "job_logs_days": 60,
                    "trash_days": 15,
                    "terminal_job_checkpoints_days": 75,
                },
                "updated_at": updated_at,
                "updated_by": "alice",
            }
        ]
    )

    payload = system_settings_store.get_retention_settings(
        conn=conn,
        table_exists=lambda table: table == "system_settings",
        defaults={
            "action_logs_days": 10,
            "job_logs_days": 20,
            "trash_days": 30,
            "terminal_job_checkpoints_days": 40,
        },
        account_id="acc_team",
    )

    sql, params = conn.cursor_obj.calls[-1]
    assert "WHERE account_id = %s" in sql
    assert params == ("acc_team", system_settings_store.RETENTION_SETTINGS_KEY)
    assert payload["account_id"] == "acc_team"
    assert payload["action_logs_days"] == 45
    assert payload["job_logs_days"] == 60
    assert payload["trash_days"] == 15
    assert payload["terminal_job_checkpoints_days"] == 75
    assert payload["updated_at"] == updated_at
    assert payload["updated_by"] == "alice"


def test_save_retention_settings_upserts_normalized_payload() -> None:
    updated_at = datetime(2026, 4, 5, 13, 0, tzinfo=UTC)
    conn = _Conn(
        fetchone_results=[
            {
                "setting_key": system_settings_store.RETENTION_SETTINGS_KEY,
                "account_id": "acc_ops",
                "setting_value_jsonb": {
                    "action_logs_days": 14,
                    "job_logs_days": 30,
                    "trash_days": 7,
                    "terminal_job_checkpoints_days": 9,
                },
                "updated_at": updated_at,
                "updated_by": "operator",
            }
        ]
    )

    payload = system_settings_store.save_retention_settings(
        conn=conn,
        settings={
            "action_logs_days": "14",
            "job_logs_days": "30",
            "trash_days": "7",
            "terminal_job_checkpoints_days": "9",
        },
        actor="operator",
        account_id="acc_ops",
    )

    sql, params = conn.cursor_obj.calls[-1]
    assert "INSERT INTO system_settings" in sql
    assert params[0] == "acc_ops"
    assert params[1] == system_settings_store.RETENTION_SETTINGS_KEY
    assert params[3] == "operator"
    assert payload["account_id"] == "acc_ops"
    assert payload["updated_at"] == updated_at
    assert payload["updated_by"] == "operator"


def test_save_retention_settings_raises_runtime_error_when_table_is_missing() -> None:
    conn = _Conn(execute_errors=[_PgLookupError(errorcodes.UNDEFINED_TABLE)])

    try:
        system_settings_store.save_retention_settings(
            conn=conn,
            settings={
                "action_logs_days": 14,
                "job_logs_days": 30,
                "trash_days": 7,
                "terminal_job_checkpoints_days": 9,
            },
            actor="operator",
            account_id="acc_ops",
        )
    except RuntimeError as exc:
        assert str(exc) == "system_settings table is not available"
    else:
        raise AssertionError("Expected RuntimeError when system_settings table is missing")


def test_workflow_sync_loop_status_preserves_active_claim_identity() -> None:
    conn = _Conn(
        fetchone_results=[
            {
                "setting_value_jsonb": {
                    "last_attempt_at": "2026-09-07T04:45:00+00:00",
                    "last_success_at": "2026-09-07T04:44:00+00:00",
                    "last_failure_reason": "",
                    "consecutive_failures": 0,
                    "active_outbox_id": "outbox-active",
                    "active_claim_owner": "worker-1",
                    "active_claim_version": 7,
                }
            }
        ]
    )

    status = system_settings_store.get_workflow_knowledge_sync_loop_status(conn=conn)

    assert status["active_outbox_id"] == "outbox-active"
    assert status["active_claim_owner"] == "worker-1"
    assert status["active_claim_version"] == 7


def test_workflow_sync_attempt_persists_active_claim_identity() -> None:
    conn = _Conn(
        fetchone_results=[
            {
                "setting_value_jsonb": {
                    "last_success_at": "2026-09-07T04:44:00+00:00",
                    "consecutive_failures": 0,
                }
            },
            None,
        ]
    )

    status = system_settings_store.record_workflow_knowledge_sync_loop_attempt(
        conn=conn,
        outbox_id="outbox-active",
        claim_owner="worker-1",
        claim_version=7,
    )

    assert status["active_outbox_id"] == "outbox-active"
    assert status["active_claim_owner"] == "worker-1"
    assert status["active_claim_version"] == 7
    sql, params = conn.cursor_obj.calls[-1]
    saved = json.loads(params[2])
    assert "INSERT INTO system_settings" in sql
    assert saved["active_outbox_id"] == "outbox-active"
    assert saved["active_claim_owner"] == "worker-1"
    assert saved["active_claim_version"] == 7
