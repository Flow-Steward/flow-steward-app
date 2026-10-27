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
            "module": _STANDARD_TOOLS_MODULE,
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


def builtin_standard_tool_manifests() -> dict[str, dict[str, Any]]:
    return {
        "std.numbers": _numbers_manifest(),
        "std.text": _text_manifest(),
        "std.json": _json_manifest(),
    }


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
