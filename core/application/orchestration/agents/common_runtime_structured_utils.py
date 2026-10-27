"""Structured-output utilities for agent runtime helpers."""

from __future__ import annotations

import copy
import json
import re
from typing import Any


def _structured_output_kwargs(provider: str) -> dict:
    """Return provider-appropriate LangChain structured-output settings."""
    normalized = str(provider or "").strip().lower()
    if normalized == "openai":
        return {"method": "json_schema", "strict": True}
    if normalized == "google":
        return {"method": "json_schema"}
    if normalized == "anthropic":
        return {"method": "json_schema"}
    return {}


def _prepare_openai_json_schema(schema):
    """Normalize schema for OpenAI strict json_schema mode.

    OpenAI strict mode requires object.required to align with object.properties.
    Some registry contracts also carry dynamic object schemas
    (additionalProperties=true) that are not strict-compatible. We coerce those
    leaf objects to strings to keep finalization stable.
    """
    if not isinstance(schema, dict):
        return schema

    def _sanitize(node):
        if isinstance(node, list):
            return [_sanitize(item) for item in node]
        if not isinstance(node, dict):
            return node

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "required":
                continue
            out[key] = _sanitize(value)

        node_type = str(out.get("type") or "").strip().lower()
        had_dynamic_object = (
            node_type == "object"
            and not isinstance(node.get("properties"), dict)
            and node.get("additionalProperties") not in (None, False)
        )
        if had_dynamic_object:
            description = str(out.get("description") or "").strip()
            return {
                "type": "string",
                "description": description or "JSON-serialized value.",
            }
        props = out.get("properties")
        if node_type == "object" or isinstance(props, dict):
            prop_map = props if isinstance(props, dict) else {}
            normalized_props: dict[str, Any] = {}
            for prop_name, prop_schema in prop_map.items():
                normalized = _sanitize(prop_schema)
                normalized_props[str(prop_name)] = normalized
            out["type"] = "object"
            out["properties"] = normalized_props
            out["additionalProperties"] = False
            out["required"] = list(normalized_props.keys())
            return out

        return out

    return _sanitize(copy.deepcopy(schema))


def _serialize_structured_payload(payload) -> dict:
    """Normalize structured-output results into a plain dict."""
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    if isinstance(payload, dict):
        return payload
    raise TypeError(f"Structured output returned unsupported payload type: {type(payload)!r}")


def _iter_json_object_candidates(text: str) -> list[dict]:
    """Extract JSON object candidates from plain text or fenced JSON."""
    raw = str(text or "")
    candidates: list[dict] = []
    decoder = json.JSONDecoder()

    def _append_candidate(fragment: str) -> None:
        stripped = str(fragment or "").strip()
        if not stripped:
            return
        try:
            parsed, _end = decoder.raw_decode(stripped)
        except json.JSONDecodeError:
            return
        if isinstance(parsed, dict):
            candidates.append(parsed)

    fence_cursor = 0
    while (opening_fence := raw.find("```", fence_cursor)) >= 0:
        content_start = opening_fence + 3
        if raw.startswith("json", content_start):
            content_start += 4
        closing_fence = raw.find("```", content_start)
        if closing_fence < 0:
            break
        _append_candidate(raw[content_start:closing_fence])
        fence_cursor = closing_fence + 3

    stripped_raw = raw.strip()
    if stripped_raw.startswith("{"):
        _append_candidate(stripped_raw)

    # Last-chance scan for responses with prose before the JSON envelope.
    for match in re.finditer(r"\{", raw):
        if raw[max(0, match.start() - 80) : match.start()].count("```"):
            continue
        fragment = raw[match.start() :]
        try:
            parsed, _end = decoder.raw_decode(fragment)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            candidates.append(parsed)

    return candidates


def _schema_allows_null(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if schema_type == "null":
        return True
    if isinstance(schema_type, list) and "null" in schema_type:
        return True
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and any(_schema_allows_null(item) for item in variants):
            return True
    return False


def _required_artifact_fields(schema: Any) -> list[str]:
    """Return non-nullable artifact fields required by the phase result schema."""
    if not isinstance(schema, dict):
        return []
    artifacts_schema = (schema.get("properties") or {}).get("artifacts") or {}
    if not isinstance(artifacts_schema, dict):
        return []
    properties = artifacts_schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    required = artifacts_schema.get("required")
    if not isinstance(required, list):
        required = list(properties.keys())

    fields: list[str] = []
    for field in required:
        name = str(field or "").strip()
        if not name:
            continue
        field_schema = properties.get(name)
        if _schema_allows_null(field_schema):
            continue
        fields.append(name)
    return fields


def _artifact_value_is_publishable(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return False
        return stripped not in {"...", "{}", "[]", "{", "[", ":{", ":", "null"}
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _draft_response_satisfies_phase_result_schema(draft_response: Any, schema: Any) -> bool:
    """Detect when the draft already carries the requested phase_result_v2 payload.

    A second native structured-output call is useful when the model returned prose,
    but it can be lossy for large dynamic artifact payloads. If the draft already
    publishes all non-nullable schema artifacts, keep it as the source of truth.
    """
    content = getattr(draft_response, "content", "") or ""
    required_artifacts = _required_artifact_fields(schema)
    for candidate in _iter_json_object_candidates(str(content)):
        if candidate.get("schema_version") != "phase_result_v2":
            continue
        artifacts = candidate.get("artifacts")
        if not isinstance(artifacts, dict):
            continue
        if all(
            _artifact_value_is_publishable(artifacts.get(field)) for field in required_artifacts
        ):
            return True
    return False
