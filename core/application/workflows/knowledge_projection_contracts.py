from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.application.workflows.knowledge_projection_common import _as_list, _mapping, _text


def _raw_groups_and_steps(
    workflow: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The workflow's real loop/parallel groups and its own steps, in display order."""
    groups = [
        dict(group)
        for group in _as_list(workflow.get("groups"))
        if isinstance(group, Mapping) and _text(group.get("group_id"))
    ]
    steps = [
        dict(step)
        for step in _as_list(workflow.get("steps"))
        if isinstance(step, Mapping) and _text(step.get("id") or step.get("step_id"))
    ]
    return groups, steps


def _connector_id(step: Mapping[str, Any]) -> str:
    override = _mapping(step.get("override"))
    connector = _mapping(override.get("connector"))
    return _text(connector.get("connector_id") or step.get("connector_id"))


def _child_workflow_id(step: Mapping[str, Any]) -> str:
    override = _mapping(step.get("override"))
    invoke_spec = _mapping(override.get("invoke_workflow"))
    return _text(invoke_spec.get("workflow_id") or step.get("workflow_id"))


def _connector_operation_id(step: Mapping[str, Any]) -> str:
    override = _mapping(step.get("override"))
    connector = _mapping(override.get("connector"))
    return _text(connector.get("operation_id") or step.get("operation_id"))


def _find_summary_row(rows: Sequence[Any], key: str, value: str) -> dict[str, Any]:
    needle = _text(value)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if _text(row.get(key)) == needle:
            return dict(row)
    return {}


def _field_names(raw_fields: Any) -> list[str]:
    names: list[str] = []
    for row in _as_list(raw_fields):
        if isinstance(row, Mapping):
            name = _text(row.get("name") or row.get("id"))
            if name:
                names.append(name)
    return names


def _normalize_contract_rows(raw_fields: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in _as_list(raw_fields):
        if not isinstance(row, Mapping):
            continue
        name = _text(row.get("name") or row.get("id"))
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "type": _text(row.get("type") or "text") or "text",
                "description": _text(row.get("description")),
                "required": bool(row.get("required", False)),
                "contract_id": _text(row.get("contract_id")),
                "contract_kind": _text(row.get("contract_kind")),
                "contract_display_name": _text(row.get("contract_display_name")),
                "contract_schema": _mapping(row.get("contract_schema")),
            }
        )
    return rows


def _schema_contract_rows(schema: Any) -> list[dict[str, Any]]:
    payload = _mapping(schema)
    rows: list[dict[str, Any]] = []
    properties = payload.get("properties")
    required_fields = {_text(name) for name in _as_list(payload.get("required")) if _text(name)}
    if isinstance(properties, Mapping):
        for field_name, raw_spec in properties.items():
            spec = _mapping(raw_spec)
            name = _text(field_name)
            if not name:
                continue
            rows.append(
                {
                    "name": name,
                    "type": _text(spec.get("type") or "text") or "text",
                    "description": _text(spec.get("description")),
                    "required": name in required_fields,
                    "contract_id": "",
                    "contract_kind": "",
                    "contract_display_name": "",
                    "contract_schema": spec,
                }
            )
        return rows
    name = _text(payload.get("name"))
    if name:
        rows.append(
            {
                "name": name,
                "type": _text(payload.get("type") or "text") or "text",
                "description": _text(payload.get("description")),
                "required": bool(payload.get("required", True)),
                "contract_id": "",
                "contract_kind": "",
                "contract_display_name": "",
                "contract_schema": payload,
            }
        )
    return rows


def _tool_interface_operations(tool: Mapping[str, Any]) -> list[dict[str, Any]]:
    interface = _mapping(tool.get("interface"))
    if not interface:
        return []
    operations = interface.get("operations")
    if isinstance(operations, list):
        return [dict(item) for item in operations if isinstance(item, Mapping)]
    functions = interface.get("functions")
    if isinstance(functions, list):
        return [dict(item) for item in functions if isinstance(item, Mapping)]
    operation_name = (
        _text(interface.get("function_name") or tool.get("tool_id") or "invoke") or "invoke"
    )
    parameters = interface.get("parameters")
    returns = interface.get("returns")
    if isinstance(parameters, list) or isinstance(returns, Mapping):
        return [
            {
                "name": operation_name,
                "display_name": operation_name,
                "parameters": [dict(item) for item in parameters if isinstance(item, Mapping)]
                if isinstance(parameters, list)
                else [],
                "returns": dict(returns) if isinstance(returns, Mapping) else {},
                "input_schema": _mapping(interface.get("input_schema")),
                "output_schema": _mapping(interface.get("output_schema")),
            }
        ]
    return []
