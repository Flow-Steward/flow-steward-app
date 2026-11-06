"""Structural (JSON Schema) validation of report documents with exact field paths.

A plain ``oneOf`` failure points at a whole component and says nothing useful.
Schema v1 section 7 therefore asks the validator to pick the component
sub-schema by ``type`` (a format sub-schema by its ``type``, a segment by its
shape) and validate against that one, so every error names the exact field.
Messages never quote the offending value: a boundary failure reports codes and
paths, not report data (ADR 0059 decision 11).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from core.application.workflow_documents.report_issues import (
    Issue,
    PathPart,
    describe_type,
    issue,
)
from core.application.workflow_documents.report_schemas_v1 import (
    DEFINITION_SCHEMA_V1,
    SNAPSHOT_SCHEMA_V1,
)

COMPONENT_TYPES = ("text", "metric", "table", "chart", "section")
FORMAT_TYPES = ("text", "boolean", "number", "currency", "percent", "date", "datetime")
#: Format families the schema names, and the format types each one admits.
_FORMAT_FAMILIES: dict[str, tuple[str, ...]] = {
    "format": FORMAT_TYPES,
    "numeric_format": ("number", "currency", "percent"),
    "category_format": ("text", "number", "date", "datetime"),
}
#: ``maxItems`` on these arrays is a section 6 limit with its own code.
_MAX_ITEMS_CODES = {
    "components": "components_too_many",
    "children": "components_too_many",
    "datasets": "datasets_too_many",
    "rows": "rows_too_many",
    "columns": "columns_too_many",
    "series": "series_too_many",
    "segments": "segments_too_many",
    "summary": "segments_too_many",
    "fields": "dataset_fields_too_many",
}
_TEXT_MAX_CHARS = 10_000
_TYPE_KEYWORDS = frozenset({"type", "const", "enum", "oneOf"})


#: The two documents this module validates, by name.
_ROOTS: dict[str, Mapping[str, Any]] = {
    "definition": DEFINITION_SCHEMA_V1,
    "snapshot": SNAPSHOT_SCHEMA_V1,
}
_WHOLE = ""


@lru_cache(maxsize=64)
def _validator(root: str, definition: str) -> Draft202012Validator:
    """A validator for one ``$defs`` entry (or the whole schema) of ``root``."""
    schema = _ROOTS[root]
    if definition == _WHOLE:
        return Draft202012Validator(dict(schema))
    if definition.startswith("segment:"):
        branch = schema["$defs"]["segment"]["oneOf"][int(definition.split(":", 1)[1])]
        return Draft202012Validator({"$defs": schema["$defs"], **branch})
    return Draft202012Validator({"$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"})


def structure_issues(document: Any, root: Literal["definition", "snapshot"]) -> list[Issue]:
    """Every structural problem of ``document`` against the ``root`` schema."""
    return list(_walk(root, _WHOLE, document, ()))


def _walk(root: str, definition: str, instance: Any, base: Sequence[PathPart]) -> Iterator[Issue]:
    validator = _validator(root, definition)
    for error in sorted(validator.iter_errors(instance), key=_error_order):
        path = (*base, *error.absolute_path)
        if error.validator == "oneOf":
            yield from _resolve_one_of(error, path, root)
        else:
            yield from _map_error(error, path)


def _error_order(error: ValidationError) -> tuple[str, str]:
    return (repr(list(error.absolute_path)), str(error.validator))


def _family(schema: Any, root: str) -> str:
    defs = _ROOTS[root]["$defs"]
    for name in ("component", "segment", *_FORMAT_FAMILIES):
        if schema == defs.get(name):
            return name
    return ""


def _resolve_one_of(
    error: ValidationError, path: tuple[PathPart, ...], root: str
) -> Iterator[Issue]:
    instance = error.instance
    family = _family(error.schema, root)
    if not isinstance(instance, dict) or not family:
        yield issue("type_invalid", f"Expected an object, got {describe_type(instance)}", path)
        return
    if family == "segment":
        branch = 0 if "text" in instance and "value" not in instance else 1
        yield from _walk(root, f"segment:{branch}", instance, path)
        return
    kind = instance.get("type")
    if kind is None:
        yield issue("property_required", "'type' is required", path)
        return
    if family == "component":
        if kind not in COMPONENT_TYPES:
            yield issue(
                "component_type_unknown",
                f"Component type must be one of {', '.join(COMPONENT_TYPES)}",
                (*path, "type"),
            )
            return
        yield from _walk(root, f"{kind}_component", instance, path)
        return
    allowed = _FORMAT_FAMILIES[family]
    if kind not in FORMAT_TYPES:
        yield issue(
            "type_invalid", f"Format type must be one of {', '.join(allowed)}", (*path, "type")
        )
    elif kind not in allowed:
        yield issue(
            "format_not_allowed",
            f"Format '{kind}' is not allowed here; use one of {', '.join(allowed)}",
            path,
        )
    else:
        yield from _walk(root, f"format_{kind}", instance, path)


def _map_error(error: ValidationError, path: tuple[PathPart, ...]) -> Iterator[Issue]:
    keyword = str(error.validator)
    if keyword == "additionalProperties":
        known = set((error.schema or {}).get("properties", {}))
        for key in error.instance if isinstance(error.instance, dict) else ():
            if key not in known:
                yield issue("property_unknown", f"Unknown property '{key}'", (*path, key))
        return
    if keyword == "required":
        missing = [
            name
            for name in error.validator_value
            if isinstance(error.instance, dict) and name not in error.instance
        ]
        for name in missing:
            yield issue("property_required", f"'{name}' is required", path)
        return
    if keyword in _TYPE_KEYWORDS:
        yield issue("type_invalid", _type_message(error), path)
        return
    yield issue(_limit_code(keyword, error, path), _value_message(keyword, error), path)


def _type_message(error: ValidationError) -> str:
    expected = error.validator_value
    if error.validator == "const":
        return f"Expected the constant {expected!r}"
    if error.validator == "enum":
        return f"Expected one of {', '.join(map(str, expected))}"
    if isinstance(expected, list):
        expected = " or ".join(map(str, expected))
    return f"Expected {expected}, got {describe_type(error.instance)}"


def _limit_code(keyword: str, error: ValidationError, path: tuple[PathPart, ...]) -> str:
    last = path[-1] if path else ""
    if keyword == "maxItems":
        if isinstance(last, int):
            return "row_shape_invalid"
        return _MAX_ITEMS_CODES.get(str(last), "value_invalid")
    if keyword == "maxLength":
        if last == "summary_text":
            return "summary_too_long"
        if error.validator_value == _TEXT_MAX_CHARS:
            return "text_too_long"
    return "value_invalid"


def _value_message(keyword: str, error: ValidationError) -> str:
    limit = error.validator_value
    messages = {
        "maxItems": f"At most {limit} items are allowed",
        "minItems": f"At least {limit} items are required",
        "maxLength": f"At most {limit} characters are allowed",
        "minLength": f"At least {limit} characters are required",
        "pattern": f"The value does not match the pattern {limit}",
        "minimum": f"The value must be at least {limit}",
        "maximum": f"The value must be at most {limit}",
        "uniqueItems": "Items must be unique",
    }
    return messages.get(keyword, f"The value breaks the '{keyword}' rule")


__all__ = ["COMPONENT_TYPES", "FORMAT_TYPES", "structure_issues"]
