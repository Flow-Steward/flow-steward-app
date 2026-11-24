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

ADR 0076 §4: :data:`MCP_TOOL_PERMISSIONS` names the catalogue permission each tool
needs, the same key the route table gives the API route the tool calls. A person may
use a tool exactly when their role may call that route; the test matrix proves it per
system role.
"""

from __future__ import annotations

_TOKEN_ADMIN_ALL = "admin:all"
_TOKEN_READ = "read:*"
_TOKEN_COMMAND = "command:*"
_TOKEN_WORKFLOWS_ALL = "workflows:*"
_TOKEN_REGISTRY_READ = "registry.read"
_TOKEN_REGISTRY_ALL = "registry:*"

ADMIN = (_TOKEN_ADMIN_ALL,)

_JOBS_READ = ("jobs.read", "jobs:*", _TOKEN_READ, _TOKEN_ADMIN_ALL)
_JOBS_WRITE = ("jobs.write", "jobs:*", _TOKEN_COMMAND, _TOKEN_ADMIN_ALL)
_PROJECTS_READ = ("projects.read", "projects:*", _TOKEN_READ, _TOKEN_ADMIN_ALL)
_PROJECTS_WRITE = ("projects.write", "projects:*", _TOKEN_COMMAND, _TOKEN_ADMIN_ALL)
_WORKFLOWS_READ = (
    "workflows.read",
    _TOKEN_WORKFLOWS_ALL,
    _TOKEN_REGISTRY_READ,
    _TOKEN_REGISTRY_ALL,
    _TOKEN_READ,
    _TOKEN_ADMIN_ALL,
)
_WORKFLOWS_WRITE = ("workflows.write", _TOKEN_WORKFLOWS_ALL, _TOKEN_COMMAND, _TOKEN_ADMIN_ALL)
# The registry's own workflow read: the definition route accepts registry scopes,
# not the generic read scope.
_WORKFLOW_DEFINITION_READ = (
    "workflows.read",
    _TOKEN_WORKFLOWS_ALL,
    _TOKEN_REGISTRY_READ,
    _TOKEN_REGISTRY_ALL,
    _TOKEN_ADMIN_ALL,
)
_REPORTS_READ = ("reports.read", "reports:*", _TOKEN_READ, _TOKEN_ADMIN_ALL)
_REPORTS_WRITE = ("reports.write", "reports:*", _TOKEN_COMMAND, _TOKEN_ADMIN_ALL)
_NOTIFY_READ = ("notify.read", "notify:*", _TOKEN_READ, _TOKEN_ADMIN_ALL)
_NOTIFY_DISPATCH = ("notify:dispatch", "notify:*", _TOKEN_COMMAND, _TOKEN_ADMIN_ALL)
# Reads the HTTP surface leaves to its generic read family.
_GENERIC_READ = (_TOKEN_READ, _TOKEN_ADMIN_ALL)
# /api/model-selection/options is a registry read route.
_REGISTRY_READ = (_TOKEN_REGISTRY_READ, _TOKEN_REGISTRY_ALL, _TOKEN_ADMIN_ALL)

#: tool name -> any-of scopes. Every registered tool must appear here; the
#: guardrail test in tests/unit/test_mcp_tool_scopes.py fails on any omission,
#: so a new tool cannot ship without an explicit decision about its scope.
MCP_TOOL_SCOPES: dict[str, tuple[str, ...]] = {
    "catalog_search": ADMIN,
    "extension_catalog_install": ADMIN,
    # --- jobs (read) -------------------------------------------------------
    "job_approval": _JOBS_READ,
    "job_approvals": _JOBS_READ,
    "job_usage_breakdown": _JOBS_READ,
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
    "workflow_run_report": _JOBS_READ,
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
    "project_connections": _PROJECTS_READ,
    "project_create": _PROJECTS_WRITE,
    "project_update": _PROJECTS_WRITE,
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
    # --- agents and skills: /api/agents* and /api/skills* are admin-only, so a
    # token needs admin:all for reads as well; the schemas are generic reads ---
    "agent_authoring_schema": _GENERIC_READ,
    "skill_authoring_schema": _GENERIC_READ,
    "agent_list": ADMIN,
    "agent_get": ADMIN,
    "agent_create": ADMIN,
    "agent_update": ADMIN,
    "agent_validate": ADMIN,
    "agent_set_enabled": ADMIN,
    "agent_skill_assign": ADMIN,
    "agent_skill_unassign": ADMIN,
    "agent_skill_set_instructions": ADMIN,
    "skill_list": ADMIN,
    "skill_get": ADMIN,
    "skill_create": ADMIN,
    "skill_update": ADMIN,
    "skill_inputs_set": ADMIN,
    "skill_outputs_set": ADMIN,
    "skill_validate": ADMIN,
    "skill_set_enabled": ADMIN,
    # Enabling a workflow is a registry write, like workflow_apply_changes.
    "workflow_set_enabled": ADMIN,
    "model_options": _REGISTRY_READ,
    # --- reports -----------------------------------------------------------
    "report_get": _REPORTS_READ,
    "report_generate": _REPORTS_WRITE,
    # --- notifications -----------------------------------------------------
    "project_notification_deliveries": _NOTIFY_READ,
    "project_notification_routes": _NOTIFY_READ,
    "dispatch_project_notification": _NOTIFY_DISPATCH,
}


#: tool name -> the ADR 0076 permission it needs: the route table's key for the API
#: route the tool proxies (``MCP_TOOL_ROUTES``). Every tool appears here.
MCP_TOOL_PERMISSIONS: dict[str, str] = {
    # --- public catalogues: the admin's (the former admin-only routes) -----------
    "catalog_search": "registry.manage",
    "extension_catalog_install": "extensions.manage",
    # --- jobs ----------------------------------------------------------------
    "job_approval": "jobs.read",
    "job_approvals": "jobs.read",
    "job_usage_breakdown": "jobs.read",
    "job_decision_overrides": "jobs.read",
    "job_errors": "jobs.read",
    "job_events": "jobs.read",
    "job_incident": "jobs.read",
    "job_logs": "jobs.read",
    "job_messages": "jobs.read",
    "job_list": "jobs.read",
    "job_approvals_pending": "jobs.read",
    "job_result": "jobs.read",
    "job_runtime_journal": "jobs.read",
    "job_status": "jobs.read",
    "workflow_run_report": "jobs.read",
    "job_trace": "jobs.read",
    "project_jobs": "jobs.read",
    "job_acknowledge": "jobs.write",
    "job_unacknowledge": "jobs.write",
    "job_cancel": "jobs.write",
    "job_restart": "jobs.write",
    "job_approval_decide": "approvals.decide",
    # --- projects --------------------------------------------------------------
    "project_list": "projects.read",
    "project_connections": "projects.read",
    "project_create": "projects.write",
    "project_update": "projects.write",
    # --- workflow authoring ------------------------------------------------------
    "capability_search": "workflows.read",
    "capability_describe": "workflows.read",
    "workflow_examples_search": "workflows.read",
    "workflow_authoring_schema": "workflows.read",
    "workflow_draft_validate": "workflows.read",
    "workflow_draft_save": "workflows.manage",
    "workflow_definition": "registry.read",
    "workflow_apply_changes": "workflows.manage",
    "workflow_convert_group": "workflows.manage",
    # --- workflows ---------------------------------------------------------------
    "workflow_contract": "workflows.read",
    "workflow_list": "workflows.read",
    "workflow_suggestions": "workflows.read",
    "workflow_validate": "workflows.read",
    "workflow_trigger": "workflows.run",
    # --- hooks, portability: the project's workflow configuration ----------------
    "workflow_hooks_list": "workflows.manage",
    "workflow_hook_get": "workflows.manage",
    "workflow_hook_preview": "workflows.manage",
    "workflow_hook_bind": "workflows.manage",
    "workflow_hook_unbind": "workflows.manage",
    "workflow_hook_create_and_bind": "workflows.manage",
    "workflow_where_used": "workflows.manage",
    "workflow_export": "workflows.manage",
    "workflow_stats": "workflows.manage",
    "workflow_import": "workflows.manage",
    "workflow_set_enabled": "workflows.manage",
    # --- agents and skills -------------------------------------------------------
    "agent_authoring_schema": "platform.read",
    "skill_authoring_schema": "platform.read",
    "agent_list": "registry.manage",
    "agent_get": "registry.manage",
    "agent_create": "registry.manage",
    "agent_update": "registry.manage",
    "agent_validate": "registry.manage",
    "agent_set_enabled": "registry.manage",
    "agent_skill_assign": "registry.manage",
    "agent_skill_unassign": "registry.manage",
    "agent_skill_set_instructions": "registry.manage",
    "skill_list": "registry.manage",
    "skill_get": "registry.manage",
    "skill_create": "registry.manage",
    "skill_update": "registry.manage",
    "skill_inputs_set": "registry.manage",
    "skill_outputs_set": "registry.manage",
    "skill_validate": "registry.manage",
    "skill_set_enabled": "registry.manage",
    "model_options": "registry.read",
    # --- reports -----------------------------------------------------------------
    "report_get": "reports.read",
    "report_generate": "reports.write",
    # --- notifications -------------------------------------------------------------
    "project_notification_deliveries": "notify.read",
    "project_notification_routes": "notify.read",
    "dispatch_project_notification": "notify.dispatch",
}


def required_permission_for_tool(tool: str) -> str:
    """The permission *tool* needs; an unknown tool needs one no role is given by default.

    ``platform.manage`` is the admin's: an unmapped tool is a wiring mistake and the
    safe reading is the most restrictive one, as for its scopes.
    """
    return MCP_TOOL_PERMISSIONS.get(str(tool or "").strip(), "platform.manage")


def tool_allowed_for_permissions(tool: str, permissions: frozenset[str] | set[str]) -> bool:
    """Whether a holder of ``permissions`` may use *tool* (the API route decides as well)."""
    return required_permission_for_tool(tool) in permissions


def required_scopes_for_tool(tool: str) -> tuple[str, ...]:
    """Return the any-of scopes for *tool*.

    Unknown tools fail closed on `admin:all`: an unmapped tool is a wiring
    mistake, and the safe reading of "we do not know what this needs" is the
    most restrictive one, not the least.
    """
    return MCP_TOOL_SCOPES.get(str(tool or "").strip(), ADMIN)


__all__ = [
    "MCP_TOOL_PERMISSIONS",
    "MCP_TOOL_SCOPES",
    "required_permission_for_tool",
    "required_scopes_for_tool",
    "tool_allowed_for_permissions",
]
