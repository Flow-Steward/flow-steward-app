"""Bounded, provider-neutral contract for read-only structured Dataset queries.

Adapters must additionally restrict provider arguments, resource/column access,
row limits and timeouts before dispatch. Core never interprets provider commands.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from core.application.datasets.saved_query_errors import invalid_saved_query

MAX_DEFINITION_BYTES = 64 * 1024
MAX_DEFINITION_DEPTH = 20
MAX_DEFINITION_NODES = 10000
READ_OPERATIONS = frozenset(
    {"find", "aggregate", "get", "mget", "hget", "hgetall", "lrange", "search", "query"}
)
_DEFINITION_PATH = "query_definition_jsonb"
_ARGUMENTS_PATH = f"{_DEFINITION_PATH}.arguments"


def _resource_scope(dataset: dict[str, Any]) -> set[str]:
    scope = dataset.get("resource_scope_jsonb") or dataset.get("resource_scope") or {}
    if not scope:
        metadata = dataset.get("metadata_jsonb") or dataset.get("metadata") or {}
        if isinstance(metadata, dict):
            scope = metadata.get("resource_scope_jsonb") or metadata.get("resource_scope") or {}
    resources = scope.get("resources") if isinstance(scope, dict) else None
    if not isinstance(resources, list):
        return set()
    return {
        item["resource_id"]
        for item in resources
        if isinstance(item, dict)
        and isinstance(item.get("resource_id"), str)
        and item["resource_id"].strip()
    }


def _validate_arguments(arguments: dict[str, Any], declared_params: set[str]) -> None:
    stack: list[tuple[Any, int, str]] = [(arguments, 2, _ARGUMENTS_PATH)]
    nodes = 3
    while stack:
        value, depth, path = stack.pop()
        nodes += 1
        if depth > MAX_DEFINITION_DEPTH or nodes > MAX_DEFINITION_NODES:
            raise invalid_saved_query(_ARGUMENTS_PATH, "Structured query exceeds JSON bounds")
        if (
            isinstance(value, (dict, list))
            and len(value) + len(stack) > MAX_DEFINITION_NODES - nodes
        ):
            raise invalid_saved_query(_ARGUMENTS_PATH, "Structured query exceeds JSON bounds")
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise invalid_saved_query(_ARGUMENTS_PATH, "JSON object keys must be strings")
            if "$param" in value:
                name = value["$param"]
                if (
                    len(value) != 1
                    or not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z_](?a:\w)*", name)
                ):
                    raise invalid_saved_query(path, "Parameter reference must be {$param: name}")
                if name not in declared_params:
                    raise invalid_saved_query(
                        f"params_schema_jsonb.properties.{name}",
                        "Structured query parameter is not declared",
                    )
            stack.extend((item, depth + 1, f"{path}.{key}") for key, item in value.items())
        elif isinstance(value, list):
            stack.extend((item, depth + 1, f"{path}[{index}]") for index, item in enumerate(value))
        elif type(value) not in {str, int, float, bool, type(None)} or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise invalid_saved_query(_ARGUMENTS_PATH, "Arguments must contain JSON values")


def validate_structured_query_definition(
    definition: Any, *, dataset: dict[str, Any], declared_params: set[str]
) -> None:
    """Validate a structured read definition without coercing or substituting values."""
    if not isinstance(definition, dict):
        raise invalid_saved_query(_DEFINITION_PATH, "Query definition must be an object")
    allowed_fields = {"resource_id", "operation", "arguments"}
    for key in definition:
        if key not in allowed_fields:
            raise invalid_saved_query(f"{_DEFINITION_PATH}.{key}", "Unsupported definition field")
    resource = definition.get("resource_id")
    if not isinstance(resource, str) or not resource or resource != resource.strip():
        raise invalid_saved_query(
            f"{_DEFINITION_PATH}.resource_id", "Exact resource_id is required"
        )
    resources = _resource_scope(dataset)
    if not resources:
        raise invalid_saved_query("dataset.resource_scope_jsonb", "Dataset resource scope is empty")
    if resource not in resources:
        raise invalid_saved_query(
            f"{_DEFINITION_PATH}.resource_id", "Query resource is outside the dataset scope"
        )
    operation = definition.get("operation")
    if not isinstance(operation, str) or operation not in READ_OPERATIONS:
        raise invalid_saved_query(f"{_DEFINITION_PATH}.operation", "Unsupported read operation")
    arguments = definition.get("arguments")
    if not isinstance(arguments, dict):
        raise invalid_saved_query(_ARGUMENTS_PATH, "Arguments must be an object")
    _validate_arguments(arguments, declared_params)
    try:
        encoded = json.dumps(definition, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise invalid_saved_query(
            _ARGUMENTS_PATH, "Query definition must contain bounded JSON"
        ) from exc
    if len(encoded) > MAX_DEFINITION_BYTES:
        raise invalid_saved_query(_ARGUMENTS_PATH, "Structured query exceeds JSON size bound")
