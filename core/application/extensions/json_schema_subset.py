"""The JSON Schema subset the platform actually executes, in one place.

Extension authors declare their contracts in JSON Schema, and this module is
what the platform can enforce. Two rules keep the two halves honest:

* Every keyword listed in :data:`EXECUTED_KEYWORDS` is executed by
  :func:`validate_json_schema_value`.
* Every other keyword except the annotations in :data:`ANNOTATION_KEYWORDS` is
  rejected at install time by :func:`unsupported_schema_keywords`.

The rule that matters is the second one. Before it existed the validator was a
hand-written subset that silently ignored anything it did not recognise, so a
schema declaring `allOf` / `if` / `then` to require a host and a database
accepted `{"prebuilt": "postgres"}` without a single error. An author had every
reason to believe their contract was enforced. Silence is the worst answer a
validator can give: a loud rejection at install time tells the author their
declaration will not be executed, while acceptance tells them it will.

`format` is deliberately an annotation rather than an unsupported keyword. JSON
Schema itself defines it as annotation-only unless a format-assertion vocabulary
is enabled, so allowing it without executing it is what the specification says,
not a loophole.
"""

from __future__ import annotations

import re
from typing import Any

# Keywords validate_json_schema_value below actually enforces.
EXECUTED_KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "oneOf",
        "anyOf",
        "allOf",
        "if",
        "then",
        "else",
        "dependentRequired",
        "not",
    }
)

# Keywords that carry no assertion. Allowed, and knowingly not executed.
ANNOTATION_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$comment",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "format",
    }
)

