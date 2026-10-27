"""Workflow revision history: what a save records and what a restore may take back.

Every save records the stored workflow, read back after the write in the same
transaction, so an entry is the definition the workflow had from that revision
on — never the caller's payload, which a settings-only save sends without its
definition. ``history_schema_version`` marks entries written this way; only they
carry a revision number that can be trusted, because older entries recorded the
number the caller had loaded before the write.

An installed extension's projection anchor does not own its definition, so its
entries reference the projection instead of pretending to hold an empty graph,
and they are never restored (``projection_reference``).

Restore takes back what an author wrote — the definition, its settings and the
descriptive fields — and keeps everything the platform owns current: identity,
``enabled``, provenance and availability, canvas notes, and the secrets and
sample files of the workflow's inputs.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from core.application.workflows.execution_graph import (
    EXECUTION_GRAPH_SCHEMA_VERSION,
    EXECUTION_GRAPH_SETTING,
    Edge,
    NodeRef,
    Outcome,
    edge_identity,
)
from core.application.workflows.workflow_canvas_annotation_contract import ANNOTATIONS_SETTING
from core.application.workflows.workflow_definition_changes import (
    definition_from_workflow,
    removed_format_issues,
)
from core.application.workflows.workflow_history_snapshot import (
    DEFINITION_COLLECTIONS,
    HISTORY_SCHEMA_KEY,
    HISTORY_SCHEMA_VERSION,
    SNAPSHOT_KIND_KEY,
    SNAPSHOT_KIND_LOCAL,
    SNAPSHOT_KIND_PROJECTION,
    WorkflowHistoryComparisonError,
    history_revision,
    history_snapshot,
    holds_definition,
    is_projection_workflow,
    snapshot_kind,
)
from core.application.workflows.workflow_portability_common import TRUSTED_WORKFLOW_DEFAULTS

#: Keys of the phase-based format, refused before anything else reads a snapshot.
LEGACY_DEFINITION_KEYS = ("phases", "_phases")

#: Workflow fields an author owns. Restore takes them from the snapshot; every
#: other column stays current, including any column added later.
RESTORED_FIELDS = (
    "version",
    "display_name",
    "description",
    "tags",
    "builder_metadata_jsonb",
    "trigger",
)
#: Settings their own services own besides the graph. Restore keeps them current.
CURRENT_SETTINGS_KEYS = (ANNOTATIONS_SETTING, "canvas_annotations_revision")
#: Input fields that point at stored credentials and files, never taken from history.
INPUT_AUTHORITY_KEYS = ("default_secret_ref", "default_configured", "sample_file")

#: Fields that are not content: identity, bookkeeping, and what restore keeps current.
_NOT_CONTENT_FIELDS = frozenset(
    {
        "id",
        "workflow_id",
        "workflow_key",
        "account_id",
        "scope_kind",
        "enabled",
        "revision",
        "created_at",
        "updated_at",
        "deleted_at",
        "deleted_by",
        HISTORY_SCHEMA_KEY,
        SNAPSHOT_KIND_KEY,
        *TRUSTED_WORKFLOW_DEFAULTS,
    }
)

RESTORE_LIFECYCLE = "This entry records a lifecycle change, not workflow content."
RESTORE_PROJECTION = (
    "This workflow's definition belongs to an installed extension; "
    "its history references the extension and cannot be restored."
)
RESTORE_INCOMPLETE = (
    "This entry does not record the workflow definition: it was written by a "
    "settings-only save before history kept full snapshots."
)
RESTORE_REMOVED_FORMAT = "This revision uses a removed pre-release definition format."
RESTORE_GRAPH_INVALID = "This revision's routing graph is not in a supported format."


def _text(value: Any) -> str:
    return str(value or "").strip()


def _settings(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    settings = snapshot.get("settings")
    return dict(settings) if isinstance(settings, Mapping) else {}


def snapshot_graph_valid(snapshot: Mapping[str, Any]) -> bool:
    """An absent graph is an empty one; a present graph must be the current schema."""
    graph = _settings(snapshot).get(EXECUTION_GRAPH_SETTING)
    if graph is None:
        return True
    return (
        isinstance(graph, Mapping)
        and graph.get("schema_version") == EXECUTION_GRAPH_SCHEMA_VERSION
        and isinstance(graph.get("edges", []), list)
    )


def restore_unavailable_reason(snapshot: Mapping[str, Any], *, lifecycle: bool) -> str | None:
    """Why this entry cannot be restored under the current policy, or ``None``."""
    if lifecycle:
        return RESTORE_LIFECYCLE
    if snapshot_kind(snapshot) == SNAPSHOT_KIND_PROJECTION:
        return RESTORE_PROJECTION
    if any(key in snapshot for key in LEGACY_DEFINITION_KEYS):
        return RESTORE_REMOVED_FORMAT
    if not holds_definition(snapshot):
        return RESTORE_INCOMPLETE
    if removed_format_issues(snapshot):
        return RESTORE_REMOVED_FORMAT
    if not snapshot_graph_valid(snapshot):
        return RESTORE_GRAPH_INVALID
    return None


def _without_input_authority(settings: dict[str, Any]) -> dict[str, Any]:
    callable_settings = settings.get("callable")
    if not isinstance(callable_settings, Mapping):
        return settings
    rows = callable_settings.get("expected_inputs")
    if not isinstance(rows, list):
        return settings
    cleaned = [
        {key: value for key, value in row.items() if key not in INPUT_AUTHORITY_KEYS}
        if isinstance(row, Mapping)
        else row
        for row in rows
    ]
    return {**settings, "callable": {**callable_settings, "expected_inputs": cleaned}}


def restore_candidate(
    snapshot: Mapping[str, Any], current: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(workflow row, definition) for restoring *snapshot* over *current*.

    The row is *current* with the author's fields and settings of the snapshot;
    the definition is the snapshot's, its graph normalized when absent.
    """
    from core.application.registry.workflow_secret_defaults import preserve_secret_references

    row = copy.deepcopy(dict(current))
    for key in RESTORED_FIELDS:
        if key in snapshot:
            row[key] = copy.deepcopy(snapshot[key])
    current_settings = _settings(current)
    settings = preserve_secret_references(
        _without_input_authority(copy.deepcopy(_settings(snapshot))), current_settings
    )
    for key in CURRENT_SETTINGS_KEYS:
        if key in current_settings:
            settings[key] = copy.deepcopy(current_settings[key])
        else:
            settings.pop(key, None)
    settings.pop(EXECUTION_GRAPH_SETTING, None)
    row["settings"] = settings
    definition = definition_from_workflow(snapshot)
    return row, definition


