"""MCP answers about runs and workflows carry a link to the product page.

Live in claude.ai (2026-11-18) a manager asked for the last run of a workflow
"with links to the run page and the workflow editor"; no tool returned either,
so the assistant could only quote ids.
"""

from __future__ import annotations

import pytest

from core.entrypoints.mcp.server_api_tool import api_tool_envelope

ORIGIN = "https://flow.example.com"


@pytest.fixture(autouse=True)
def _public_origin(monkeypatch) -> None:
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", ORIGIN)


def _data(tool: str, data: object) -> dict:
    return api_tool_envelope(tool, data, "c-1")["data"]


def test_trigger_status_and_report_link_the_run_page() -> None:
    run = f"{ORIGIN}/jobs/job-1"
    assert _data("workflow_trigger", {"job_id": "job-1", "status": "pending"})["job_url"] == run
    status = _data(
        "job_status",
        {"id": "job-1", "active_steps": [{"step_id": "review", "step_execution_id": "s-1"}]},
    )
    assert status["job_url"] == run
    assert status["active_steps"] == [{"step_id": "review", "step_execution_id": "s-1"}]
    report = {"job": {"id": "job-1", "status": "completed"}, "report": {"title": "Weekly"}}
    assert _data("workflow_run_report", report)["job_url"] == run
    assert _data("job_result", {"job": {"id": "job-1"}, "artifacts": []})["job_url"] == run


def test_restart_links_both_runs() -> None:
    data = _data("job_restart", {"job_id": "old", "new_job_id": "new"})
    assert data["job_url"] == f"{ORIGIN}/jobs/old"
    assert data["new_job_url"] == f"{ORIGIN}/jobs/new"


def test_run_lists_link_every_row() -> None:
    listed = _data("job_list", {"jobs": [{"job_id": "a"}, {"job_id": "b"}], "total": 2})
    assert [row["job_url"] for row in listed["jobs"]] == [f"{ORIGIN}/jobs/a", f"{ORIGIN}/jobs/b"]
    pending = _data("job_approvals_pending", {"jobs": [{"id": "c"}], "has_more": False})
    assert pending["jobs"][0]["job_url"] == f"{ORIGIN}/jobs/c"
    assert _data("project_jobs", {"jobs": [{"id": "d"}]})["jobs"][0]["job_url"] == (
        f"{ORIGIN}/jobs/d"
    )


def test_workflow_answers_link_the_editor() -> None:
    editor = f"{ORIGIN}/registry/workflows/weekly_sales_report/builder"
    stats = _data("workflow_stats", {"workflow_id": "weekly_sales_report", "total_runs": 3})
    assert stats["editor_url"] == editor
    listed = _data("workflow_list", {"workflows": [{"workflow_id": "weekly_sales_report"}]})
    assert listed["workflows"][0]["editor_url"] == editor


def test_only_listed_ids_become_links() -> None:
    definition = {
        "workflow_id": "w",
        "steps": [{"id": "load", "workflow_id": "child"}],
        "job_id": "not-a-run-of-this-tool",
    }
    data = _data("workflow_definition", definition)
    assert data["editor_url"] == f"{ORIGIN}/registry/workflows/w/builder"
    assert "job_url" not in data
    assert data["steps"] == definition["steps"]
    assert "job_url" not in _data("job_events", {"events": [{"job_id": "x"}], "last_id": 1})


def test_existing_link_is_kept_and_ids_are_escaped() -> None:
    saved = _data("workflow_set_enabled", {"workflow_id": "w", "editor_url": "/custom"})
    assert saved["editor_url"] == f"{ORIGIN}/custom"
    assert _data("job_cancel", {"job_id": "a/b c"})["job_url"] == f"{ORIGIN}/jobs/a%2Fb%20c"


def test_links_stay_relative_without_a_public_origin(monkeypatch) -> None:
    monkeypatch.delenv("FS_PUBLIC_BASE_URL", raising=False)
    assert _data("workflow_trigger", {"job_id": "job-1"})["job_url"] == "/jobs/job-1"
