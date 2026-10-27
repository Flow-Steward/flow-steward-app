"""What a workflow save records in history: the stored workflow and its revision.

This module is on every save path, including the CLI's seed bootstrap, so it
imports nothing beyond the graph constants and the workflow lookup. Comparing
and restoring entries lives in ``workflow_history``.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from core.application.workflows.execution_graph import EXECUTION_GRAPH_SETTING

HISTORY_SCHEMA_VERSION = 1
HISTORY_SCHEMA_KEY = "history_schema_version"
SNAPSHOT_KIND_KEY = "snapshot_kind"
SNAPSHOT_KIND_LOCAL = "local_definition"
SNAPSHOT_KIND_PROJECTION = "projection_reference"

#: A snapshot holds a definition only when it carries all three collections.
DEFINITION_COLLECTIONS = (("steps", "step_id"), ("groups", "group_id"), ("for_each", "for_each_id"))


class WorkflowHistoryComparisonError(ValueError):
    """Two workflow history entries cannot be compared as definitions."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _settings(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    settings = snapshot.get("settings")
    return dict(settings) if isinstance(settings, Mapping) else {}


def is_projection_workflow(workflow: Mapping[str, Any]) -> bool:
    from core.application.registry.workflow_lookup import is_projection_anchor

    return is_projection_anchor(dict(workflow))


def history_snapshot(workflow: Mapping[str, Any]) -> dict[str, Any]:
    """The history entry of a workflow as it is stored after a write."""
    snapshot = copy.deepcopy(dict(workflow))
    snapshot[HISTORY_SCHEMA_KEY] = HISTORY_SCHEMA_VERSION
    if is_projection_workflow(snapshot):
        for collection, _id_key in DEFINITION_COLLECTIONS:
            snapshot.pop(collection, None)
        settings = _settings(snapshot)
        settings.pop(EXECUTION_GRAPH_SETTING, None)
        snapshot["settings"] = settings
        snapshot[SNAPSHOT_KIND_KEY] = SNAPSHOT_KIND_PROJECTION
    else:
        snapshot[SNAPSHOT_KIND_KEY] = SNAPSHOT_KIND_LOCAL
    return snapshot


def history_revision(snapshot: Mapping[str, Any]) -> int | None:
    """The revision an entry records, or ``None`` when the number cannot be trusted."""
    if snapshot.get(HISTORY_SCHEMA_KEY) != HISTORY_SCHEMA_VERSION:
        return None
    raw = snapshot.get("revision")
    if raw is None:
        return None
    try:
        revision = int(raw)
    except (TypeError, ValueError):
        return None
    return revision if revision >= 1 else None


def snapshot_kind(snapshot: Mapping[str, Any]) -> str:
    kind = _text(snapshot.get(SNAPSHOT_KIND_KEY))
    return kind or SNAPSHOT_KIND_LOCAL


def holds_definition(snapshot: Mapping[str, Any]) -> bool:
    return all(isinstance(snapshot.get(key), list) for key, _id_key in DEFINITION_COLLECTIONS)


__all__ = [
    "DEFINITION_COLLECTIONS",
    "HISTORY_SCHEMA_KEY",
    "HISTORY_SCHEMA_VERSION",
    "SNAPSHOT_KIND_KEY",
    "SNAPSHOT_KIND_LOCAL",
    "SNAPSHOT_KIND_PROJECTION",
    "WorkflowHistoryComparisonError",
    "history_revision",
    "history_snapshot",
    "holds_definition",
    "is_projection_workflow",
    "snapshot_kind",
]
