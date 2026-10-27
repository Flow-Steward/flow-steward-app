"""Canonical MCP-backed tool manifests that Flow Steward keeps in sync at runtime."""

from __future__ import annotations

import logging
from copy import deepcopy

from core.application.auth.config import DEFAULT_ACCOUNT_ID

logger = logging.getLogger(__name__)

_BUILTIN_FS_CONNECTOR_ID = "builtin_fs_mcp"


def _legacy_kb_write_manifest() -> dict:
    return {
        "tool_id": "kb_write",
        "version": "1.0.0",
        "display_name": "Knowledge Base Write",
        "description": "Persist learnings and delivery knowledge entries.",
        "enabled": True,
        "category": "knowledge",
        "prompt": "Write concise reusable knowledge back to the project KB.",
        "interface": {"functions": []},
        "implementation": {"type": "internal"},
        "config": {},
        "tags": ["bootstrap"],
        "builder_metadata_jsonb": {
            "purpose": "Persist delivery lessons and reusable knowledge after work is complete.",
            "use_cases": [
                "knowledge capture",
                "post-task writeups",
                "retrospective notes",
            ],
            "anti_use_cases": ["source code editing", "planning new work"],
            "composition_role": "knowledge_capture",
            "determinism_level": "deterministic",
            "safe_for_auto_use": False,
        },
    }


def _workflow_ops_manifest() -> dict:
    return {
        "tool_id": "workflow_ops",
        "version": "1.0.0",
        "display_name": "Workflow Ops",
        "description": "Workflow discovery, validation, triggering, result, and control tools.",
        "enabled": True,
        "category": "workflow",
        "prompt": (
            "Use project_list to discover projects, project_jobs to inspect recent activity, workflow_list "
            "to discover workflows in the selected project, "
            "workflow_validate, workflow_contract, and workflow_suggestions to inspect or repair contracts, "
            "ask for all missing required inputs before workflow_trigger(project_name, workflow_id). "
            "Ask for required files and send actual bytes in files as input_name, filename, mime_type, "
            "and content_base64 (strict base64; at most 1 MiB decoded total, 10 files). "
            "Never substitute a filename, URL, local path, or fabricated artifact handle. "
            "Use job_result to fetch the normalized final outcome, "
            "job_acknowledge or job_unacknowledge to manage attention state, job_restart to relaunch a failed "
            "or stalled run, job_cancel to stop active runs, and job_approval_decide to approve or reject paused runs."
        ),
        "interface": {
            "functions": [
                {
                    "name": "project_list",
                    "parameters": [],
                },
                {
                    "name": "project_jobs",
                    "parameters": [
                        {"name": "project_name", "type": "string", "required": True},
                        {"name": "limit", "type": "integer", "required": False, "default": 10},
                    ],
                },
                {
                    "name": "workflow_list",
                    "parameters": [
                        {"name": "project_name", "type": "string", "required": True},
                        {
                            "name": "enabled_only",
                            "type": "boolean",
                            "required": False,
                            "default": True,
                        },
                        {
                            "name": "include_validation",
                            "type": "boolean",
                            "required": False,
                            "default": False,
                        },
                    ],
                },
                {
                    "name": "workflow_validate",
                    "parameters": [
                        {"name": "workflow_id", "type": "string", "required": True},
                        {"name": "project_name", "type": "string", "required": False},
                    ],
                },
                {
                    "name": "workflow_contract",
                    "parameters": [
                        {"name": "workflow_id", "type": "string", "required": True},
                        {"name": "project_name", "type": "string", "required": True},
                    ],
                },
                {
                    "name": "workflow_suggestions",
                    "parameters": [
                        {"name": "workflow_id", "type": "string", "required": True},
                        {"name": "project_name", "type": "string", "required": False},
                    ],
                },
                {
                    "name": "workflow_trigger",
                    "parameters": [
                        {"name": "project_name", "type": "string", "required": True},
                        {"name": "workflow_id", "type": "string", "required": True},
                        {"name": "payload", "type": "object", "required": False},
                        {"name": "files", "type": "array", "required": False},
                        {"name": "client_context", "type": "object", "required": False},
                        {
                            "name": "test_mode",
                            "type": "boolean",
                            "required": False,
                            "default": False,
                        },
                    ],
                },
                {
                    "name": "job_result",
                    "parameters": [
                        {"name": "job_id", "type": "string", "required": True},
                        {
                            "name": "include_messages_limit",
                            "type": "integer",
                            "required": False,
                            "default": 100,
                            "description": "Maximum messages to include (1-2000).",
                        },
                        {
                            "name": "include_logs_limit",
                            "type": "integer",
                            "required": False,
                            "default": 50,
                            "description": "Maximum logs to include (1-2000).",
                        },
                    ],
                },
                {
                    "name": "job_acknowledge",
                    "parameters": [
                        {"name": "job_id", "type": "string", "required": True},
                        {"name": "actor", "type": "string", "required": False},
                    ],
                },
                {
                    "name": "job_unacknowledge",
                    "parameters": [
                        {"name": "job_id", "type": "string", "required": True},
                    ],
                },
                {
                    "name": "job_restart",
                    "parameters": [
                        {"name": "job_id", "type": "string", "required": True},
                    ],
                },
                {
                    "name": "job_cancel",
                    "parameters": [
                        {"name": "job_id", "type": "string", "required": True},
                        {"name": "comment", "type": "string", "required": False},
                    ],
                },
                {
                    "name": "job_approval_decide",
                    "parameters": [
                        {"name": "thread_id", "type": "string", "required": True},
                        {"name": "approved", "type": "boolean", "required": True},
                        {"name": "comment", "type": "string", "required": False},
                        {"name": "action", "type": "string", "required": False},
                    ],
                },
            ],
        },
        "implementation": {
            "type": "mcp",
            "server": "fs",
            "connector_id": _BUILTIN_FS_CONNECTOR_ID,
        },
        "config": {},
        "tags": ["builtin", "mcp", "workflow"],
        "builder_metadata_jsonb": {
            "purpose": "Inspect, validate, trigger, and control workflows through the Flow Steward runtime.",
            "use_cases": [
                "workflow validation",
                "workflow triggering",
                "workflow result inspection",
            ],
            "anti_use_cases": ["free-form reasoning without workflow context"],
            "composition_role": "workflow_orchestration",
            "determinism_level": "hybrid",
            "safe_for_auto_use": False,
        },
    }


