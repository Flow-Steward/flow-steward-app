"""What a browser edit session may send as a workflow's authoring content.

The builder keeps its local copy of a workflow in the browser (the edit
session): the author's content and its Undo history. To compare that copy with
the server, or to save it whole after the author chose "keep my version", the
copy travels as *authoring content*: the fields an author owns and nothing
else. Identity, ``enabled``, provenance, availability, canvas notes and the
secret and file authority of inputs stay with the server; content that names
them is refused, never silently ignored.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from core.application.workflows.execution_graph import (
    EXECUTION_GRAPH_SCHEMA_VERSION,
    EXECUTION_GRAPH_SETTING,
)
from core.application.workflows.workflow_definition_changes import WorkflowDefinitionChangeError
from core.application.workflows.workflow_history import (
    CURRENT_SETTINGS_KEYS,
    INPUT_AUTHORITY_KEYS,
    RESTORED_FIELDS,
)

#: The definition collections an authoring content always carries in full.
CONTENT_COLLECTIONS = (("steps", "step_id"), ("groups", "group_id"), ("for_each", "for_each_id"))
#: Every field authoring content may carry: the author's columns, settings
#: (with the routing graph) and the whole definition.
AUTHORING_FIELDS = (*RESTORED_FIELDS, "settings", *(name for name, _key in CONTENT_COLLECTIONS))

FIELD_UNKNOWN = "workflow.edit_session.field_unknown"
FIELD_INVALID = "workflow.edit_session.field_invalid"
SERVICE_OWNED = "workflow.edit_session.service_owned"
AUTHORITY_FIELD = "workflow.edit_session.authority_field"
NODE_IDS_INVALID = "workflow.edit_session.node_ids_invalid"
GRAPH_INVALID = "workflow.edit_session.graph_invalid"


def _issue(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path, "severity": "error"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _input_rows(settings: Mapping[str, Any]) -> list[Any]:
    callable_settings = settings.get("callable")
    if not isinstance(callable_settings, Mapping):
        return []
    rows = callable_settings.get("expected_inputs")
    return list(rows) if isinstance(rows, list) else []


def authoring_content(workflow: Mapping[str, Any]) -> dict[str, Any]:
    """The author's content of a stored or public workflow, as a session compares it.

    Only :data:`AUTHORING_FIELDS`; settings without the keys their own services
    own and without the secret and file authority of inputs.
    """
    content = {key: copy.deepcopy(workflow[key]) for key in AUTHORING_FIELDS if key in workflow}
    settings = dict(content.get("settings") or {})
    for key in CURRENT_SETTINGS_KEYS:
        settings.pop(key, None)
    callable_settings = settings.get("callable")
    if isinstance(callable_settings, Mapping) and isinstance(
        callable_settings.get("expected_inputs"), list
    ):
        settings["callable"] = {
            **callable_settings,
            "expected_inputs": [
                {key: value for key, value in row.items() if key not in INPUT_AUTHORITY_KEYS}
                if isinstance(row, Mapping)
                else row
                for row in callable_settings["expected_inputs"]
            ],
        }
    content["settings"] = settings
    for name, _id_key in CONTENT_COLLECTIONS:
        if not isinstance(content.get(name), list):
            content[name] = []
    return content


def _collection_issues(content: Mapping[str, Any], field: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for name, id_key in CONTENT_COLLECTIONS:
        rows = content.get(name)
        path = f"{field}.{name}"
        if not isinstance(rows, list):
            issues.append(_issue(FIELD_INVALID, f"'{name}' must be the whole list", path))
            continue
        ids = [_text(row.get(id_key)) if isinstance(row, Mapping) else "" for row in rows]
        if not all(ids) or len(set(ids)) != len(ids):
            issues.append(
                _issue(NODE_IDS_INVALID, f"Every entry of '{name}' needs its own {id_key}", path)
            )
    return issues


def _settings_issues(settings: Any, field: str) -> list[dict[str, str]]:
    path = f"{field}.settings"
    if not isinstance(settings, Mapping):
        return [_issue(FIELD_INVALID, "settings must be an object", path)]
    issues = [
        _issue(SERVICE_OWNED, f"'{key}' is kept by its own service", f"{path}.{key}")
        for key in CURRENT_SETTINGS_KEYS
        if key in settings
    ]
    for index, row in enumerate(_input_rows(settings)):
        if not isinstance(row, Mapping):
            continue
        issues.extend(
            _issue(
                AUTHORITY_FIELD,
                f"'{key}' is set only by its own endpoint",
                f"{path}.callable.expected_inputs[{index}].{key}",
            )
            for key in INPUT_AUTHORITY_KEYS
            if key in row
        )
    graph = settings.get(EXECUTION_GRAPH_SETTING)
    if graph is not None and not (
        isinstance(graph, Mapping)
        and graph.get("schema_version") == EXECUTION_GRAPH_SCHEMA_VERSION
        and isinstance(graph.get("edges", []), list)
    ):
        issues.append(
            _issue(
                GRAPH_INVALID,
                "The routing graph is not in a supported format",
                f"{path}.{EXECUTION_GRAPH_SETTING}",
            )
        )
    return issues


def parse_authoring_content(raw: Any, *, field: str = "content") -> dict[str, Any]:
    """A strict copy of *raw* as authoring content, or a refusal naming every problem.

    Unknown, service-owned and authority fields are refused; the collections
    must be complete lists with unique business ids. Values are not judged
    here: the definition writer judges the candidate as a whole.
    """
    if not isinstance(raw, Mapping):
        raise WorkflowDefinitionChangeError(
            "The authoring content must be an object",
            [_issue(FIELD_INVALID, "The authoring content must be an object", field)],
        )
    issues = [
        _issue(FIELD_UNKNOWN, f"'{key}' is not an authoring field", f"{field}.{key}")
        for key in sorted(map(str, raw))
        if key not in AUTHORING_FIELDS
    ]
    issues.extend(_collection_issues(raw, field))
    issues.extend(_settings_issues(raw.get("settings", {}), field))
    if issues:
        raise WorkflowDefinitionChangeError("The authoring content is not acceptable", issues)
    content = copy.deepcopy(dict(raw))
    content.setdefault("settings", {})
    return content


__all__ = [
    "AUTHORING_FIELDS",
    "CONTENT_COLLECTIONS",
    "authoring_content",
    "parse_authoring_content",
]
