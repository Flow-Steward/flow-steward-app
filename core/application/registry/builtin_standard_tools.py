"""Canonical standard tool-action manifests kept in sync at runtime."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)

_STANDARD_TOOLS_MODULE = "core.application.orchestration.tools.standard_tools"
_STANDARD_TOOL_CATEGORY = "standard_tool_action"
_CONNECTOR_ONLY_CONFIG = {"agent_exposure": "connector_only"}


def _result_return(return_type: str) -> dict[str, Any]:
    return {"name": "result", "type": return_type, "required": True}


def _standard_manifest(
    *,
    tool_id: str,
    display_name: str,
    description: str,
    label_key: str,
    description_key: str,
    functions: list[dict[str, Any]],
    builder_metadata_jsonb: dict[str, Any],
    implementation_module: str = _STANDARD_TOOLS_MODULE,
) -> dict[str, Any]:
    return {
        "tool_id": tool_id,
        "version": "1.0.0",
        "display_name": display_name,
        "description": description,
        # UI translation keys for the names above; the registry row keeps the English text.
        "label_key": label_key,
        "description_key": description_key,
        "enabled": True,
        "category": _STANDARD_TOOL_CATEGORY,
        "prompt": "",
        "interface": {"functions": functions},
        "implementation": {
            "type": "internal",
            "module": implementation_module,
        },
        "config": dict(_CONNECTOR_ONLY_CONFIG),
        "tags": ["builtin", "standard-tool-action"],
        "builder_metadata_jsonb": builder_metadata_jsonb,
    }


def _numbers_manifest() -> dict[str, Any]:
    return _standard_manifest(
        tool_id="std.numbers",
        display_name="Numbers",
        description="Deterministic numeric Standard Tool Actions.",
        label_key="registry.tools.standard.numbers.label",
        description_key="registry.tools.standard.numbers.description",
        builder_metadata_jsonb={
            "purpose": "Perform deterministic numeric cleanup and shaping without using an agent.",
            "use_cases": [
                "round computed values",
                "normalize numeric outputs",
                "prepare numbers for downstream state",
            ],
            "anti_use_cases": ["semantic analysis", "text generation"],
            "composition_role": "deterministic_transform",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        functions=[
            {
                "name": "round",
                "label_key": "registry.tools.standard.numbers.round.label",
                "description_key": "registry.tools.standard.numbers.round.description",
                "display_name": "Round",
                "description": "Round a numeric value to a fixed number of decimal places.",
                "parameters": [
                    {
                        "name": "value",
                        "type": "number",
                        "required": True,
                        "description": "Number to round.",
                    },
                    {
                        "name": "decimals",
                        "type": "integer",
                        "required": False,
                        "default": 0,
                        "description": "Decimal places to keep.",
                    },
                ],
                "returns": _result_return("number"),
            },
        ],
    )


def _text_manifest() -> dict[str, Any]:
    return _standard_manifest(
        tool_id="std.text",
        display_name="Text",
        description="Deterministic text Standard Tool Actions.",
        label_key="registry.tools.standard.text.label",
        description_key="registry.tools.standard.text.description",
        builder_metadata_jsonb={
            "purpose": "Apply simple deterministic text transformations and measurements.",
            "use_cases": [
                "normalize letter case",
                "count payload length",
                "prepare text for routing or storage",
            ],
            "anti_use_cases": ["drafting new content", "semantic classification"],
            "composition_role": "deterministic_transform",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        functions=[
            {
                "name": "upper",
                "label_key": "registry.tools.standard.text.upper.label",
                "description_key": "registry.tools.standard.text.upper.description",
                "display_name": "Uppercase",
                "description": "Convert text to uppercase.",
                "parameters": [{"name": "value", "type": "string", "required": True}],
                "returns": _result_return("string"),
            },
            {
                "name": "html_to_text",
                "label_key": "registry.tools.standard.text.html_to_text.label",
                "description_key": "registry.tools.standard.text.html_to_text.description",
                "display_name": "HTML to text",
                "description": (
                    "Extract plain text from HTML, removing scripts, styles and extra whitespace. "
                    "Use before empty or length checks. The result is plain text, not sanitized HTML."
                ),
                "parameters": [{"name": "value", "type": "string", "required": True}],
                "returns": _result_return("string"),
            },
            {
                "name": "lower",
                "label_key": "registry.tools.standard.text.lower.label",
                "description_key": "registry.tools.standard.text.lower.description",
                "display_name": "Lowercase",
                "description": "Convert text to lowercase.",
                "parameters": [{"name": "value", "type": "string", "required": True}],
                "returns": _result_return("string"),
            },
            {
                "name": "length",
                "label_key": "registry.tools.standard.text.length.label",
                "description_key": "registry.tools.standard.text.length.description",
                "display_name": "Length",
                "description": "Count the length of text, arrays, or objects.",
                "parameters": [{"name": "value", "type": "json", "required": True}],
                "returns": _result_return("integer"),
            },
        ],
    )


def _json_manifest() -> dict[str, Any]:
    return _standard_manifest(
        tool_id="std.json",
        display_name="JSON",
        description="Deterministic JSON Standard Tool Actions.",
        label_key="registry.tools.standard.json.label",
        description_key="registry.tools.standard.json.description",
        builder_metadata_jsonb={
            "purpose": "Inspect and reshape structured JSON payloads deterministically.",
            "use_cases": [
                "extract nested fields",
                "serialize payloads",
                "prepare structured data for later steps",
            ],
            "anti_use_cases": ["natural-language reasoning", "external side effects"],
            "composition_role": "deterministic_transform",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        functions=[
            {
                "name": "stringify",
                "label_key": "registry.tools.standard.json.stringify.label",
                "description_key": "registry.tools.standard.json.stringify.description",
                "display_name": "Stringify",
                "description": "Serialize a JSON object to a deterministic string.",
                "parameters": [{"name": "value", "type": "json", "required": True}],
                "returns": _result_return("string"),
            },
            {
                "name": "pick",
                "label_key": "registry.tools.standard.json.pick.label",
                "description_key": "registry.tools.standard.json.pick.description",
                "display_name": "Pick",
                "description": "Read a dotted path from a JSON object.",
                "parameters": [
                    {"name": "value", "type": "json", "required": True},
                    {
                        "name": "path",
                        "type": "string",
                        "required": True,
                        "description": "Dotted object path, with numeric segments for array indexes.",
                    },
                ],
                "returns": _result_return("json"),
            },
        ],
    )


def _additional_stage_one_functions() -> dict[str, list[dict[str, Any]]]:
    """New standard operations; existing seven connector contracts stay intact."""
    return {
        "std.text": [
            {
                "name": "trim",
                "display_name": "Trim",
                "description": "Trim using deterministic text operations.",
                "label_key": "registry.tools.standard.text.trim.label",
                "description_key": "registry.tools.standard.text.trim.description",
                "parameters": [{"name": "value", "type": "string", "required": True}],
                "returns": {"name": "result", "type": "string", "required": True},
            },
            {
                "name": "title_case",
                "display_name": "Title Case",
                "description": "Title Case using deterministic text operations.",
                "label_key": "registry.tools.standard.text.title_case.label",
                "description_key": "registry.tools.standard.text.title_case.description",
                "parameters": [{"name": "value", "type": "string", "required": True}],
                "returns": {"name": "result", "type": "string", "required": True},
            },
            {
                "name": "replace",
                "display_name": "Replace",
                "description": "Replace using deterministic text operations.",
                "label_key": "registry.tools.standard.text.replace.label",
                "description_key": "registry.tools.standard.text.replace.description",
                "parameters": [
                    {"name": "value", "type": "string", "required": True},
                    {"name": "find", "type": "string", "required": True},
                    {"name": "replace_with", "type": "string", "required": True},
                ],
                "returns": {"name": "result", "type": "string", "required": True},
            },
            {
                "name": "append",
                "display_name": "Append",
                "description": "Append using deterministic text operations.",
                "label_key": "registry.tools.standard.text.append.label",
                "description_key": "registry.tools.standard.text.append.description",
                "parameters": [
                    {"name": "value", "type": "string", "required": True},
                    {"name": "append_value", "type": "json", "required": True},
                ],
                "returns": {"name": "result", "type": "string", "required": True},
            },
            {
                "name": "pad",
                "display_name": "Pad",
                "description": "Pad using deterministic text operations.",
                "label_key": "registry.tools.standard.text.pad.label",
                "description_key": "registry.tools.standard.text.pad.description",
                "parameters": [
                    {"name": "value", "type": "string", "required": True},
                    {"name": "width", "type": "number", "required": True},
                    {"name": "fill_char", "type": "string", "required": False, "default": " "},
                    {
                        "name": "side",
                        "type": "string",
                        "required": False,
                        "values": ["left", "right"],
                        "default": "left",
                    },
                ],
                "returns": {"name": "result", "type": "string", "required": True},
            },
        ],
        "std.numbers": [
            {
                "name": "coerce",
                "display_name": "Coerce Number",
                "description": "Coerce Number using deterministic number operations.",
                "label_key": "registry.tools.standard.numbers.coerce.label",
                "description_key": "registry.tools.standard.numbers.coerce.description",
                "parameters": [{"name": "value", "type": "json", "required": True}],
                "returns": {"name": "result", "type": "number", "required": True},
            },
            {
                "name": "add",
                "display_name": "Add",
                "description": "Add using deterministic number operations.",
                "label_key": "registry.tools.standard.numbers.add.label",
                "description_key": "registry.tools.standard.numbers.add.description",
                "parameters": [
                    {"name": "value", "type": "number", "required": True},
                    {"name": "operand", "type": "number", "required": True},
                ],
                "returns": {"name": "result", "type": "number", "required": True},
            },
            {
                "name": "subtract",
                "display_name": "Subtract",
                "description": "Subtract using deterministic number operations.",
                "label_key": "registry.tools.standard.numbers.subtract.label",
                "description_key": "registry.tools.standard.numbers.subtract.description",
                "parameters": [
                    {"name": "value", "type": "number", "required": True},
                    {"name": "operand", "type": "number", "required": True},
                ],
                "returns": {"name": "result", "type": "number", "required": True},
            },
            {
                "name": "multiply",
                "display_name": "Multiply",
                "description": "Multiply using deterministic number operations.",
                "label_key": "registry.tools.standard.numbers.multiply.label",
                "description_key": "registry.tools.standard.numbers.multiply.description",
                "parameters": [
                    {"name": "value", "type": "number", "required": True},
                    {"name": "factor", "type": "number", "required": True},
                ],
                "returns": {"name": "result", "type": "number", "required": True},
            },
            {
                "name": "divide",
                "display_name": "Divide",
                "description": "Divide using deterministic number operations.",
                "label_key": "registry.tools.standard.numbers.divide.label",
                "description_key": "registry.tools.standard.numbers.divide.description",
                "parameters": [
                    {"name": "value", "type": "number", "required": True},
                    {"name": "divisor", "type": "number", "required": True},
                ],
                "returns": {"name": "result", "type": "number", "required": True},
            },
            {
                "name": "floor",
                "display_name": "Floor",
                "description": "Floor using deterministic number operations.",
                "label_key": "registry.tools.standard.numbers.floor.label",
                "description_key": "registry.tools.standard.numbers.floor.description",
                "parameters": [{"name": "value", "type": "number", "required": True}],
                "returns": {"name": "result", "type": "number", "required": True},
            },
            {
                "name": "ceil",
                "display_name": "Ceil",
                "description": "Ceil using deterministic number operations.",
                "label_key": "registry.tools.standard.numbers.ceil.label",
                "description_key": "registry.tools.standard.numbers.ceil.description",
                "parameters": [{"name": "value", "type": "number", "required": True}],
                "returns": {"name": "result", "type": "number", "required": True},
            },
        ],
        "std.money": [
            {
                "name": "coerce",
                "display_name": "Coerce Money",
                "description": "Coerce Money using deterministic money operations.",
                "label_key": "registry.tools.standard.money.coerce.label",
                "description_key": "registry.tools.standard.money.coerce.description",
                "parameters": [{"name": "value", "type": "json", "required": True}],
                "returns": {"name": "result", "type": "string", "required": True},
            },
            {
                "name": "add",
                "display_name": "Add Money",
                "description": "Add Money using deterministic money operations.",
                "label_key": "registry.tools.standard.money.add.label",
                "description_key": "registry.tools.standard.money.add.description",
                "parameters": [
                    {"name": "value", "type": "string", "required": True},
                    {"name": "operand", "type": "string", "required": True},
                ],
                "returns": {"name": "result", "type": "string", "required": True},
            },
            {
                "name": "subtract",
                "display_name": "Subtract Money",
                "description": "Subtract Money using deterministic money operations.",
                "label_key": "registry.tools.standard.money.subtract.label",
                "description_key": "registry.tools.standard.money.subtract.description",
                "parameters": [
                    {"name": "value", "type": "string", "required": True},
                    {"name": "operand", "type": "string", "required": True},
                ],
                "returns": {"name": "result", "type": "string", "required": True},
            },
            {
                "name": "multiply",
                "display_name": "Multiply Money",
                "description": "Multiply Money using deterministic money operations.",
                "label_key": "registry.tools.standard.money.multiply.label",
                "description_key": "registry.tools.standard.money.multiply.description",
                "parameters": [
                    {"name": "value", "type": "string", "required": True},
                    {"name": "factor", "type": "string", "required": True},
                ],
                "returns": {"name": "result", "type": "string", "required": True},
            },
            {
                "name": "round",
                "display_name": "Round Money",
                "description": "Round Money using deterministic money operations.",
                "label_key": "registry.tools.standard.money.round.label",
                "description_key": "registry.tools.standard.money.round.description",
                "parameters": [
                    {"name": "value", "type": "string", "required": True},
                    {"name": "decimals", "type": "number", "required": False, "default": 2},
                    {
                        "name": "mode",
                        "type": "string",
                        "required": False,
                        "values": ["half_up", "half_even", "ceil", "floor"],
                    },
                ],
                "returns": {"name": "result", "type": "string", "required": True},
            },
        ],
        "std.boolean": [
            {
                "name": "coerce",
                "display_name": "Coerce Boolean",
                "description": "Coerce Boolean using deterministic boolean operations.",
                "label_key": "registry.tools.standard.boolean.coerce.label",
                "description_key": "registry.tools.standard.boolean.coerce.description",
                "parameters": [{"name": "value", "type": "json", "required": True}],
                "returns": {"name": "result", "type": "boolean", "required": True},
            }
        ],
        "std.array": [
            {
                "name": "length",
                "display_name": "Array Length",
                "description": "Array Length using deterministic array operations.",
                "label_key": "registry.tools.standard.array.length.label",
                "description_key": "registry.tools.standard.array.length.description",
                "parameters": [{"name": "value", "type": "array", "required": True}],
                "returns": {"name": "result", "type": "number", "required": True},
            },
            {
                "name": "first",
                "display_name": "First Item",
                "description": "First Item using deterministic array operations.",
                "label_key": "registry.tools.standard.array.first.label",
                "description_key": "registry.tools.standard.array.first.description",
                "parameters": [
                    {"name": "value", "type": "array", "required": True},
                    {"name": "default", "type": "json", "required": False},
                ],
                "returns": {"name": "result", "type": "json", "required": True},
            },
            {
                "name": "at",
                "display_name": "Item At Index",
                "description": "Item At Index using deterministic array operations.",
                "label_key": "registry.tools.standard.array.at.label",
                "description_key": "registry.tools.standard.array.at.description",
                "parameters": [
                    {"name": "value", "type": "array", "required": True},
                    {"name": "index", "type": "number", "required": True},
                    {"name": "default", "type": "json", "required": False},
                ],
                "returns": {"name": "result", "type": "json", "required": True},
            },
            {
                "name": "compact",
                "display_name": "Compact",
                "description": "Compact using deterministic array operations.",
                "label_key": "registry.tools.standard.array.compact.label",
                "description_key": "registry.tools.standard.array.compact.description",
                "parameters": [{"name": "value", "type": "array", "required": True}],
                "returns": {"name": "result", "type": "array", "required": True},
            },
            {
                "name": "join_text",
                "display_name": "Join Text",
                "description": "Join Text using deterministic array operations.",
                "label_key": "registry.tools.standard.array.join_text.label",
                "description_key": "registry.tools.standard.array.join_text.description",
                "parameters": [
                    {"name": "value", "type": "array", "required": True},
                    {"name": "separator", "type": "string", "required": True},
                ],
                "returns": {"name": "result", "type": "string", "required": True},
            },
        ],
        "std.json": [
            {
                "name": "omit",
                "display_name": "Omit Paths",
                "description": "Omit Paths using deterministic json operations.",
                "label_key": "registry.tools.standard.json.omit.label",
                "description_key": "registry.tools.standard.json.omit.description",
                "parameters": [
                    {"name": "value", "type": "object", "required": True},
                    {"name": "paths", "type": "array", "required": True},
                ],
                "returns": {"name": "result", "type": "object", "required": True},
            },
            {
                "name": "get",
                "display_name": "Get Path",
                "description": "Get Path using deterministic json operations.",
                "label_key": "registry.tools.standard.json.get.label",
                "description_key": "registry.tools.standard.json.get.description",
                "parameters": [
                    {"name": "value", "type": "json", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "default", "type": "json", "required": False},
                ],
                "returns": {"name": "result", "type": "json", "required": True},
            },
            {
                "name": "set",
                "display_name": "Set Path",
                "description": "Set Path using deterministic json operations.",
                "label_key": "registry.tools.standard.json.set.label",
                "description_key": "registry.tools.standard.json.set.description",
                "parameters": [
                    {"name": "value", "type": "object", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "new_value", "type": "json", "required": True},
                ],
                "returns": {"name": "result", "type": "object", "required": True},
            },
            {
                "name": "keys",
                "display_name": "Object Keys",
                "description": "Object Keys using deterministic json operations.",
                "label_key": "registry.tools.standard.json.keys.label",
                "description_key": "registry.tools.standard.json.keys.description",
                "parameters": [{"name": "value", "type": "object", "required": True}],
                "returns": {"name": "result", "type": "array", "required": True},
            },
            {
                "name": "values",
                "display_name": "Object Values",
                "description": "Object Values using deterministic json operations.",
                "label_key": "registry.tools.standard.json.values.label",
                "description_key": "registry.tools.standard.json.values.description",
                "parameters": [{"name": "value", "type": "object", "required": True}],
                "returns": {"name": "result", "type": "array", "required": True},
            },
        ],
    }


def _date_manifest() -> dict[str, Any]:
    from core.application.orchestration.date_actions import build_date_actions

    types = {"text": "string", "number": "number", "object": "object", "array": "array"}
    functions = []
    for action in build_date_actions().values():
        parameters = [{"name": "value", "type": types[action.input_type], "required": True}]
        for name, schema in action.params_schema["properties"].items():
            parameter = {
                "name": name,
                "type": schema["type"],
                "required": name in action.required_params,
            }
            if "default" in schema:
                parameter["default"] = schema["default"]
            choices = schema.get("enum")
            if choices and all(isinstance(choice, str) for choice in choices):
                parameter["values"] = choices
            # ToolParameter supports string choices only. Numeric bounds/choices
            # remain enforced by the common date dispatcher without coercion.
            parameters.append(parameter)
        suffix = action.tool_action_id
        functions.append(
            {
                "name": suffix,
                "display_name": action.display_name,
                "description": f"{action.display_name} using deterministic date operations.",
                "label_key": f"registry.tools.standard.date.{suffix}.label",
                "description_key": f"registry.tools.standard.date.{suffix}.description",
                "parameters": parameters,
                "returns": _result_return(types[action.output_type]),
            }
        )
    return _standard_manifest(
        tool_id="std.date",
        display_name="Dates and time",
        description="Deterministic date, timezone and interval Standard Tool Actions.",
        label_key="registry.tools.standard.date.label",
        description_key="registry.tools.standard.date.description",
        functions=functions,
        implementation_module="core.application.orchestration.tools.standard_date_actions",
        builder_metadata_jsonb={
            "purpose": "Parse and shape dates, timezones and meeting intervals deterministically.",
            "composition_role": "deterministic_transform",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
    )


def builtin_standard_tool_manifests() -> dict[str, dict[str, Any]]:
    manifests = {
        "std.numbers": _numbers_manifest(),
        "std.text": _text_manifest(),
        "std.json": _json_manifest(),
        "std.date": _date_manifest(),
    }
    for tool_id, functions in _additional_stage_one_functions().items():
        if tool_id in manifests:
            manifests[tool_id]["interface"]["functions"].extend(functions)
            continue
        category = tool_id.removeprefix("std.")
        manifests[tool_id] = _standard_manifest(
            tool_id=tool_id,
            display_name={"array": "Arrays"}.get(category, category.title()),
            description=f"Deterministic {category} Standard Tool Actions.",
            label_key=f"registry.tools.standard.{category}.label",
            description_key=f"registry.tools.standard.{category}.description",
            functions=functions,
            implementation_module=f"core.application.orchestration.tools.standard_{category}_actions",
            builder_metadata_jsonb={
                "purpose": f"Apply deterministic {category} transformations without using an agent.",
                "composition_role": "deterministic_transform",
                "determinism_level": "deterministic",
                "safe_for_auto_use": True,
            },
        )
    return manifests


def _presentation(source: dict[str, Any]) -> dict[str, str]:
    return {
        "display_name": str(source.get("display_name") or ""),
        "description": str(source.get("description") or ""),
        "label_key": str(source.get("label_key") or ""),
        "description_key": str(source.get("description_key") or ""),
    }


def standard_tool_presentation(tool_id: str) -> dict[str, Any]:
    """A built-in tool's own name and description, and its operations', with their UI keys.

    Empty for a tool that is not built in. Operations are keyed by their name.
    """
    manifest = builtin_standard_tool_manifests().get(str(tool_id or "").strip())
    if manifest is None:
        return {}
    functions = manifest.get("interface", {}).get("functions", [])
    return {
        **_presentation(manifest),
        "operations": {
            str(function["name"]): _presentation(function)
            for function in functions
            if isinstance(function, dict) and function.get("name")
        },
    }


def _tool_payload(existing: dict[str, Any] | None, desired: dict[str, Any]) -> dict[str, Any]:
    existing = existing or {}
    existing_tags = existing.get("tags")
    desired_tags = desired.get("tags", [])
    existing_config = existing.get("config")
    config = deepcopy(existing_config) if isinstance(existing_config, dict) else {}
    config["agent_exposure"] = _CONNECTOR_ONLY_CONFIG["agent_exposure"]
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
        "config": config,
        "tags": list(existing_tags if isinstance(existing_tags, list) else desired_tags),
        "builder_metadata_jsonb": deepcopy(desired.get("builder_metadata_jsonb", {})),
    }


def _canonical_tool_payload(payload: dict[str, Any], *, from_db: bool) -> dict[str, Any]:
    from core.application.registry.tool_manifest import ToolManifest

    manifest = (
        ToolManifest.from_db_row(payload) if from_db else ToolManifest.model_validate(payload)
    )
    return manifest.to_registry_payload()


def _tool_payload_matches(
    existing: dict[str, Any] | None,
    payload: dict[str, Any],
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


def sync_builtin_standard_tools(db_store) -> list[str]:
    changed: list[str] = []
    for tool_id, desired in builtin_standard_tool_manifests().items():
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
                    "config",
                    "builder_metadata_jsonb",
                ),
            ):
                continue
            db_store.save_tool(payload, author="system")
            changed.append(tool_id)
        except Exception as exc:
            logger.warning("Could not sync builtin standard tool '%s': %s", tool_id, exc)
    return changed
