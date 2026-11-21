"""MCP tool annotations (``readOnlyHint`` and friends) for every Flow Steward tool.

AI clients use these hints to decide how to present a tool and when to ask the
person first: a read is shown as safe, a destructive change gets a stronger
confirmation. Before 2026-11-17 no tool carried any, so clients treated all of
them as unknown writes. The hints describe the tool, never authorize it: the
application API still enforces role, scope and policy on every call.

Every tool in ``MCP_TOOL_SCOPES`` is classified here explicitly; the guardrail
test fails on an omission, as it does for scopes.
"""

from __future__ import annotations

from typing import Any

#: Tools that only read. A dry-run preview or validation counts as a read.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "agent_authoring_schema",
        "agent_get",
        "agent_list",
        "agent_validate",
        "capability_describe",
        "capability_search",
        "catalog_search",
        "job_approval",
        "job_approvals",
        "job_approvals_pending",
        "job_decision_overrides",
        "job_errors",
        "job_events",
        "job_incident",
        "job_list",
        "job_logs",
        "job_messages",
        "job_result",
        "job_runtime_journal",
        "job_status",
        "job_trace",
        "job_usage_breakdown",
        "model_options",
        "project_connections",
        "project_jobs",
        "project_list",
        "project_notification_deliveries",
        "project_notification_routes",
        "report_get",
        "skill_authoring_schema",
        "skill_get",
        "skill_list",
        "skill_validate",
        "workflow_authoring_schema",
        "workflow_contract",
        "workflow_definition",
        "workflow_draft_validate",
        "workflow_examples_search",
        "workflow_export",
        "workflow_hook_get",
        "workflow_hook_preview",
        "workflow_hooks_list",
        "workflow_list",
        "workflow_run_report",
        "workflow_stats",
        "workflow_suggestions",
        "workflow_validate",
        "workflow_where_used",
    }
)

#: Writes that change or remove what exists, or release side effects that cannot
#: be taken back (a run, an approval). Everything else that writes only adds.
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {
        "agent_set_enabled",
        "agent_skill_set_instructions",
        "agent_skill_unassign",
        "agent_update",
        "extension_catalog_install",
        "job_approval_decide",
        "job_cancel",
        "job_restart",
        "skill_inputs_set",
        "skill_outputs_set",
        "skill_set_enabled",
        "skill_update",
        "workflow_apply_changes",
        "workflow_convert_group",
        "workflow_hook_bind",
        "workflow_hook_unbind",
        "workflow_import",
        "workflow_set_enabled",
        "workflow_trigger",
    }
)

#: Writes that are additive only.
ADDITIVE_TOOLS: frozenset[str] = frozenset(
    {
        "agent_create",
        "agent_skill_assign",
        "dispatch_project_notification",
        "job_acknowledge",
        "job_unacknowledge",
        "report_generate",
        "skill_create",
        "workflow_draft_save",
        "workflow_hook_create_and_bind",
    }
)

#: Writes whose repetition with the same arguments changes nothing more.
IDEMPOTENT_TOOLS: frozenset[str] = frozenset(
    {
        "agent_set_enabled",
        "agent_skill_set_instructions",
        "job_acknowledge",
        "job_unacknowledge",
        "skill_inputs_set",
        "skill_outputs_set",
        "skill_set_enabled",
        "workflow_set_enabled",
    }
)

#: Tools that can reach systems outside Flow Steward: a run executes workflow
#: steps (connectors, HTTP, email), and a notification is delivered elsewhere.
OPEN_WORLD_TOOLS: frozenset[str] = frozenset(
    {
        "dispatch_project_notification",
        "extension_catalog_install",
        "job_approval_decide",
        "job_restart",
        "workflow_trigger",
    }
)


def tool_annotations(name: str) -> dict[str, Any]:
    """MCP ``ToolAnnotations`` fields for *name*; an unknown tool is a cautious write."""
    tool = str(name or "").strip()
    if tool in READ_ONLY_TOOLS:
        return {"readOnlyHint": True, "openWorldHint": False}
    return {
        "readOnlyHint": False,
        "destructiveHint": tool not in ADDITIVE_TOOLS,
        "idempotentHint": tool in IDEMPOTENT_TOOLS,
        "openWorldHint": tool in OPEN_WORLD_TOOLS or tool not in DESTRUCTIVE_TOOLS | ADDITIVE_TOOLS,
    }


__all__ = [
    "ADDITIVE_TOOLS",
    "DESTRUCTIVE_TOOLS",
    "IDEMPOTENT_TOOLS",
    "OPEN_WORLD_TOOLS",
    "READ_ONLY_TOOLS",
    "tool_annotations",
]
