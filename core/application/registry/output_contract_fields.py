"""Pure normalization of output declarations and provider-format schemas."""

from __future__ import annotations

import json
from typing import Any


def response_field_json_schema(
    field_type: str, description: str = "", contract_schema: dict | None = None
) -> dict:
    """Map registry contract field types onto JSON Schema types for Structured Outputs."""
    normalized = str(field_type or "text").strip().lower()
    if isinstance(contract_schema, dict) and contract_schema:
        schema = dict(contract_schema)
        if description and not str(schema.get("description") or "").strip():
            schema["description"] = str(description).strip()
        return schema

    schema: dict[str, Any]
    if normalized == "number":
        schema = {"type": "number"}
    elif normalized == "boolean":
        schema = {"type": "boolean"}
    elif normalized in {"json", "object"}:
        schema = {"type": "object", "additionalProperties": True}
    elif normalized in {"list", "array"}:
        # An array with no linked contract has no item shape; text items are the
        # only one every provider's strict mode accepts. A contract shapes items.
        schema = {"type": "array", "items": {"type": "string"}}
    else:
        schema = {"type": "string"}
    if description:
        schema["description"] = str(description).strip()
    return schema


def published_output_description(row: dict) -> str:
    """Established block presentation fallback, shared by editor and execution."""
    return next(
        (
            str(row.get(key) or "").strip()
            for key in ("notes", "field_description", "field_display_name")
            if str(row.get(key) or "").strip()
        ),
        "",
    )


class OutputContractResolutionError(ValueError):
    """The selected output authority could not be resolved consistently."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


_TYPE_ALIASES = {
    **dict.fromkeys(("text", "paragraph", "select", "secret"), "string"),
    "json": "object",
    "list": "array",
    "bool": "boolean",
    "int": "integer",
    "float": "number",
    "file": "object",
}


def _type(value: Any) -> str:
    raw = str(value or "string").strip().lower()
    normalized = _TYPE_ALIASES.get(raw, raw)
    if normalized not in {"string", "number", "integer", "boolean", "array", "object", "null"}:
        raise OutputContractResolutionError(f"Unsupported output type '{raw}'")
    return normalized


def _declaration_views(row: dict, schema: dict | None) -> tuple[str, dict, dict]:
    """Keep declaration constraints separate from legacy response-format defaults."""
    from jsonschema import Draft202012Validator

    raw_type = str(row.get("type") or "string").strip().lower()
    normalized = _type(raw_type)
    authored = schema if schema is not None else row.get("contract_schema")
    if authored is not None and not isinstance(authored, dict):
        raise OutputContractResolutionError("Invalid output declaration schema")
    declaration = (
        authored if authored is not None else {} if raw_type == "json" else {"type": normalized}
    )
    description = str(row.get("description") or "").strip()
    if authored == {}:
        # An explicitly unshaped authored schema stays unshaped; only the
        # existing presentation annotation may be supplied alongside it.
        response = {"description": description} if description else {}
    else:
        response = response_field_json_schema(raw_type, description, authored)
    declaration = json.loads(_canonical(declaration))
    response = json.loads(_canonical(response))
    Draft202012Validator.check_schema(declaration)
    Draft202012Validator.check_schema(response)
    schema_type = declaration.get("type")
    schema_types = schema_type if isinstance(schema_type, list) else [schema_type]
    declared_types = {_type(value) for value in schema_types if isinstance(value, str)}
    non_null_types = declared_types - {"null"}
    precise_types = non_null_types or declared_types
    if raw_type == "json":
        producer_type = next(iter(precise_types)) if len(precise_types) == 1 else "json"
    else:
        producer_type = normalized
        if declared_types and precise_types != {normalized}:
            raise OutputContractResolutionError(f"Conflicting output type for '{row.get('name')}'")
    return producer_type, declaration, response


def _intersect_schemas(first: dict, second: dict) -> dict:
    if first == second or not second:
        return first
    if not first:
        return second
    return {"allOf": [first, second]}


def _without_description(schema: dict) -> dict:
    """Compare format constraints without the helper's top-level text annotation."""
    return {key: value for key, value in schema.items() if key != "description"}


def _merge_declaration_views(existing: dict, field_type: str, declaration: dict, response: dict):
    previous_type = existing["type"]
    if previous_type != field_type and "json" not in {previous_type, field_type}:
        raise OutputContractResolutionError(f"Conflicting output type for '{existing['name']}'")
    merged_type = field_type if previous_type == "json" else previous_type
    previous_schema = existing["contract_schema"]
    previous_response = existing["response_schema"]
    # Response-only defaults are not authored constraints. Prefer an authored
    # response view when another declaration supplies one for the same value.
    previous_format = _without_description(previous_response)
    next_format = _without_description(response)
    previous_authored = previous_format == _without_description(previous_schema)
    next_authored = next_format == _without_description(declaration)
    if not previous_authored and next_authored:
        merged_response = response
    elif not next_authored and previous_authored:
        merged_response = previous_response
    elif previous_format == next_format:
        # Equal constraints do not make an injected row description authored.
        # Compare the intact views before applying presentation-text precedence.
        if (
            "description" in declaration
            and response == declaration
            and previous_response != previous_schema
        ):
            merged_response = response
        else:
            merged_response = (
                previous_response if previous_response.get("description") else response
            )
    else:
        merged_response = _intersect_schemas(previous_response, response)
    return merged_type, _intersect_schemas(previous_schema, declaration), merged_response


def add_output_declaration(
    fields: dict[str, dict], row: dict, source: str, source_id: str, schema: dict | None = None
) -> None:
    name = str(row.get("name") or "").strip()
    if not name:
        raise OutputContractResolutionError("Output declaration has no name")
    field_type, declaration, response = _declaration_views(row, schema)
    existing = fields.get(name)
    if existing:
        field_type, declaration, response = _merge_declaration_views(
            existing, field_type, declaration, response
        )
        source_id = existing["source_id"] + ";" + source_id
        source = existing["source"] + ";" + source
    fields[name] = {
        "name": name,
        "type": field_type,
        "description": str(
            (existing or {}).get("description") or row.get("description") or ""
        ).strip(),
        "required": bool(row.get("required", False)) or bool(existing and existing["required"]),
        "source": source,
        "source_id": source_id,
        "contract_schema": declaration,
        "response_schema": response,
    }