_SUPPORTED_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def _json_schema_type_matches(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _subschemas(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every nested schema and the path fragment that reaches it."""
    found: list[tuple[str, dict[str, Any]]] = []
    for key in ("properties", "$defs", "definitions"):
        nested = schema.get(key)
        if isinstance(nested, dict):
            found.extend(
                (f"{key}.{name}", value)
                for name, value in nested.items()
                if isinstance(value, dict)
            )
    for key in ("items", "additionalProperties", "if", "then", "else", "not"):
        nested = schema.get(key)
        if isinstance(nested, dict):
            found.append((key, nested))
    for key in ("oneOf", "anyOf", "allOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            found.extend(
                (f"{key}[{index}]", branch)
                for index, branch in enumerate(branches)
                if isinstance(branch, dict)
            )
    return found


def unsupported_schema_keywords(schema: Any, *, path: str = "") -> list[str]:
    """Report every keyword the platform cannot execute, deepest paths included.

    A schema that passes this is one whose every assertion the platform will
    actually run. A schema that fails it is telling its author something true:
    the declaration would have been decoration.
    """
    if not isinstance(schema, dict):
        return []

    findings: list[str] = []
    known = EXECUTED_KEYWORDS | ANNOTATION_KEYWORDS
    for keyword in schema:
        name = str(keyword)
        if name in known:
            continue
        findings.append(
            f"{path or '<root>'}: schema keyword {name!r} is not executed by this platform"
        )

    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        declared_types = [raw_type]
    elif isinstance(raw_type, list):
        declared_types = raw_type
    else:
        declared_types = []
    for declared in declared_types:
        text = str(declared).strip().lower()
        if text and text not in _SUPPORTED_TYPES:
            findings.append(f"{path or '<root>'}: unknown schema type {declared!r}")

    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            re.compile(pattern)
        except re.error:
            findings.append(f"{path or '<root>'}: schema pattern is not a valid regular expression")

    for fragment, nested in _subschemas(schema):
        findings.extend(
            unsupported_schema_keywords(nested, path=f"{path}.{fragment}" if path else fragment)
        )
    return findings


def _branch_errors(schema: dict[str, Any], value: Any, path: str) -> list[str]:
    collected: list[str] = []
    validate_json_schema_value(schema=schema, value=value, path=path, errors=collected)
    return collected


def _closest_branch(failures: list[list[str]], branches: list[Any], value: Any) -> list[str]:
    """The branch whose errors are worth showing.

    Fewest errors is the wrong tie-break on its own: for a list value against
    `string | array of string`, both branches fail with one error each, and the
    string branch wins by position while saying nothing useful. Prefer a branch
    whose declared type matches what the caller actually sent - that is the one
    they meant.
    """
    if not failures:
        return []
    typed = [
        index
        for index, branch in enumerate(branches)
        if isinstance(branch, dict)
        and isinstance(branch.get("type"), str)
        and _json_schema_type_matches(str(branch["type"]).strip().lower(), value)
    ]
    candidates = [failures[index] for index in typed if index < len(failures)] or failures
    return min(candidates, key=len)


def _validate_composition(schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    """oneOf / anyOf / allOf / if-then-else / dependentRequired."""
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            if isinstance(branch, dict):
                errors.extend(_branch_errors(branch, value, path))

    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        branch_failures = [
            _branch_errors(branch, value, path) for branch in any_of if isinstance(branch, dict)
        ]
        if branch_failures and all(branch_failures):
            # Report the branch that came closest rather than every alternative:
            # the author needs one actionable list, not the cross-product.
            errors.extend(_closest_branch(branch_failures, list(any_of), value))

    one_of = schema.get("oneOf")
    if isinstance(one_of, list) and one_of:
        branch_failures = [
            _branch_errors(branch, value, path) for branch in one_of if isinstance(branch, dict)
        ]
        matched = [index for index, failures in enumerate(branch_failures) if not failures]
        if not matched:
            closest = _closest_branch(branch_failures, list(one_of), value)
            errors.extend(closest or [f"{path}: value matches none of the allowed alternatives"])
        elif len(matched) > 1:
            errors.append(
                f"{path}: value matches {len(matched)} alternatives but must match exactly one"
            )

    if_schema = schema.get("if")
    if isinstance(if_schema, dict):
        condition_met = not _branch_errors(if_schema, value, path)
        follow_up = schema.get("then") if condition_met else schema.get("else")
        if isinstance(follow_up, dict):
            errors.extend(_branch_errors(follow_up, value, path))

    dependent_required = schema.get("dependentRequired")
    if isinstance(dependent_required, dict) and isinstance(value, dict):
        for trigger, dependents in dependent_required.items():
            if trigger not in value or not isinstance(dependents, list):
                continue
            for dependent in dependents:
                key = str(dependent or "").strip()
                if key and key not in value:
                    errors.append(
                        f"{path}.{key}: required property is missing because {trigger!r} is present"
                    )

    negated = schema.get("not")
    if isinstance(negated, dict) and not _branch_errors(negated, value, path):
        errors.append(f"{path}: value matches a forbidden schema")


def validate_json_schema_value(
    *,
    schema: dict[str, Any],
    value: Any,
    path: str,
    errors: list[str],
) -> None:
    """Validate a value against the executed subset, appending readable errors."""
    schema_payload = dict(schema or {})
    if not schema_payload:
        return

    raw_type = schema_payload.get("type")
    allowed_types: list[str] = []
    if isinstance(raw_type, str) and raw_type.strip():
        allowed_types = [raw_type.strip().lower()]
    elif isinstance(raw_type, list):
        allowed_types = [
            str(item).strip().lower()
            for item in raw_type
            if isinstance(item, str) and str(item).strip()
        ]

    if allowed_types and not any(_json_schema_type_matches(item, value) for item in allowed_types):
        errors.append(
            f"{path}: expected type {allowed_types if len(allowed_types) > 1 else allowed_types[0]}"
        )
        return

    enum_values = schema_payload.get("enum")
    if isinstance(enum_values, list) and enum_values and value not in enum_values:
        errors.append(f"{path}: value {value!r} is not in enum {enum_values!r}")

    if "const" in schema_payload and value != schema_payload["const"]:
        errors.append(f"{path}: value {value!r} must be {schema_payload['const']!r}")

    if isinstance(value, str):
        min_length = schema_payload.get("minLength")
        max_length = schema_payload.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"{path}: string length must be >= {min_length}")
        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(f"{path}: string length must be <= {max_length}")
        pattern = schema_payload.get("pattern")
        if isinstance(pattern, str):
            try:
                matches = re.search(pattern, value) is not None
            except re.error:
                matches = False
            if not matches:
                errors.append(f"{path}: string does not match the required pattern")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema_payload.get("minimum")
        maximum = schema_payload.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}: value must be >= {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path}: value must be <= {maximum}")

    if isinstance(value, list):
        min_items = schema_payload.get("minItems")
        max_items = schema_payload.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{path}: array length must be >= {min_items}")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"{path}: array length must be <= {max_items}")
        item_schema = schema_payload.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                validate_json_schema_value(
                    schema=item_schema,
                    value=item,
                    path=f"{path}[{index}]",
                    errors=errors,
                )

    if isinstance(value, dict):
        properties = schema_payload.get("properties")
        properties_map = dict(properties) if isinstance(properties, dict) else {}
        required_fields = schema_payload.get("required")
        if isinstance(required_fields, list):
            for field in required_fields:
                key = str(field or "").strip()
                if key and key not in value:
                    errors.append(f"{path}.{key}: required property is missing")

        for key, nested_schema in properties_map.items():
            if key in value and isinstance(nested_schema, dict):
                validate_json_schema_value(
                    schema=nested_schema,
                    value=value.get(key),
                    path=f"{path}.{key}",
                    errors=errors,
                )

        additional_properties = schema_payload.get("additionalProperties", True)
        known = set(properties_map)
        for key, nested_value in value.items():
            if key in known:
                continue
            if additional_properties is False:
                errors.append(f"{path}.{key}: additional property is not allowed")
            elif isinstance(additional_properties, dict):
                validate_json_schema_value(
                    schema=additional_properties,
                    value=nested_value,
                    path=f"{path}.{key}",
                    errors=errors,
                )

    _validate_composition(schema_payload, value, path, errors)


__all__ = [
    "ANNOTATION_KEYWORDS",
    "EXECUTED_KEYWORDS",
    "unsupported_schema_keywords",
    "validate_json_schema_value",
]
