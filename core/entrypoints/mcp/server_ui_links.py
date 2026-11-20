"""Product page links that MCP answers carry for the AI client.

The application API serves Flow Steward's own browser, which knows its routes
and needs no link next to an id. An AI client in another app does: without a
link it can only quote ids, as claude.ai did on 2026-11-18 when a manager asked
for "the run page and the workflow editor". These helpers add ``job_url`` (the
run page) and ``editor_url`` (the workflow editor) next to the id each tool
already returns; ``server_api_tool`` then makes them absolute on the public
origin. Each tool's id location is listed explicitly: a key that merely looks
like an id elsewhere in an answer (a step id, an id inside workflow data) never
becomes a link.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

#: Where each tool's answer names the one run it is about.
_RUN_ID_PATHS: dict[str, tuple[str, ...]] = {
    "job_status": ("id",),
    "job_result": ("job", "id"),
    "workflow_run_report": ("job", "id"),
    **dict.fromkeys(
        (
            "job_acknowledge",
            "job_approval",
            "job_approval_decide",
            "job_approvals",
            "job_cancel",
            "job_decision_overrides",
            "job_errors",
            "job_incident",
            "job_restart",
            "job_runtime_journal",
            "job_trace",
            "job_unacknowledge",
            "workflow_trigger",
        ),
        ("job_id",),
    ),
}

#: Tools that list runs: (list key, id key of each row).
_RUN_ROWS: dict[str, tuple[str, str]] = {
    "job_approvals_pending": ("jobs", "id"),
    "job_list": ("jobs", "job_id"),
    "project_jobs": ("jobs", "id"),
}

#: Tools whose answer names one workflow by a top-level ``workflow_id``.
_WORKFLOW_TOOLS = frozenset(
    {
        "workflow_apply_changes",
        "workflow_contract",
        "workflow_definition",
        "workflow_set_enabled",
        "workflow_stats",
        "workflow_validate",
    }
)

#: Tools that list workflows: (list key, id key of each row).
_WORKFLOW_ROWS: dict[str, tuple[str, str]] = {"workflow_list": ("workflows", "workflow_id")}


def run_page_path(job_id: str) -> str:
    """The product page of one run."""
    return f"/jobs/{quote(job_id, safe='')}"


def workflow_editor_path(workflow_id: str) -> str:
    """The editor of one workflow."""
    return f"/registry/workflows/{quote(workflow_id, safe='')}/builder"


def with_ui_links(tool: str, data: object) -> object:
    """*data* with ``job_url`` / ``new_job_url`` / ``editor_url`` beside the ids it names.

    A link already present is kept, and an answer of another shape is returned
    unchanged.
    """
    if not isinstance(data, dict):
        return data
    result: dict[str, Any] = dict(data)
    job_id = _string_at(result, _RUN_ID_PATHS.get(tool, ()))
    if job_id:
        result.setdefault("job_url", run_page_path(job_id))
    new_job_id = _string_at(result, ("new_job_id",)) if tool == "job_restart" else ""
    if new_job_id:
        result.setdefault("new_job_url", run_page_path(new_job_id))
    workflow_id = _string_at(result, ("workflow_id",)) if tool in _WORKFLOW_TOOLS else ""
    if workflow_id:
        result.setdefault("editor_url", workflow_editor_path(workflow_id))
    if tool in _RUN_ROWS:
        rows_key, id_key = _RUN_ROWS[tool]
        result[rows_key] = _rows_with_link(result.get(rows_key), id_key, "job_url", run_page_path)
    if tool in _WORKFLOW_ROWS:
        rows_key, id_key = _WORKFLOW_ROWS[tool]
        result[rows_key] = _rows_with_link(
            result.get(rows_key), id_key, "editor_url", workflow_editor_path
        )
    return result


def _string_at(data: dict[str, Any], path: tuple[str, ...]) -> str:
    value: object = data
    for key in path:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return value.strip() if path and isinstance(value, str) else ""


def _rows_with_link(rows: object, id_key: str, link_key: str, path_of: Any) -> object:
    if not isinstance(rows, list):
        return rows
    linked: list[object] = []
    for row in rows:
        row_id = _string_at(row, (id_key,)) if isinstance(row, dict) else ""
        if row_id:
            row = {**row}
            row.setdefault(link_key, path_of(row_id))
        linked.append(row)
    return linked


__all__ = ["run_page_path", "with_ui_links", "workflow_editor_path"]
