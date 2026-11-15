"""Provider-neutral conditional configuration writes for an owning extension."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from core.application.extensions.connection_partition_validation import (
    require_valid_connection_partitions,
)
from core.application.projects.connection_config_repository import JsonEquality

CONDITIONAL_CONFIG_MUTATION = "project_connection_config_compare_and_set"
MAX_CONFIG_PATHS = 16
MAX_CONFIG_PATH_LENGTH = 256
MAX_CONFIG_PATH_DEPTH = 16
MAX_JSON_DEPTH = 64
_MISSING = object()


class ConnectionConfigMutationRejected(ValueError):
    """A safe, structured conditional-write rejection without config values."""

    def __init__(
        self,
        reason: str,
        *,
        path: str = "",
        code: str = "invalid_payload",
    ) -> None:
        super().__init__(f"Conditional connection configuration rejected: {reason}")
        self.code = code
        self.reason = reason
        self.path = path


@dataclass(frozen=True)
class _Condition:
    path: str
    parts: tuple[str, ...]
    state: str
    value: Any


def _parts(path: Any) -> tuple[str, ...]:
    if (
        not isinstance(path, str)
        or not path
        or len(path) > MAX_CONFIG_PATH_LENGTH
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise ConnectionConfigMutationRejected("invalid_path")
    _json_value(path)
    parts = tuple(path.split("."))
    if any(not part for part in parts) or len(parts) > MAX_CONFIG_PATH_DEPTH:
        raise ConnectionConfigMutationRejected("invalid_path")
    if parts[0] == "oauth":
        raise ConnectionConfigMutationRejected("protected_path", path=path)
    return parts


def _json_value(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ConnectionConfigMutationRejected("json_depth_exceeded")
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ConnectionConfigMutationRejected("invalid_json_string") from exc
        if "\x00" not in value:
            return
    elif isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
        return
    elif isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, item in value.items():
            _json_value(key, depth + 1)
            _json_value(item, depth + 1)
        return
    raise ConnectionConfigMutationRejected("invalid_json_value")


def _parse(payload: dict[str, Any]) -> tuple[list[_Condition], dict[str, Any]]:
    allowed = {"connection_id", "extension_install_id", "expect", "set"}
    if set(payload) - allowed:
        raise ConnectionConfigMutationRejected("unknown_fields")
    expected, assignments = payload.get("expect"), payload.get("set")
    if not isinstance(expected, list) or not expected or len(expected) > MAX_CONFIG_PATHS:
        raise ConnectionConfigMutationRejected("invalid_expectations")
    if not isinstance(assignments, dict) or not assignments or len(assignments) > MAX_CONFIG_PATHS:
        raise ConnectionConfigMutationRejected("invalid_assignments")
    conditions: list[_Condition] = []
    seen: set[str] = set()
    for row in expected:
        if not isinstance(row, dict) or set(row) - {"path", "state", "value"}:
            raise ConnectionConfigMutationRejected("invalid_expectation")
        path, state = row.get("path"), row.get("state")
        parts = _parts(path)
        if path in seen:
            raise ConnectionConfigMutationRejected("duplicate_expectation")
        if state not in ("absent", "equals", "absent_or_equals"):
            raise ConnectionConfigMutationRejected("invalid_expectation_state")
        if (state == "absent") == ("value" in row):
            raise ConnectionConfigMutationRejected("invalid_expectation_value")
        value = row.get("value")
        _json_value(value)
        seen.add(path)
        conditions.append(_Condition(path, parts, state, value))
    for path, value in assignments.items():
        _parts(path)
        _json_value(value)
    if len(seen | set(assignments)) > MAX_CONFIG_PATHS:
        raise ConnectionConfigMutationRejected("path_budget_exceeded")
    if any(
        later.startswith(earlier + ".")
        for earlier in assignments
        for later in assignments
        if earlier != later
    ):
        raise ConnectionConfigMutationRejected("overlapping_assignments")
    return conditions, assignments


def _read(config: dict[str, Any], parts: tuple[str, ...]) -> Any:
    value: Any = config
    for key in parts:
        if not isinstance(value, dict):
            raise ConnectionConfigMutationRejected("non_object_path")
        if key not in value:
            return _MISSING
        value = value[key]
    return value


def _patch(config: dict[str, Any], assignments: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(config)
    for path, value in assignments.items():
        parts = _parts(path)
        parent = result
        for key in parts[:-1]:
            if key not in parent:
                parent[key] = {}
            child = parent[key]
            if not isinstance(child, dict):
                raise ConnectionConfigMutationRejected("non_object_path", path=path)
            parent = child
        parent[parts[-1]] = deepcopy(value)
    return result


def _payload_identifier(payload: dict[str, Any], key: str) -> str:
    if key not in payload:
        return ""
    value = payload[key]
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_CONFIG_PATH_LENGTH:
        raise ConnectionConfigMutationRejected("invalid_identifier", path=key)
    _json_value(value)
    return value.strip()


def apply_conditional_connection_config(
    payload: dict[str, Any],
    *,
    db: Any,
    extension: dict[str, Any],
    grant: dict[str, Any],
    account_id: str,
    project_id: str,
    extension_id: str,
    connection_id: str | None,
    extension_install_id: str | None,
) -> None:
    """Apply a declared conditional write inside the existing host transaction."""
    conditions, assignments = _parse(payload)
    payload_connection = _payload_identifier(payload, "connection_id")
    payload_install = _payload_identifier(payload, "extension_install_id")
    requested_connection = str(connection_id or "").strip()
    requested_install = str(
        extension_install_id or extension.get("extension_install_id") or ""
    ).strip()
    if payload_connection and requested_connection and payload_connection != requested_connection:
        raise ConnectionConfigMutationRejected("connection_scope_mismatch")
    if payload_install and payload_install != requested_install:
        raise ConnectionConfigMutationRejected("install_scope_mismatch")
    resolved_connection = requested_connection or payload_connection
    if not resolved_connection or not requested_install:
        raise ConnectionConfigMutationRejected("scope_missing")

    def transform(existing: dict[str, Any], equal: JsonEquality) -> dict[str, Any]:
        config = existing.get("connection_config")
        if not isinstance(config, dict):
            raise ConnectionConfigMutationRejected("invalid_current_config")
        allowed_types = grant.get("connection_type_ids")
        if allowed_types and existing.get("connection_type") not in allowed_types:
            raise ConnectionConfigMutationRejected("connection_type_forbidden")
        for condition in conditions:
            current = _read(config, condition.parts)
            absent = current is _MISSING
            matches = (
                absent
                if condition.state == "absent"
                else (absent and condition.state == "absent_or_equals")
                or (not absent and equal(condition.parts, condition.value))
            )
            if not matches:
                raise ConnectionConfigMutationRejected(
                    "absent_expected" if condition.state == "absent" else "value_mismatch",
                    path=condition.path,
                    code="host_mutation_precondition_failed",
                )
        updated = _patch(config, assignments)
        require_valid_connection_partitions(
            extension,
            connection_type=str(existing.get("connection_type") or ""),
            connection_config=updated,
            ingress_config=existing.get("ingress_config") or {},
        )
        return assignments

    found = db.mutate_locked_project_connection_config(
        connection_id=resolved_connection,
        account_id=account_id,
        project_id=project_id,
        extension_id=extension_id,
        extension_install_id=requested_install,
        transform=transform,
    )
    if not found:
        raise ConnectionConfigMutationRejected(
            "connection_not_found", code="host_mutation_connection_not_found"
        )
