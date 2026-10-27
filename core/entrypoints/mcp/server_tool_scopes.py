"""Canonical token-scope requirements for every MCP tool.

Remediation epic 2026-07-29, WS-07 — closes `AUDIT-068` and completes
`CLAUDE-P1-04`.

The table controls request-local discovery only. Every tool forwards the
original bearer to the application API, whose normal role, capability,
tenant, project, and workflow policy remains the authoritative enforcement
boundary.

This module maps every tool to the same scope taxonomy the HTTP
surface uses (`core/application/auth/auth_http_runtime.py::_required_http_scopes`),
so one token means the same thing on both transports.

Requirements are **any-of**: holding any listed scope is enough. `scope_granted`
additionally honours wildcards (`jobs:*`, `*:read`, `*:*`) and `admin:all`.

One deliberate divergence from `AUDIT-068`'s wording: workflow reads still accept
`registry.read`, because `/api/workflows` grants exactly that over HTTP. Making
MCP stricter than HTTP would mean the same token behaves differently per
transport, which is the problem this table exists to remove. Project and job
reads do **not** accept `registry.read` — HTTP never granted them either.
"""

from __future__ import annotations

_TOKEN_ADMIN_ALL = "admin:all"
_TOKEN_READ = "read:*"
_TOKEN_COMMAND = "command:*"
_TOKEN_WORKFLOWS_ALL = "workflows:*"

ADMIN = (_TOKEN_ADMIN_ALL,)

_JOBS_READ = ("jobs.read", "jobs:*", _TOKEN_READ, _TOKEN_ADMIN_ALL)
_JOBS_WRITE = ("jobs.write", "jobs:*", _TOKEN_COMMAND, _TOKEN_ADMIN_ALL)
_PROJECTS_READ = ("projects.read", "projects:*", _TOKEN_READ, _TOKEN_ADMIN_ALL)
_WORKFLOWS_READ = (
    "workflows.read",
    _TOKEN_WORKFLOWS_ALL,
    "registry.read",
    "registry:*",
    _TOKEN_READ,
    _TOKEN_ADMIN_ALL,
)
_WORKFLOWS_WRITE = ("workflows.write", _TOKEN_WORKFLOWS_ALL, _TOKEN_COMMAND, _TOKEN_ADMIN_ALL)
# The registry's own workflow read: the definition route accepts registry scopes,
# not the generic read scope.
_WORKFLOW_DEFINITION_READ = (
    "workflows.read",
    _TOKEN_WORKFLOWS_ALL,
    "registry.read",
    "registry:*",
    _TOKEN_ADMIN_ALL,
)
_REPORTS_READ = ("reports.read", "reports:*", _TOKEN_READ, _TOKEN_ADMIN_ALL)
_REPORTS_WRITE = ("reports.write", "reports:*", _TOKEN_COMMAND, _TOKEN_ADMIN_ALL)
_NOTIFY_READ = ("notify.read", "notify:*", _TOKEN_READ, _TOKEN_ADMIN_ALL)
_NOTIFY_DISPATCH = ("notify:dispatch", "notify:*", _TOKEN_COMMAND, _TOKEN_ADMIN_ALL)

#: tool name -> any-of scopes. Every registered tool must appear here; the
#: guardrail test in tests/unit/test_mcp_tool_scopes.py fails on any omission,
#: so a new tool cannot ship without an explicit decision about its scope.
MCP_TOOL_SCOPES: dict[str, tuple[str, ...]] = {
    # --- jobs (read) -------------------------------------------------------
    "job_approval": _JOBS_READ,
    "job_approvals": _JOBS_READ,
    "job_cost_breakdown": _JOBS_READ,
    "job_decision_overrides": _JOBS_READ,
    "job_errors": _JOBS_READ,
    "job_events": _JOBS_READ,
    "job_incident": _JOBS_READ,
    "job_logs": _JOBS_READ,
    "job_messages": _JOBS_READ,
    "job_list": _JOBS_READ,
    "job_approvals_pending": _JOBS_READ,
    "job_result": _JOBS_READ,
    "job_runtime_journal": _JOBS_READ,
    "job_status": _JOBS_READ,
    "job_trace": _JOBS_READ,
    "project_jobs": _JOBS_READ,
    # --- jobs (write / lifecycle) -----------------------------------------
    "job_acknowledge": _JOBS_WRITE,
    "job_unacknowledge": _JOBS_WRITE,
    "job_cancel": _JOBS_WRITE,
    "job_restart": _JOBS_WRITE,
    # Approving a paused gate releases a side effect: never a read scope.
    "job_approval_decide": _JOBS_WRITE,
    # --- projects ----------------------------------------------------------
    "project_list": _PROJECTS_READ,
    # --- workflow authoring: registry creation differs from launch ----------
    "capability_search": _WORKFLOWS_READ,
    "capability_describe": _WORKFLOWS_READ,
    "workflow_examples_search": _WORKFLOWS_READ,
    "workflow_authoring_schema": _WORKFLOWS_READ,
    "workflow_draft_validate": _WORKFLOWS_READ,
    "workflow_draft_save": ADMIN,
    "workflow_definition": _WORKFLOW_DEFINITION_READ,
    "workflow_apply_changes": ADMIN,
    "workflow_convert_group": ADMIN,
    # --- workflows ---------------------------------------------------------
    "workflow_contract": _WORKFLOWS_READ,
    "workflow_list": _WORKFLOWS_READ,
    "workflow_suggestions": _WORKFLOWS_READ,
    "workflow_validate": _WORKFLOWS_READ,
    "workflow_trigger": _WORKFLOWS_WRITE,
    # --- hooks: reads follow the project workflow routes, writes are admin ---
    "workflow_hooks_list": _WORKFLOWS_READ,
    "workflow_hook_get": _WORKFLOWS_READ,
    "workflow_hook_preview": ADMIN,
    "workflow_hook_bind": ADMIN,
    "workflow_hook_unbind": ADMIN,
    "workflow_hook_create_and_bind": ADMIN,
    "workflow_where_used": _WORKFLOWS_READ,
    # --- portability: export/stats follow the project workflow reads; an import
    # creates workflows, so it needs what saving a draft needs ---
    "workflow_export": _WORKFLOWS_READ,
    "workflow_stats": _WORKFLOWS_READ,
    "workflow_import": ADMIN,
    # --- reports -----------------------------------------------------------
    "report_get": _REPORTS_READ,
    "report_generate": _REPORTS_WRITE,
    # --- notifications -----------------------------------------------------
    "project_notification_deliveries": _NOTIFY_READ,
    "project_notification_routes": _NOTIFY_READ,
    "dispatch_project_notification": _NOTIFY_DISPATCH,
}


def required_scopes_for_tool(tool: str) -> tuple[str, ...]:
    """Return the any-of scopes for *tool*.

    Unknown tools fail closed on `admin:all`: an unmapped tool is a wiring
    mistake, and the safe reading of "we do not know what this needs" is the
    most restrictive one, not the least.
    """
    return MCP_TOOL_SCOPES.get(str(tool or "").strip(), ADMIN)


__all__ = ["MCP_TOOL_SCOPES", "required_scopes_for_tool"]
