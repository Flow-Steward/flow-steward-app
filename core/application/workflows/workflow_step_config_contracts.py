"""Application-owned strict configuration keys for canonical workflow steps."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .workflow_step_kinds import (
    STEP_KIND_AGENT,
    STEP_KIND_APPROVAL,
    STEP_KIND_BATCH,
    STEP_KIND_BREAK,
    STEP_KIND_COLLECT,
    STEP_KIND_CONNECTOR,
    STEP_KIND_CONTINUE,
    STEP_KIND_DATASET_QUERY,
    STEP_KIND_FILE_FROM_BASE64,
    STEP_KIND_HTTP_REQUEST,
    STEP_KIND_IF,
    STEP_KIND_INVOKE_WORKFLOW,
    STEP_KIND_SET_VARIABLE,
    STEP_KIND_STOP_FAIL,
    STEP_KIND_STOP_SUCCESS,
    STEP_KIND_SWITCH,
    STEP_KIND_TABULAR_FEED,
    STEP_KIND_TEMPLATE,
    STEP_KIND_TRANSFORM,
    STEP_KIND_WAIT,
)

#: Top-level tabular_feed config keys. The workflow contract owns them so the
#: control process can validate a definition without the tabular_feed package.
TABULAR_FEED_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "operation",
        "input",
        "result_target",
        "options",
        "column_map",
        "key_column",
        "rules",
        "actions",
        "relational",
        "checks",
        "rows",
        "schema",
        "format",
        "profile_key",
        "snapshot_artifact_id",
        "compare_columns",
        "baseline_dataset_handle",
        "expected_snapshot_ref",
        "snapshot_mode",
        "raw_sha256",
        "enrichment_invalidate_on",
        "filter",
        "columns",
        "offset",
        "limit",
        "allow_new_columns",
        "append_to",
    }
)


# Selectable successful result members, not a response schema or presence guarantee.
# Snapshot revision/ref bookkeeping stays internal to the publication contract.
TABULAR_FEED_PICKER_OUTPUT_FIELDS_BY_OPERATION: dict[str, tuple[tuple[str, str], ...]] = {
    "checksum": (
        ("unchanged", "boolean"),
        ("committed_snapshot_artifact_id", "artifact_handle"),
    ),
    "read": (("dataset_handle", "dataset_handle"),),
    "create_dataset": (("dataset_handle", "dataset_handle"),),
    "transform": (("dataset_handle", "dataset_handle"),),
    "concat": (("dataset_handle", "dataset_handle"),),
    "merge_rows": (("dataset_handle", "dataset_handle"),),
    "diff": (
        ("diff_dataset_handle", "dataset_handle"),
        ("snapshot_artifact_id", "artifact_handle"),
    ),
    "read_rows": (("rows", "array"),),
    "write": (("artifact_handle", "artifact_handle"), ("sha256", "string")),
}

STEP_CONFIG_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    STEP_KIND_AGENT: frozenset(
        {"input_mapping", "input_mapping_jsonb", "output_mapping", "output_mapping_jsonb"}
    ),
    STEP_KIND_CONNECTOR: frozenset(
        {
            "action_id",
            "connection_binding",
            "connection_binding_jsonb",
            "extension_id",
            "function_name",
            "input_mapping",
            "input_mapping_jsonb",
            "operation_id",
            "output_mapping",
            "output_mapping_jsonb",
        }
    ),
    STEP_KIND_INVOKE_WORKFLOW: frozenset(
        {
            "artifact_mapping",
            # A hook: the author's declaration; the target is a project binding.
            "hook",
            "input_mapping",
            "output_mapping",
            "workflow_id",
        }
    ),
    # Branch targets are execution_graph edges, so if/switch keep only conditions.
    STEP_KIND_IF: frozenset({"condition"}),
    STEP_KIND_SWITCH: frozenset({"cases", "selector"}),
    STEP_KIND_APPROVAL: frozenset(
        {"message", "output_mapping", "response_mapping", "show_outputs"}
    ),
    STEP_KIND_WAIT: frozenset({"duration_ms", "input_mapping"}),
    STEP_KIND_STOP_SUCCESS: frozenset({"message", "reason"}),
    STEP_KIND_STOP_FAIL: frozenset({"message", "reason"}),
    STEP_KIND_SET_VARIABLE: frozenset({"assignments", "output_types", "target", "value"}),
    # Loop controls of a sequence For each body (ADR 0038).
    STEP_KIND_BREAK: frozenset(),
    STEP_KIND_CONTINUE: frozenset(),
    STEP_KIND_COLLECT: frozenset({"key", "mode", "source", "target"}),
    STEP_KIND_BATCH: frozenset(
        {"batch_size", "emit_partial_batch", "result_target", "source_path", "steps"}
    ),
    STEP_KIND_HTTP_REQUEST: frozenset(
        {
            "artifact_filename",
            "auth_mode",
            "auth_ref",
            "body_template",
            "expected_statuses",
            "headers",
            "max_response_bytes",
            "method",
            "query_params",
            "response_mode",
            "response_target",
            "timeout_seconds",
            "url",
        }
    ),
    STEP_KIND_DATASET_QUERY: frozenset(
        {
            "dataset_id",
            "input_params",
            "params",
            "query_template_id",
            "result_target",
            "row_limit",
            "sql",
            "timeout_seconds",
        }
    ),
    STEP_KIND_TRANSFORM: frozenset(
        {"fields", "input", "mode", "output_mode", "result_target", "row_filter"}
    ),
    STEP_KIND_TABULAR_FEED: TABULAR_FEED_CONFIG_KEYS,
    STEP_KIND_FILE_FROM_BASE64: frozenset(
        {"content_type", "filename", "max_bytes", "result_target", "source"}
    ),
    STEP_KIND_TEMPLATE: frozenset({"format", "inputs", "result_target", "text"}),
}

STEP_OVERRIDE_CROSS_CUTTING_KEYS = frozenset(
    {
        "add_tools",
        "block_key",
        "config",
        "connection_binding",
        "connector_install_id",
        "extra_prompt",
        "published_block_key",
        "remove_tools",
        "require_runtime_tools",
        "returned_files",
    }
)
STEP_OVERRIDE_ALLOWED_KEYS = frozenset(STEP_CONFIG_ALLOWED_KEYS) | STEP_OVERRIDE_CROSS_CUTTING_KEYS


def validate_step_config_keys(
    *,
    step_kind: str,
    override: Any,
    step_path: str,
) -> list[dict[str, str]]:
    """Return exact-path issues for keys the selected runtime never consumes."""
    normalized_step_kind = str(step_kind or "").strip() or STEP_KIND_AGENT
    allowed = STEP_CONFIG_ALLOWED_KEYS.get(normalized_step_kind)
    if allowed is None or not isinstance(override, Mapping):
        return []
    allowed_wrappers = {normalized_step_kind}
    allowed_envelope = STEP_OVERRIDE_CROSS_CUTTING_KEYS | allowed_wrappers
    issues = [
        {
            "code": "workflow.step_override_key_unknown",
            "message": f"Unknown step override key '{key}'",
            "path": f"{step_path}.override.{key}",
            "severity": "error",
        }
        for key in sorted(set(override) - allowed_envelope)
    ]
    for config_kind in sorted(allowed_wrappers):
        config = override.get(config_kind)
        if not isinstance(config, Mapping):
            continue
        config_allowed = STEP_CONFIG_ALLOWED_KEYS[config_kind]
        issues.extend(
            {
                "code": "workflow.step_config_key_unknown",
                "message": f"Unknown {config_kind} config key '{key}'",
                "path": f"{step_path}.override.{config_kind}.{key}",
                "severity": "error",
            }
            for key in sorted(set(config) - config_allowed)
        )
    return issues


def validate_workflow_step_config_keys(
    workflow: Any,
    *,
    workflow_path: str = "workflow",
) -> list[dict[str, str]]:
    if not isinstance(workflow, Mapping):
        return []
    issues: list[dict[str, str]] = []

    def validate_steps(steps: Any, *, steps_path: str) -> None:
        if not isinstance(steps, list):
            return
        for step_index, step in enumerate(steps):
            if isinstance(step, Mapping):
                validate_step(step, step_path=f"{steps_path}[{step_index}]")

    def validate_step(step: Mapping[str, Any], *, step_path: str) -> None:
        step_kind = str(
            step.get("step_kind")
            or step.get("executor_kind")
            or step.get("kind")
            or STEP_KIND_AGENT
        ).strip()
        override = step.get("override")
        issues.extend(
            validate_step_config_keys(
                step_kind=step_kind,
                override=override,
                step_path=step_path,
            )
        )
        if step_kind != STEP_KIND_BATCH or not isinstance(override, Mapping):
            return
        loop_config = override.get(step_kind)
        if isinstance(loop_config, Mapping):
            validate_steps(
                loop_config.get("steps"),
                steps_path=f"{step_path}.override.{step_kind}.steps",
            )

    steps = workflow.get("steps")
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        if isinstance(step, Mapping):
            step_id = str(step.get("step_id") or "").strip() or f"[{index}]"
            validate_step(step, step_path=f"{workflow_path}.steps.{step_id}")
    return issues


__all__ = [
    "STEP_CONFIG_ALLOWED_KEYS",
    "STEP_OVERRIDE_ALLOWED_KEYS",
    "STEP_OVERRIDE_CROSS_CUTTING_KEYS",
    "validate_step_config_keys",
    "validate_workflow_step_config_keys",
]
