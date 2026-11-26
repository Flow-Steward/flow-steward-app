"""Operator jobs-list filters against real PostgreSQL rows (ADR 0075).

The filters compare the full values written at run acceptance (meta.filter_values)
and the stored outputs (meta.workflow_outputs), so a match past the display
projection's cut (strings at 2 KB, lists at 25 items) is still found.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from core.infrastructure.db import job_queries_store

pytestmark = pytest.mark.integration

WORKFLOW = "wf.operator-filters"
LONG_NOTE = "x" * 2100 + " needle"
TAGS = [f"t{index}" for index in range(1, 31)]


@pytest.fixture
def conn(disposable_db):
    from core.infrastructure.db import psycopg_runtime

    connection = psycopg_runtime.connect(disposable_db.dsn)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _project(conn) -> str:
    project_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, description) VALUES (%s, %s, '')",
            (project_id, f"operator-filters-{project_id[:8]}"),
        )
    return project_id


def _job(
    conn,
    project_id: str,
    *,
    status: str,
    meta: dict[str, Any],
    minutes_ago: int,
    task_source: str = "manual_ui",
) -> str:
    job_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (
                id, project_id, task_id, thread_id, status, meta, started_at,
                selected_workflow, task_source
            )
            VALUES (
                %s, %s, %s, %s, %s, %s::jsonb, NOW() - (%s * INTERVAL '1 minute'), %s, %s
            )
            """,
            (
                job_id,
                project_id,
                f"task-{job_id[:8]}",
                f"thread-{job_id}",
                status,
                json.dumps(meta),
                minutes_ago,
                WORKFLOW,
                task_source,
            ),
        )
    return job_id


@pytest.fixture
def jobs(conn):
    project_id = _project(conn)
    long_job = _job(
        conn,
        project_id,
        status="completed",
        minutes_ago=1,
        meta={
            "filter_values": {
                "note": LONG_NOTE,
                "tags": TAGS,
                "urgent": True,
                "signed_on": "2026-03-05",
            },
            "workflow_outputs": {"outputs": {"total": 120.5, "verdict": "ok"}},
        },
    )
    short_job = _job(
        conn,
        project_id,
        status="failed",
        minutes_ago=2,
        meta={
            "filter_values": {
                "note": "short",
                "tags": ["t1"],
                "urgent": False,
                "signed_on": "2025-12-31",
            },
            "workflow_outputs": {"outputs": {"total": 5}},
        },
    )
    # A run accepted before filter values existed matches no value filter.
    _job(conn, project_id, status="completed", minutes_ago=3, meta={})
    # A Test Run with matching values never appears in the operator list.
    _job(
        conn,
        project_id,
        status="completed",
        minutes_ago=4,
        task_source="workflow_test_run",
        meta={"filter_values": {"note": LONG_NOTE, "tags": TAGS}},
    )
    return {"project": project_id, "long": long_job, "short": short_job}


def _ids(conn, jobs, *filters: dict[str, Any]) -> list[str]:
    rows = job_queries_store.list_jobs_for_attention(
        conn,
        column_exists=lambda *_args: True,
        project_id=jobs["project"],
        workflow_id=WORKFLOW,
        exclude_task_source="workflow_test_run",
        value_filters=list(filters),
    )
    count = job_queries_store.count_jobs_for_attention(
        conn,
        column_exists=lambda *_args: True,
        project_id=jobs["project"],
        workflow_id=WORKFLOW,
        exclude_task_source="workflow_test_run",
        value_filters=list(filters),
    )
    ids = [str(row["id"]) for row in rows]
    assert count == len(ids)
    return ids


def _f(source: str, key: str, filter_type: str, value: Any) -> dict[str, Any]:
    return {"source": source, "key": key, "type": filter_type, "value": value}


def test_search_finds_text_past_the_display_cut(conn, jobs) -> None:
    assert _ids(conn, jobs, _f("input", "note", "search", "needle")) == [jobs["long"]]
    assert _ids(conn, jobs, _f("input", "note", "search", "%")) == []


def test_a_multi_select_matches_its_thirtieth_element(conn, jobs) -> None:
    assert _ids(conn, jobs, _f("input", "tags", "choice_multi", ("t30",))) == [jobs["long"]]
    assert _ids(conn, jobs, _f("input", "tags", "choice_multi", ("t1", "t99"))) == [
        jobs["long"],
        jobs["short"],
    ]


def test_a_select_output_matches_its_value(conn, jobs) -> None:
    assert _ids(conn, jobs, _f("output", "verdict", "choice", ("ok",))) == [jobs["long"]]


def test_number_and_date_ranges(conn, jobs) -> None:
    assert _ids(conn, jobs, _f("output", "total", "number_range", (100.0, None))) == [jobs["long"]]
    assert _ids(conn, jobs, _f("output", "total", "number_range", (None, 10.0))) == [jobs["short"]]
    assert _ids(conn, jobs, _f("input", "signed_on", "date_range", ("2026-01-01", None))) == [
        jobs["long"]
    ]
    assert _ids(
        conn, jobs, _f("input", "signed_on", "date_range", ("2025-12-01", "2025-12-31"))
    ) == [jobs["short"]]


def test_boolean_and_status_filters_combine(conn, jobs) -> None:
    assert _ids(conn, jobs, _f("input", "urgent", "boolean", True)) == [jobs["long"]]
    assert _ids(conn, jobs, _f("input", "urgent", "boolean", False)) == [jobs["short"]]
    assert _ids(
        conn,
        jobs,
        _f("job", "status", "choice_multi", ("failed",)),
        _f("input", "urgent", "boolean", False),
    ) == [jobs["short"]]
    assert _ids(conn, jobs, _f("job", "status", "choice", ("completed",))) != []
