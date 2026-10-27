from __future__ import annotations

from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_contract_row(row: Any) -> dict[str, Any] | None:
    if isinstance(row, str):
        name = _text(row)
        if not name:
            return None
        return {"name": name, "type": "text", "required": False, "description": ""}
    if not isinstance(row, dict):
        return None
    name = _text(row.get("name"))
    if not name:
        return None
    normalized = {
        "name": name,
        "type": _text(row.get("type")) or "text",
        "required": bool(row.get("required")),
        "description": _text(row.get("description")),
    }
    schema = row.get("schema")
    if isinstance(schema, dict):
        normalized["schema"] = dict(schema)
    return normalized


def _normalized_contract_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        normalized_row = _normalized_contract_row(row)
        if normalized_row is None:
            continue
        key = _text(normalized_row.get("name")).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(normalized_row)
    return normalized


def _rows_from_schema(schema: Any) -> list[dict[str, Any]]:
    if not isinstance(schema, dict):
        return []
    direct_name = _text(schema.get("name"))
    if direct_name:
        return [
            {
                "name": direct_name,
                "type": _text(schema.get("type")) or "text",
                "required": bool(schema.get("required", True)),
                "description": _text(schema.get("description")),
            }
        ]
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    required = {_text(name) for name in (schema.get("required") or []) if _text(name)}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, details in properties.items():
        normalized_name = _text(name)
        key = normalized_name.lower()
        if not normalized_name or key in seen:
            continue
        seen.add(key)
        details_dict = details if isinstance(details, dict) else {}
        rows.append(
            {
                "name": normalized_name,
                "type": _text(details_dict.get("type")) or "text",
                "required": normalized_name in required,
                "description": _text(details_dict.get("description")),
            }
        )
    return rows


def _rows_from_interface_operations(
    interface: dict[str, Any], *, kind: str
) -> list[dict[str, Any]]:
    operations = _interface_operations(interface)
    if not operations:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        if kind == "input":
            operation_rows = _normalized_contract_rows(
                operation.get("parameters")
            ) or _rows_from_schema(operation.get("input_schema"))
        else:
            operation_rows = _rows_from_schema(operation.get("output_schema")) or _rows_from_schema(
                operation.get("returns")
            )
        for row in operation_rows:
            key = _text(row.get("name")).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def _interface_operations(interface: Any) -> list[dict[str, Any]]:
    if not isinstance(interface, dict):
        return []
    operations = interface.get("operations")
    if not isinstance(operations, list):
        operations = interface.get("functions")
    if not isinstance(operations, list):
        return []
    return [row for row in operations if isinstance(row, dict)]


def _operation_name(operation: Any) -> str:
    if not isinstance(operation, dict):
        return ""
    return _text(
        operation.get("name")
        or operation.get("id")
        or operation.get("operation_id")
        or operation.get("function_name")
    )


def _operation_display_name(operation: Any) -> str:
    if not isinstance(operation, dict):
        return ""
    return _text(
        operation.get("display_name") or operation.get("label") or _operation_name(operation)
    )


def _operation_description(operation: Any) -> str:
    if not isinstance(operation, dict):
        return ""
    return _text(operation.get("description") or operation.get("summary"))


def _operation_contract_rows(operation: dict[str, Any], *, kind: str) -> list[dict[str, Any]]:
    if kind == "input":
        return _normalized_contract_rows(operation.get("parameters")) or _rows_from_schema(
            operation.get("input_schema")
        )
    return (
        _normalized_contract_rows(operation.get("returns"))
        or _rows_from_schema(operation.get("output_schema"))
        or _rows_from_schema(operation.get("returns"))
    )


def _tool_operation_match(
    operation: dict[str, Any],
    *,
    operation_id: str = "",
    function_name: str = "",
) -> bool:
    selectors = {
        _text(operation_id).lower(),
        _text(function_name).lower(),
    } - {""}
    if not selectors:
        return False
    names = {
        _operation_name(operation).lower(),
        _operation_display_name(operation).lower(),
    } - {""}
    return bool(selectors & names)