def builtin_mcp_tool_manifests() -> dict[str, dict]:
    return {
        "workflow_ops": _workflow_ops_manifest(),
    }


def _tool_payload(existing: dict | None, desired: dict) -> dict:
    existing = existing or {}
    existing_tags = existing.get("tags")
    desired_tags = desired.get("tags", [])
    return {
        "tool_id": desired["tool_id"],
        "version": existing.get("version") or desired.get("version", "1.0.0"),
        "display_name": existing.get("display_name")
        or desired.get("display_name")
        or desired["tool_id"],
        "description": desired.get("description", ""),
        "enabled": existing.get("enabled", desired.get("enabled", True)),
        "category": existing.get("category") or desired.get("category", "general"),
        "prompt": desired.get("prompt", ""),
        "interface": deepcopy(desired.get("interface", {})),
        "implementation": deepcopy(desired.get("implementation", {})),
        "config": deepcopy(
            existing.get("config")
            if existing.get("config") is not None
            else desired.get("config", {})
        ),
        "tags": list(existing_tags if isinstance(existing_tags, list) else desired_tags),
        "builder_metadata_jsonb": deepcopy(desired.get("builder_metadata_jsonb", {})),
    }


def _canonical_tool_payload(payload: dict, *, from_db: bool) -> dict:
    from core.application.registry.tool_manifest import ToolManifest

    manifest = (
        ToolManifest.from_db_row(payload) if from_db else ToolManifest.model_validate(payload)
    )
    return manifest.to_registry_payload()


def _tool_payload_matches(
    existing: dict | None,
    payload: dict,
    *,
    fields: tuple[str, ...],
) -> bool:
    if not existing:
        return False
    try:
        existing_payload = _canonical_tool_payload(existing, from_db=True)
        desired_payload = _canonical_tool_payload(payload, from_db=False)
    except Exception:
        existing_payload = existing
        desired_payload = payload
    return all(existing_payload.get(field) == desired_payload.get(field) for field in fields)


def _has_shipped_system_ownership(existing: dict) -> bool:
    return (
        str(existing.get("scope_kind") or "system").strip() == "system"
        and not str(existing.get("project_id") or "").strip()
        and not str(existing.get("source_install_id") or "").strip()
        and str(existing.get("account_id") or DEFAULT_ACCOUNT_ID).strip() == DEFAULT_ACCOUNT_ID
    )


def _is_shipped_kb_query(existing: dict | None) -> bool:
    if not existing or not _has_shipped_system_ownership(existing):
        return False
    tags = {str(tag or "").strip() for tag in existing.get("tags") or []}
    implementation = existing.get("implementation") or {}
    return (
        "builtin" in tags
        and str(implementation.get("connector_id") or "").strip() == _BUILTIN_FS_CONNECTOR_ID
        and str(implementation.get("type") or implementation.get("kind") or "").strip() == "mcp"
    )


def _is_shipped_kb_write(existing: dict | None) -> bool:
    if not existing or not _has_shipped_system_ownership(existing):
        return False
    baseline = _legacy_kb_write_manifest()
    return _tool_payload_matches(
        existing,
        baseline,
        fields=(
            "tool_id",
            "version",
            "display_name",
            "description",
            "enabled",
            "category",
            "prompt",
            "interface",
            "implementation",
            "config",
            "tags",
            "builder_metadata_jsonb",
        ),
    )


def _retire_removed_builtin_tools(db_store) -> list[str]:
    retired: list[str] = []
    for tool_id, predicate in (
        ("kb_query", _is_shipped_kb_query),
        ("kb_write", _is_shipped_kb_write),
    ):
        try:
            existing = db_store.get_tool(tool_id)
            if not existing:
                continue
            if not predicate(existing):
                logger.warning(
                    "Leaving retired builtin tool id '%s' untouched because its ownership "
                    "or shipped fingerprint no longer matches",
                    tool_id,
                )
                continue
            db_store.delete_tool(tool_id, author="system")
            retired.append(tool_id)
        except Exception as exc:
            logger.warning("Could not retire builtin MCP tool '%s': %s", tool_id, exc)
    return retired


def sync_builtin_mcp_tools(db_store) -> list[str]:
    changed = _retire_removed_builtin_tools(db_store)
    for tool_id, desired in builtin_mcp_tool_manifests().items():
        try:
            existing = db_store.get_tool(tool_id)
            payload = _tool_payload(existing, desired)
            if _tool_payload_matches(
                existing,
                payload,
                fields=(
                    "description",
                    "prompt",
                    "interface",
                    "implementation",
                    "builder_metadata_jsonb",
                ),
            ):
                continue
            db_store.save_tool(payload, author="system")
            changed.append(tool_id)
        except Exception as e:
            logger.warning("Could not sync builtin MCP tool '%s': %s", tool_id, e)
    return changed
