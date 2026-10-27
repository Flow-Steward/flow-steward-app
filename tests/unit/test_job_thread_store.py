from __future__ import annotations

import json
from unittest.mock import MagicMock

from core.infrastructure.db import job_thread_store


def _mock_conn(row=None, *, rowcount: int = 0) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = row
    cur.rowcount = rowcount
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur
    return conn, cur


def test_get_job_context_by_thread_id_returns_dict_row() -> None:
    conn, cur = _mock_conn({"id": 7, "project_id": 11, "meta": {"a": 1}})

    payload = job_thread_store.get_job_context_by_thread_id(conn, "thread-7")

    assert payload == {"id": 7, "project_id": 11, "meta": {"a": 1}}
    cur.execute.assert_called_once_with(
        "SELECT id, project_id, meta FROM jobs WHERE thread_id = %s",
        ("thread-7",),
    )


def test_get_job_meta_by_thread_id_returns_none_when_missing() -> None:
    conn, cur = _mock_conn(None)

    payload = job_thread_store.get_job_meta_by_thread_id(conn, "thread-8")

    assert payload is None
    cur.execute.assert_called_once_with(
        "SELECT meta FROM jobs WHERE thread_id = %s",
        ("thread-8",),
    )


def test_update_job_status_by_thread_id_optionally_commits() -> None:
    conn, cur = _mock_conn(rowcount=2)

    updated = job_thread_store.update_job_status_by_thread_id(
        conn,
        thread_id="thread-9",
        status="awaiting_approval",
        commit=True,
    )

    assert updated == 2
    cur.execute.assert_called_once_with(
        "UPDATE jobs SET status = %s WHERE thread_id = %s",
        ("awaiting_approval", "thread-9"),
    )
    conn.commit.assert_called_once()


def test_update_job_meta_by_thread_id_serializes_dict_payloads() -> None:
    conn, cur = _mock_conn(rowcount=1)
    meta = {"approvals": [{"approved": True}]}

    updated = job_thread_store.update_job_meta_by_thread_id(
        conn,
        thread_id="thread-10",
        meta=meta,
    )

    assert updated == 1
    cur.execute.assert_called_once_with(
        "UPDATE jobs SET meta = %s WHERE thread_id = %s",
        (json.dumps(meta), "thread-10"),
    )
