"""MCP answers carry product UI links an AI client can open.

Live in claude.ai (2026-11-17) workflow_draft_save returned the bare path
``/registry/workflows/weekly_sales_report/builder`` and the assistant had to
guess the host.
"""

from __future__ import annotations

from core.entrypoints.mcp.server_api_tool import api_tool_envelope

DRAFT = {
    "workflow_id": "weekly_sales_report",
    "editor_url": "/registry/workflows/weekly_sales_report/builder",
    "jobs": [{"detail_url": "/jobs/job-1", "delete_plan_url": "/api/tools/t/delete-plan"}],
    "description": "/registry/workflows is a path in prose, not a link field",
    "deep_links": {"jobs": ["/jobs/job-2"], "project": "/projects/p-1"},
    "callback_url": "/callback",
    "fields": [{"name": "hook", "default": {"callback_url": "/callback"}}],
}


def test_ui_paths_become_absolute_on_the_public_origin(monkeypatch) -> None:
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://flow.example.com:443/")
    data = api_tool_envelope("workflow_draft_save", DRAFT, "c-1")["data"]
    assert (
        data["editor_url"]
        == "https://flow.example.com/registry/workflows/weekly_sales_report/builder"
    )
    assert data["jobs"][0]["detail_url"] == "https://flow.example.com/jobs/job-1"
    assert data["jobs"][0]["delete_plan_url"] == "/api/tools/t/delete-plan"
    assert data["description"] == DRAFT["description"]
    # User data keeps its value: a workflow default or report row with a *_url
    # key is not a product link (PR 23 review).
    assert data["callback_url"] == "/callback"
    assert data["fields"] == DRAFT["fields"]
    assert data["deep_links"] == {
        "jobs": ["https://flow.example.com/jobs/job-2"],
        "project": "https://flow.example.com/projects/p-1",
    }


def test_links_stay_unchanged_without_a_public_origin(monkeypatch) -> None:
    monkeypatch.delenv("FS_PUBLIC_BASE_URL", raising=False)
    assert api_tool_envelope("workflow_draft_save", DRAFT, "c-1")["data"] == DRAFT