# ---------------------------------------------------------------------------
# Semantic diff
# ---------------------------------------------------------------------------


def _key(identity: str) -> str:
    return f"[{json.dumps(identity, ensure_ascii=False)}]"


def _flatten(value: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(value, Mapping):
        if not value:
            out[prefix] = {}
            return
        for key in sorted(value, key=str):
            _flatten(value[key], f"{prefix}.{key}", out)
        return
    if isinstance(value, list):
        if not value:
            out[prefix] = []
            return
        for index, item in enumerate(value):
            _flatten(item, f"{prefix}[{index}]", out)
        return
    out[prefix] = value


def _edge_key(validator: Any, edge: Any, index: int) -> str:
    """The edge's whole ``edge_identity`` in its scope, as an unambiguous path key.

    The scope is the source node's, found by the canonical validator's rules,
    so an unfinished graph without an entry still compares edge by edge. The
    identity tuple is serialized as JSON with its nesting, ``None`` and strings
    kept, so a ``:`` or ``/`` inside an id never merges two different edges.
    """
    source = edge.get("source") if isinstance(edge, Mapping) else None
    outcome = edge.get("outcome") if isinstance(edge, Mapping) else None
    if not (
        isinstance(source, Mapping)
        and isinstance(outcome, Mapping)
        and _text(source.get("kind"))
        and _text(source.get("id"))
        and _text(outcome.get("name"))
    ):
        raise WorkflowHistoryComparisonError(
            f"Routing edge {index} has no source or outcome to compare it by"
        )
    node = NodeRef(_text(source.get("kind")), _text(source.get("id")))
    scope = validator.node_scope(node)
    if scope is None:
        raise WorkflowHistoryComparisonError(
            f"Routing edge {index} starts at '{node.id}', which this revision does not define"
        )
    # The target is a compared value, not part of the identity.
    probe = Edge(
        source=node,
        outcome=Outcome(_text(outcome.get("name")), _text(outcome.get("case_id")) or None),
        target=node,
    )
    return json.dumps(edge_identity(scope, probe), ensure_ascii=False, separators=(",", ":"))


def _graph_view(definition: Mapping[str, Any], view: dict[str, Any]) -> None:
    from core.application.workflows.workflow_definition_validation import _Validator

    graph = definition.get("execution_graph")
    if not isinstance(graph, Mapping):
        return
    graph_path = f"settings.{EXECUTION_GRAPH_SETTING}"
    _flatten(graph.get("entry"), f"{graph_path}.entry", view)
    edges = graph.get("edges") or []
    if not isinstance(edges, list):
        raise WorkflowHistoryComparisonError("The routing graph's edges are not a list")
    validator = _Validator(definition)
    seen: set[str] = set()
    for index, edge in enumerate(edges):
        key = _edge_key(validator, edge, index)
        if key in seen:
            raise WorkflowHistoryComparisonError(f"Two routing edges share the identity {key}")
        seen.add(key)
        _flatten(dict(edge), f"{graph_path}.edges{_key(key)}", view)


def _content_view(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    view: dict[str, Any] = {}
    for key in sorted(snapshot, key=str):
        if key in _NOT_CONTENT_FIELDS or key == "settings" or key in LEGACY_DEFINITION_KEYS:
            continue
        if key in {collection for collection, _id_key in DEFINITION_COLLECTIONS}:
            continue
        _flatten(snapshot[key], key, view)
    settings = _without_input_authority(_settings(snapshot))
    settings.pop(EXECUTION_GRAPH_SETTING, None)
    for key in CURRENT_SETTINGS_KEYS:
        settings.pop(key, None)
    for key in sorted(settings, key=str):
        _flatten(settings[key], f"settings.{key}", view)
    definition = definition_from_workflow(snapshot)
    _graph_view(definition, view)
    orders: dict[str, list[str]] = {}
    for collection, id_key in DEFINITION_COLLECTIONS:
        order: list[str] = []
        for row in definition[collection]:
            identity = _text(row.get(id_key))
            order.append(identity)
            content = {key: value for key, value in row.items() if key != id_key}
            # A node with no content still exists, so adding it is still a change.
            _flatten(content, f"{collection}{_key(identity)}", view)
        orders[collection] = order
    return view, orders


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)
    return value


def diff_workflow_snapshots(
    older: Mapping[str, Any], newer: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Changes between two definition snapshots, nodes matched by their business ids.

    Inserting a node is one addition, not a change of every node after it. A
    reorder of nodes both sides have is one ``<collection>.order`` change.
    """
    old_view, old_orders = _content_view(older)
    new_view, new_orders = _content_view(newer)
    changes: list[dict[str, Any]] = []
    for path in sorted(set(old_view) | set(new_view)):
        in_old, in_new = path in old_view, path in new_view
        old_value, new_value = _jsonable(old_view.get(path)), _jsonable(new_view.get(path))
        if in_old != in_new and (old_value if in_old else new_value) in (None, "", {}, []):
            continue  # an unset field of an added or removed node is not a change
        if in_old and not in_new:
            changes.append({"path": path, "change": "removed", "from": old_value, "to": None})
        elif in_new and not in_old:
            changes.append({"path": path, "change": "added", "from": None, "to": new_value})
        elif old_value != new_value:
            changes.append({"path": path, "change": "changed", "from": old_value, "to": new_value})
    for collection, _id_key in DEFINITION_COLLECTIONS:
        common = set(old_orders[collection]) & set(new_orders[collection])
        old_order = [item for item in old_orders[collection] if item in common]
        new_order = [item for item in new_orders[collection] if item in common]
        if old_order != new_order:
            changes.append(
                {
                    "path": f"{collection}.order",
                    "change": "changed",
                    "from": old_order,
                    "to": new_order,
                }
            )
    return changes


def comparison_unavailable_reason(snapshot: Mapping[str, Any]) -> str | None:
    """Why *snapshot* cannot be compared as a definition, or ``None``."""
    if snapshot_kind(snapshot) == SNAPSHOT_KIND_PROJECTION:
        return RESTORE_PROJECTION
    if not holds_definition(snapshot):
        return RESTORE_INCOMPLETE
    return None


__all__ = [
    "CURRENT_SETTINGS_KEYS",
    "DEFINITION_COLLECTIONS",
    "HISTORY_SCHEMA_KEY",
    "HISTORY_SCHEMA_VERSION",
    "INPUT_AUTHORITY_KEYS",
    "LEGACY_DEFINITION_KEYS",
    "RESTORED_FIELDS",
    "SNAPSHOT_KIND_KEY",
    "SNAPSHOT_KIND_LOCAL",
    "SNAPSHOT_KIND_PROJECTION",
    "WorkflowHistoryComparisonError",
    "comparison_unavailable_reason",
    "diff_workflow_snapshots",
    "history_revision",
    "history_snapshot",
    "holds_definition",
    "is_projection_workflow",
    "restore_candidate",
    "restore_unavailable_reason",
    "snapshot_graph_valid",
    "snapshot_kind",
]
