# ruff: noqa: RUF022
"""
RegistryDBStore — PostgreSQL-backed CRUD for registry entities.

Replaces RegistryGitStore.  Every mutation logs a full-snapshot changelog entry.
Uses psycopg_runtime with autocommit (same pattern as the Flow Steward logger).
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.builder_metadata import normalize_builder_metadata
from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.db_pool import (
    PooledConnectionProxy,
    get_threaded_pool,
    pool_bounds,
    pooling_enabled,
    register_transaction_after_commit,
)

try:
    from core.infrastructure.db.psycopg_runtime import extensions as _psycopg_runtime_extensions
except Exception:  # pragma: no cover - exercised in optional-dependency test fallbacks
    _psycopg_runtime_extensions = None

from core.application.registry.prompt_contracts import strip_agent_output_format

A_DELETED_AT_IS_NULL_VALUE = "a.deleted_at IS NULL"
BUILDER_METADATA_JSONB_EXCLUDED_BUILDER_METADATA_SQL = (
    ", builder_metadata_jsonb = EXCLUDED.builder_metadata_jsonb"
)
BUILDER_METADATA_JSONB_S_VALUE = ", %(builder_metadata_jsonb)s"
BUILDER_METADATA_JSONB_VALUE = ", builder_metadata_jsonb"
DELETED_AT_IS_NULL_VALUE = "deleted_at IS NULL"
DELETED_AT_NULL_SQL = "deleted_at = NULL"
DESCRIPTION_EXCLUDED_DESCRIPTION_SQL = "description = EXCLUDED.description"
DISPLAY_NAME_EXCLUDED_DISPLAY_NAME_SQL = "display_name = EXCLUDED.display_name"
ENABLED_EXCLUDED_ENABLED_SQL = "enabled = EXCLUDED.enabled"
TAGS_EXCLUDED_TAGS_SQL = "tags = EXCLUDED.tags"
UNNAMED_RULE_VALUE = "Unnamed Rule"
VERSION_EXCLUDED_VERSION_SQL = "version = EXCLUDED.version"
W_DELETED_AT_IS_NULL_VALUE = "w.deleted_at IS NULL"


logger = logging.getLogger(__name__)
_PG_TX_STATUS_IDLE = int(getattr(_psycopg_runtime_extensions, "TRANSACTION_STATUS_IDLE", 0) or 0)
SQL_DELETED_AT_IS_NULL = DELETED_AT_IS_NULL_VALUE
SQL_AND = " AND "
SQL_ENABLED_TRUE = "enabled = true"
SQL_AGENT_ENABLED_TRUE = "a.enabled = true"
_PUBLIC_CONTRACT_PROJECTION_LOCK_DOMAIN = "platform.public_contract_projection"
_PUBLIC_CONTRACT_PROJECTION_LOCK_KEY = "reconcile"

# Compact lifecycle actions whose snapshots describe what happened to the
# entity, not what the entity contained. They must never be fed back through
# apply_entity_snapshot() — doing so would overwrite a live entity with a
# lifecycle stub, or resurrect a purged one.
LIFECYCLE_CHANGELOG_ACTIONS = frozenset({"trash", "restore", "hard_delete"})
# Delete retains a full snapshot for existing replay policy, but its audit
# event is not a content change. Read presentation must not define replay policy.
LIFECYCLE_PRESENTATION_ACTIONS = LIFECYCLE_CHANGELOG_ACTIONS | frozenset({"delete"})

SUPPORTED_CHANGELOG_ENTITY_TYPES = {
    "agent",
    "tool",
    "skill",
    "workflow",
    "step_kind_profile",
    "guardrail",
    "data_contract",
    "dataset",
    "field",
    "published_block",
}


def _nullable_scope_sql(column: str) -> str:
    return f"NULLIF(BTRIM(COALESCE({column}::text, '')), '') IS NULL"


def _system_registry_account_id(
    data: dict[str, Any],
    *,
    account_id: Any = None,
    project_id: Any = None,
    source_install_id: Any = None,
) -> str | None:
    normalized_account_id = str(
        account_id if account_id is not None else data.get("account_id") or ""
    ).strip()
    if normalized_account_id:
        return normalized_account_id
    normalized_project_id = str(
        project_id if project_id is not None else data.get("project_id") or ""
    ).strip()
    normalized_source_install_id = str(
        source_install_id if source_install_id is not None else data.get("source_install_id") or ""
    ).strip()
    scope_kind = str(data.get("scope_kind") or "system").strip() or "system"
    if scope_kind == "system" and not normalized_project_id and not normalized_source_install_id:
        return DEFAULT_ACCOUNT_ID
    return None


class RegistryCreateConflictError(ValueError):
    """A create-only save found its id already taken, active or in Trash."""

    def __init__(self, kind: str, entity_id: str) -> None:
        super().__init__(f"{kind} '{entity_id}' already exists")
        self.kind = kind
        self.entity_id = entity_id


class RestoreValidationError(ValueError):
    """Raised when a restore callback reports an invalid restored snapshot."""

    def __init__(self, message: str, *, validation: dict | None = None):
        super().__init__(message)
        self.validation = validation or {"valid": False, "errors": [], "warnings": []}


def _workflow_step_kind(step: dict[str, Any]) -> str:
    return str((step or {}).get("step_kind") or "agent").strip().lower() or "agent"


def _workflow_dependency_ref(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"none", "null", "undefined", "n/a", "-"} else text


def _workflow_step_override(step: dict[str, Any]) -> dict[str, Any]:
    override = (step or {}).get("override")
    if isinstance(override, dict):
        return dict(override)
    if isinstance(override, str) and override.strip():
        with suppress(Exception):
            parsed = json.loads(override)
            if isinstance(parsed, dict):
                return dict(parsed)
    return {}


def _workflow_step_child_workflow_id(step: dict[str, Any]) -> str:
    override = _workflow_step_override(step)
    invoke_workflow = override.get("invoke_workflow")
    if isinstance(invoke_workflow, dict):
        override_ref = _workflow_dependency_ref(invoke_workflow.get("workflow_id"))
        if override_ref:
            return override_ref

    top_level_ref = _workflow_dependency_ref(
        (step or {}).get("workflow_id") or (step or {}).get("child_workflow_id")
    )
    if not top_level_ref:
        return ""
    if _workflow_step_kind(step) == "invoke_workflow":
        return top_level_ref
    agent_ref = str((step or {}).get("agent_id") or (step or {}).get("agent") or "").strip()
    if agent_ref == "invoke_workflow":
        return top_level_ref
    if isinstance(invoke_workflow, dict):
        return top_level_ref
    return top_level_ref


def _nested_text_value(payload: Any, *keys: str) -> str:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "").strip()


def _normalize_extension_runtime_account_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return DEFAULT_ACCOUNT_ID
    if normalized == "acc_default":
        return DEFAULT_ACCOUNT_ID
    try:
        return str(uuid.UUID(normalized))
    except Exception:
        return ""


__all__ = [
    "A_DELETED_AT_IS_NULL_VALUE",
    "Any",
    "BUILDER_METADATA_JSONB_EXCLUDED_BUILDER_METADATA_SQL",
    "BUILDER_METADATA_JSONB_S_VALUE",
    "BUILDER_METADATA_JSONB_VALUE",
    "DEFAULT_ACCOUNT_ID",
    "DELETED_AT_IS_NULL_VALUE",
    "DELETED_AT_NULL_SQL",
    "DESCRIPTION_EXCLUDED_DESCRIPTION_SQL",
    "DISPLAY_NAME_EXCLUDED_DISPLAY_NAME_SQL",
    "ENABLED_EXCLUDED_ENABLED_SQL",
    "PooledConnectionProxy",
    "RegistryCreateConflictError",
    "RestoreValidationError",
    "SQL_AGENT_ENABLED_TRUE",
    "LIFECYCLE_CHANGELOG_ACTIONS",
    "LIFECYCLE_PRESENTATION_ACTIONS",
    "SQL_AND",
    "SQL_DELETED_AT_IS_NULL",
    "SQL_ENABLED_TRUE",
    "SUPPORTED_CHANGELOG_ENTITY_TYPES",
    "TAGS_EXCLUDED_TAGS_SQL",
    "UNNAMED_RULE_VALUE",
    "UTC",
    "VERSION_EXCLUDED_VERSION_SQL",
    "W_DELETED_AT_IS_NULL_VALUE",
    "_PG_TX_STATUS_IDLE",
    "_PUBLIC_CONTRACT_PROJECTION_LOCK_DOMAIN",
    "_PUBLIC_CONTRACT_PROJECTION_LOCK_KEY",
    "_nested_text_value",
    "_normalize_extension_runtime_account_id",
    "_nullable_scope_sql",
    "_system_registry_account_id",
    "_workflow_dependency_ref",
    "_workflow_step_child_workflow_id",
    "_workflow_step_kind",
    "_workflow_step_override",
    "annotations",
    "contextmanager",
    "datetime",
    "get_threaded_pool",
    "json",
    "logger",
    "logging",
    "normalize_builder_metadata",
    "pool_bounds",
    "pooling_enabled",
    "psycopg_runtime",
    "register_transaction_after_commit",
    "strip_agent_output_format",
    "suppress",
    "threading",
    "uuid",
]
