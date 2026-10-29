"""Workflow YAML portability, dependency preflight, and protected export policy."""

from __future__ import annotations

from core.application.workflows.workflow_portability_artifact_import import (
    WorkflowPortabilityArtifactImportMixin,
)
from core.application.workflows.workflow_portability_artifacts import (
    WorkflowPortabilityArtifactsMixin,
)
from core.application.workflows.workflow_portability_common import (
    _SENSITIVE_KEY_MARKERS,
    _WORKFLOW_SAVE_FIELDS,
    BLOCKING_STATUSES,
    BUNDLE_KIND,
    BUNDLE_WORKFLOWS_VALUE,
    DEPENDENCY_STATUSES,
    PROTECTED_WORKFLOW_PATCH_FIELDS,
    RESOLUTION_ACTIONS,
    SCHEMA_VERSION,
    TOP_LEVEL_KEYS,
    TRUSTED_WORKFLOW_DEFAULTS,
    TRUSTED_WORKFLOW_METADATA_FIELDS,
    WorkflowPortabilityError,
    _add_dependency,
    _agent_tool_refs,
    _as_dict,
    _as_list,
    _call_db_get,
    _call_db_get_scoped,
    _call_db_list,
    _callable_db_method,
    _connection_refs,
    _dependency_key,
    _dependency_option,
    _explicit_full_export_allowed,
    _extension_owned,
    _first_text,
    _invalid_dependency_ref,
    _is_mock,
    _json_safe,
    _list_runtime_dependencies,
    _lower,
    _missing_dependency_label,
    _normalize_participants,
    _raise_if_raw_mcp_connector_bindings,
    _raw_mcp_connector_issues,
    _redact_sensitive,
    _row_dict,
    _sensitive_key,
    _sorted_options,
    _step_child_workflow_ref,
    _step_invoke_workflow_override,
    _step_kind,
    _step_uses_child_workflow,
    _text,
    _tool_refs_from_override,
    _workflow_groups,
    _workflow_settings,
    _workflow_steps,
)
from core.application.workflows.workflow_portability_definitions import (
    WorkflowPortabilityDefinitionsMixin,
)
from core.application.workflows.workflow_portability_export import WorkflowPortabilityExportMixin
from core.application.workflows.workflow_portability_hook_bindings import (
    WorkflowPortabilityHookBindingsMixin,
)
from core.application.workflows.workflow_portability_import import WorkflowPortabilityImportMixin
from core.application.workflows.workflow_portability_policy import WorkflowPortabilityPolicyMixin
from core.application.workflows.workflow_portability_resolution import (
    WorkflowPortabilityResolutionMixin,
)
from core.application.workflows.workflow_portability_rewrite import WorkflowPortabilityRewriteMixin


class WorkflowPortabilityService(
    WorkflowPortabilityPolicyMixin,
    WorkflowPortabilityExportMixin,
    WorkflowPortabilityImportMixin,
    WorkflowPortabilityDefinitionsMixin,
    WorkflowPortabilityArtifactsMixin,
    WorkflowPortabilityArtifactImportMixin,
    WorkflowPortabilityResolutionMixin,
    WorkflowPortabilityRewriteMixin,
    WorkflowPortabilityHookBindingsMixin,
):
    """Owns workflow YAML export/import and dependency preflight decisions."""


__all__ = [
    "BLOCKING_STATUSES",
    "BUNDLE_KIND",
    "BUNDLE_WORKFLOWS_VALUE",
    "DEPENDENCY_STATUSES",
    "PROTECTED_WORKFLOW_PATCH_FIELDS",
    "RESOLUTION_ACTIONS",
    "SCHEMA_VERSION",
    "TOP_LEVEL_KEYS",
    "TRUSTED_WORKFLOW_DEFAULTS",
    "TRUSTED_WORKFLOW_METADATA_FIELDS",
    "_SENSITIVE_KEY_MARKERS",
    "_WORKFLOW_SAVE_FIELDS",
    "WorkflowPortabilityError",
    "WorkflowPortabilityService",
    "_add_dependency",
    "_agent_tool_refs",
    "_as_dict",
    "_as_list",
    "_call_db_get",
    "_call_db_get_scoped",
    "_call_db_list",
    "_callable_db_method",
    "_connection_refs",
    "_dependency_key",
    "_dependency_option",
    "_explicit_full_export_allowed",
    "_extension_owned",
    "_first_text",
    "_invalid_dependency_ref",
    "_is_mock",
    "_json_safe",
    "_list_runtime_dependencies",
    "_lower",
    "_missing_dependency_label",
    "_normalize_participants",
    "_raise_if_raw_mcp_connector_bindings",
    "_raw_mcp_connector_issues",
    "_redact_sensitive",
    "_row_dict",
    "_sensitive_key",
    "_sorted_options",
    "_step_child_workflow_ref",
    "_step_invoke_workflow_override",
    "_step_kind",
    "_step_uses_child_workflow",
    "_text",
    "_tool_refs_from_override",
    "_workflow_groups",
    "_workflow_settings",
    "_workflow_steps",
]