def tool_operations(tool: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(tool, dict):
        return []
    operations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for contract_key in ("interface_contract", "interface"):
        contract = tool.get(contract_key)
        for operation in _interface_operations(contract):
            operation_name = _operation_name(operation)
            key = operation_name.lower()
            if not operation_name or key in seen:
                continue
            seen.add(key)
            operations.append(
                {
                    "operation_id": operation_name,
                    "function_name": operation_name,
                    "display_name": _operation_display_name(operation) or operation_name,
                    "description": _operation_description(operation),
                    "input_rows": _operation_contract_rows(operation, kind="input"),
                    "output_rows": _operation_contract_rows(operation, kind="output"),
                    "side_effect_type": _text(
                        operation.get("side_effect_type") or operation.get("side_effect")
                    ),
                    "idempotency": _text(operation.get("idempotency")),
                    **(
                        {
                            "connection_type_ids": [
                                _text(value)
                                for value in operation.get("connection_type_ids", [])
                                if _text(value)
                            ]
                        }
                        if "connection_type_ids" in operation
                        else {}
                    ),
                    **(
                        {"mutates_platform": bool(operation.get("mutates_platform"))}
                        if "mutates_platform" in operation
                        else {}
                    ),
                }
            )
    return operations


def _tool_contract_rows(
    tool: dict[str, Any] | None,
    *,
    kind: str,
    operation_id: str = "",
    function_name: str = "",
) -> list[dict[str, Any]]:
    if not isinstance(tool, dict):
        return []
    operations = tool_operations(tool)
    if operation_id or function_name:
        for operation in operations:
            if _tool_operation_match(
                operation,
                operation_id=operation_id,
                function_name=function_name,
            ):
                return _normalized_contract_rows(operation.get(f"{kind}_rows"))
    if len(operations) == 1:
        only_operation = operations[0]
        rows = _normalized_contract_rows(only_operation.get(f"{kind}_rows"))
        if rows:
            return rows
    direct_keys = (
        ("expected_inputs", "_inputs") if kind == "input" else ("expected_outputs", "_outputs")
    )
    for key in direct_keys:
        rows = _normalized_contract_rows(tool.get(key))
        if rows:
            return rows
    for contract_key in ("interface_contract", "interface"):
        contract = tool.get(contract_key)
        if not isinstance(contract, dict):
            continue
        if kind == "input":
            for key in ("expected_inputs", "inputs", "parameters"):
                rows = _normalized_contract_rows(contract.get(key))
                if rows:
                    return rows
            rows = _rows_from_schema(contract.get("input_schema"))
            if rows:
                return rows
        else:
            for key in ("expected_outputs", "outputs"):
                rows = _normalized_contract_rows(contract.get(key))
                if rows:
                    return rows
            rows = _rows_from_schema(contract.get("output_schema")) or _rows_from_schema(
                contract.get("returns")
            )
            if rows:
                return rows
        rows = _rows_from_interface_operations(contract, kind=kind)
        if rows:
            if len(operations) <= 1:
                return rows
            return []
    return []


def tool_expected_inputs(
    tool: dict[str, Any] | None,
    *,
    operation_id: str = "",
    function_name: str = "",
) -> list[dict[str, Any]]:
    return _tool_contract_rows(
        tool,
        kind="input",
        operation_id=operation_id,
        function_name=function_name,
    )


def tool_expected_outputs(
    tool: dict[str, Any] | None,
    *,
    operation_id: str = "",
    function_name: str = "",
) -> list[dict[str, Any]]:
    return _tool_contract_rows(
        tool,
        kind="output",
        operation_id=operation_id,
        function_name=function_name,
    )


def contract_field_names(rows: list[dict[str, Any]] | None) -> list[str]:
    return [
        _text(row.get("name"))
        for row in (rows or [])
        if isinstance(row, dict) and _text(row.get("name"))
    ]
