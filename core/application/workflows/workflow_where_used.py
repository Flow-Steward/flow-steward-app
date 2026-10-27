"""Where a workflow is used: direct Invokes and project hook bindings (spec §12).

One reverse-dependency read for every consumer of a reusable child — the
builder, API, MCP, CLI and the save guards. Direct Invokes come from the
owner-maintained ``workflow_runtime_dependencies`` rows; hook consumers come
from the active binding revisions, which are the hook half of the same model
(ADR 0038). Both are read inside one account only, so a consumer of another
account is never named.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from core.application.extensions.display_labels import localized_extension_display_name
from core.application.registry.projected_artifact_rows import (
    _runtime_store,
    projected_artifact_rows,
)
from core.application.registry.workflow_lookup import is_projection_anchor
from core.infrastructure.db.psycopg_runtime import OperationalError

logger = logging.getLogger(__name__)

KIND_INVOKE = "invoke"
KIND_HOOK = "hook"


@dataclass(frozen=True)
class WorkflowConsumer:
    kind: str
    caller_workflow_id: str
    step_id: str = ""
    caller_enabled: bool = True
    project_id: str | None = None
    hook_id: str | None = None
    source_install_id: str = ""
    local_workflow_id: str = ""
    binding_revision: int | None = None

    def dump(self) -> dict[str, Any]:
        return asdict(self)


def _value(row: Any, key: str, index: int) -> Any:
    return row[key] if isinstance(row, Mapping) else row[index]


def workflow_consumers(cur: Any, *, account_id: Any, workflow_id: str) -> list[WorkflowConsumer]:
    """Every direct Invoke step and active hook binding that calls *workflow_id*."""
    target = str(workflow_id or "").strip()
    account = str(account_id or "").strip()
    if not target or not account:
        return []
    consumers: list[WorkflowConsumer] = []
    cur.execute(
        """SELECT d.workflow_id, d.step_id, COALESCE(w.enabled, false) AS enabled
             FROM workflow_runtime_dependencies d
             JOIN registry_workflows w ON w.workflow_id = d.workflow_id
            WHERE d.dependency_kind = 'child_workflow'
              AND d.dependency_ref = %s
              AND d.account_id::text = %s
              AND w.deleted_at IS NULL
            ORDER BY d.workflow_id, d.step_id""",
        (target, account),
    )
    for row in cur.fetchall() or []:
        consumers.append(
            WorkflowConsumer(
                kind=KIND_INVOKE,
                caller_workflow_id=str(_value(row, "workflow_id", 0)),
                step_id=str(_value(row, "step_id", 1) or ""),
                caller_enabled=bool(_value(row, "enabled", 2)),
            )
        )
    cur.execute(
        """SELECT COALESCE(w.workflow_id, b.local_workflow_id) AS caller_workflow_id,
                  COALESCE(w.enabled, false) AS enabled,
                  b.project_id::text AS project_id, b.hook_id, b.source_install_id,
                  b.local_workflow_id, b.revision
             FROM registry_workflow_hook_bindings b
             JOIN registry_workflow_hook_binding_revisions r ON r.id = b.active_revision_id
             LEFT JOIN registry_workflows w
               ON w.account_id = b.account_id
              AND w.deleted_at IS NULL
              AND ((b.source_install_id = '' AND w.workflow_id = b.local_workflow_id)
                   OR (b.source_install_id <> '' AND w.source_install_id = b.source_install_id
                       AND w.local_workflow_id = b.local_workflow_id))
            WHERE b.account_id::text = %s AND r.target_workflow_id = %s
            ORDER BY b.project_id, b.local_workflow_id, b.hook_id""",
        (account, target),
    )
    for row in cur.fetchall() or []:
        consumers.append(
            WorkflowConsumer(
                kind=KIND_HOOK,
                caller_workflow_id=str(_value(row, "caller_workflow_id", 0)),
                caller_enabled=bool(_value(row, "enabled", 1)),
                project_id=str(_value(row, "project_id", 2)),
                hook_id=str(_value(row, "hook_id", 3)),
                source_install_id=str(_value(row, "source_install_id", 4) or ""),
                local_workflow_id=str(_value(row, "local_workflow_id", 5)),
                binding_revision=int(_value(row, "revision", 6)),
            )
        )
    return consumers


def workflow_usage_view(
    db_store: Any,
    *,
    account_id: Any,
    workflow_id: str,
    caller_visible: Callable[[WorkflowConsumer], bool],
    locale: str = "",
) -> dict[str, Any]:
    """Add scoped presentation metadata; dependency guards keep their original IDs."""
    account = str(account_id or "").strip()
    with db_store._transaction() as cur:
        consumers = workflow_consumers(cur, account_id=account, workflow_id=workflow_id)
    rows: dict[str, dict[str, Any] | None] = {}
    projections: dict[str, dict[str, Any]] | None = None
    releases: dict[str, Any] = {}
    runtime = _runtime_store(db_store)

    def workflow_row(candidate_id: str) -> dict[str, Any] | None:
        nonlocal projections
        if candidate_id not in rows:
            getter = getattr(db_store, "get_workflow", None)
            row = getter(candidate_id) if account and callable(getter) else None
            # The platform getter is unscoped. A global projection anchor is only
            # an identity; its presentation still comes from this account's active
            # projection. Explicit foreign owners and unowned manual rows stay denied.
            row_account = str(row.get("account_id") or "") if isinstance(row, dict) else ""
            if (
                isinstance(row, dict)
                and row_account != account
                and (bool(row_account) or not is_projection_anchor(row))
            ):
                rows[candidate_id] = None
            elif account and (not isinstance(row, dict) or is_projection_anchor(row)):
                if projections is None:
                    projections = {
                        str(item.get("workflow_id") or ""): item
                        for item in projected_artifact_rows(
                            db_store, "workflow", "workflow_id", account_id=account
                        )
                        if str(item.get("account_id") or "") == account
                    }
                rows[candidate_id] = projections.get(candidate_id)
            else:
                rows[candidate_id] = row if isinstance(row, dict) else None
        return rows[candidate_id]

    def name(row: dict[str, Any] | None) -> str | None:
        return str((row or {}).get("display_name") or "").strip() or None

    def presentation(consumer: WorkflowConsumer) -> dict[str, Any]:
        metadata: dict[str, Any] = dict.fromkeys(
            (
                "caller_display_name",
                "caller_extension_id",
                "caller_extension_name",
                "caller_extension_version",
                "hook_display_name",
                "step_display_name",
            )
        )
        if not caller_visible(consumer):
            return metadata
        row = workflow_row(consumer.caller_workflow_id)
        if row is None:
            return metadata
        metadata["caller_display_name"] = name(row)
        release_id = str(row.get("source_release_id") or "").strip()
        if row.get("source_install_id") and release_id and runtime is not None:
            if release_id not in releases:
                try:
                    releases[release_id] = runtime.get_release(release_id)
                except (OperationalError, LookupError):
                    logger.warning(
                        "Workflow usage release metadata unavailable",
                        extra={"release_id": release_id},
                    )
                    releases[release_id] = None
            release = releases[release_id]
            if release is not None:
                metadata.update(
                    caller_extension_id=str(getattr(release, "extension_id", "") or "").strip()
                    or None,
                    caller_extension_name=localized_extension_display_name(
                        getattr(release, "descriptor", None), locale=locale
                    )
                    or None,
                    caller_extension_version=str(
                        getattr(release, "release_version", "")
                        or getattr(release, "version", "")
                        or ""
                    ).strip()
                    or None,
                )
        steps = row.get("steps")
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict):
                continue
            if consumer.kind == KIND_HOOK:
                override = step.get("override")
                invoke = override.get("invoke_workflow") if isinstance(override, dict) else None
                hook = invoke.get("hook") if isinstance(invoke, dict) else None
                if not isinstance(hook, dict) or hook.get("hook_id") != consumer.hook_id:
                    continue
                metadata["hook_display_name"] = name(hook) or name(step)
                metadata["step_id"] = str(step.get("step_id") or "").strip()
            elif str(step.get("step_id") or "") != consumer.step_id:
                continue
            metadata["step_display_name"] = name(step)
            break
        return metadata

    return {
        "workflow_id": workflow_id,
        "workflow_display_name": name(workflow_row(workflow_id)),
        "consumers": [{**consumer.dump(), **presentation(consumer)} for consumer in consumers],
    }


__all__ = [
    "KIND_HOOK",
    "KIND_INVOKE",
    "WorkflowConsumer",
    "workflow_consumers",
    "workflow_usage_view",
]
