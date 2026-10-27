from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.infrastructure.db import job_admin_store

VALID_JOB_ID = "a1111111-2222-4333-8444-b55555555555"


def _conn_with_cursor(row: dict | None = None) -> tuple[MagicMock, MagicMock]:
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = row

    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_get_job_rejects_legacy_numeric_id_without_query() -> None:
    conn, cur = _conn_with_cursor()

    assert job_admin_store.get_job(conn, job_id="1") is None

    conn.cursor.assert_not_called()
    cur.execute.assert_not_called()


def test_get_job_normalizes_uuid_before_query() -> None:
    conn, cur = _conn_with_cursor({"id": VALID_JOB_ID, "status": "pending"})

    result = job_admin_store.get_job(conn, job_id=VALID_JOB_ID.upper())

    assert result == {"id": VALID_JOB_ID, "status": "pending"}
    cur.execute.assert_called_once()
    assert cur.execute.call_args.args[1] == (VALID_JOB_ID,)


def test_get_job_joins_registry_workflow_display_name() -> None:
    conn, cur = _conn_with_cursor(
        {
            "id": VALID_JOB_ID,
            "selected_workflow": "inventory_import",
            "selected_workflow_display_name": "Inventory Import",
        }
    )

    result = job_admin_store.get_job(conn, job_id=VALID_JOB_ID)

    assert result is not None
    assert result["selected_workflow_display_name"] == "Inventory Import"
    sql = cur.execute.call_args.args[0]
    assert "LEFT JOIN registry_workflows rw ON rw.workflow_id = j.selected_workflow" in sql
    assert "rw.display_name AS selected_workflow_display_name" in sql


def test_get_job_joins_the_hook_owner_so_a_hook_run_can_be_labelled() -> None:
    conn, cur = _conn_with_cursor({"id": VALID_JOB_ID})

    job_admin_store.get_job(conn, job_id=VALID_JOB_ID)

    sql = cur.execute.call_args.args[0]
    assert "COALESCE(rw.settings ? 'hook_storage', false) AS hook_storage" in sql
    assert "aw.display_name AS hook_author_workflow_display_name" in sql
    assert "rw.settings -> 'hook_storage' ->> 'author_workflow_id'" in sql


def test_delete_job_removes_checkpoints_before_job_row() -> None:
    conn, cur = _conn_with_cursor()
    job = {"id": VALID_JOB_ID, "status": "completed", "thread_id": "thread-delete"}
    existing_tables = {
        "checkpoint_writes",
        "checkpoint_blobs",
        "checkpoints",
        "platform_events",
    }
    cur.rowcount = 1

    deleted = job_admin_store.delete_job(
        conn,
        table_exists=lambda table: table in existing_tables,
        get_job_func=lambda _job_id: job,
        job_id=VALID_JOB_ID,
    )

    assert deleted is True
    statements = [call.args[0] for call in cur.execute.call_args_list]
    assert statements[:3] == [
        "DELETE FROM checkpoint_writes WHERE thread_id = %s",
        "DELETE FROM checkpoint_blobs WHERE thread_id = %s",
        "DELETE FROM checkpoints WHERE thread_id = %s",
    ]
    assert "DELETE FROM checkpoint_migrations WHERE thread_id = %s" not in statements
    assert statements[-1] == "DELETE FROM jobs WHERE id = %s"
    assert cur.execute.call_args_list[0].args[1] == ("thread-delete",)


def test_delete_job_without_thread_id_leaves_checkpoints_untouched() -> None:
    conn, cur = _conn_with_cursor()
    job = {"id": VALID_JOB_ID, "status": "completed", "thread_id": ""}
    cur.rowcount = 1

    deleted = job_admin_store.delete_job(
        conn,
        table_exists=lambda _table: True,
        get_job_func=lambda _job_id: job,
        job_id=VALID_JOB_ID,
    )

    assert deleted is True
    statements = [call.args[0] for call in cur.execute.call_args_list]
    assert not any("checkpoint_" in statement for statement in statements)


def test_delete_job_refuses_durable_waits_like_an_approval() -> None:
    for status in ("awaiting_approval", "awaiting_timer", "awaiting_child"):
        conn, cur = _conn_with_cursor()
        job = {"id": VALID_JOB_ID, "status": status, "thread_id": "thread-wait"}

        with pytest.raises(ValueError, match="Cannot delete active job"):
            job_admin_store.delete_job(
                conn,
                table_exists=lambda _table: True,
                get_job_func=lambda _job_id, job=job: job,
                job_id=VALID_JOB_ID,
            )

        cur.execute.assert_not_called()
